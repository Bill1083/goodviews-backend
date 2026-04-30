from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.utils.sanitize import sanitize_text
from app.services.supabase_client import get_supabase

reviews_bp = Blueprint("reviews", __name__)


@reviews_bp.post("/")
@require_auth
@limiter.limit("20 per hour")
def create_review():
    user = request.current_user
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "JSON body required"}), 400

    movie_id = body.get("movie_id")
    rating = body.get("rating")
    review_text = sanitize_text(body.get("review_text", ""))

    if not movie_id or not isinstance(movie_id, int):
        return jsonify({"error": "Valid movie_id (integer) is required"}), 400

    if rating is None or not (1 <= float(rating) <= 5):
        return jsonify({"error": "Rating must be between 1 and 5"}), 400

    # Upsert the movie stub so the FK constraint is satisfied
    movie_data = {
        "id": movie_id,
        "title": sanitize_text(body.get("title", "")),
        "poster_path": body.get("poster_path"),
        "release_date": body.get("release_date"),
    }
    vote_average = body.get("vote_average")
    if vote_average is not None:
        try:
            movie_data["vote_average"] = float(vote_average)
        except (ValueError, TypeError):
            pass

    # Optional: category_id, category_ids, group_ids, friend_ids
    category_id = body.get("category_id") or None
    raw_category_ids = body.get("category_ids") or ([] if category_id is None else [category_id])
    category_ids = [str(c) for c in raw_category_ids if c] if isinstance(raw_category_ids, list) else []
    raw_group_ids = body.get("group_ids") or []
    group_ids = [str(g) for g in raw_group_ids if g] if isinstance(raw_group_ids, list) else []
    raw_friend_ids = body.get("friend_ids") or []
    friend_ids = [str(f) for f in raw_friend_ids if f] if isinstance(raw_friend_ids, list) else []

    supabase = get_supabase()

    try:
        supabase.table("movies").upsert(movie_data, on_conflict="id").execute()

        review_payload = {
            "user_id": str(user.id),
            "movie_id": movie_id,
            "rating": float(rating),
            "review_text": review_text,
            "category_id": category_id,
            "category_ids": category_ids,
        }
        result = supabase.table("reviews").insert(review_payload).execute()
        review = result.data[0]

        # Record group recommendations if provided
        if group_ids and review.get("id"):
            rec_rows = [{"review_id": review["id"], "group_id": gid} for gid in group_ids]
            supabase.table("group_recommendations").insert(rec_rows).execute()

        # Record individual friend notifications if provided
        if friend_ids and review.get("id"):
            notif_rows = [
                {
                    "user_id": fid,
                    "sender_id": str(user.id),
                    "movie_id": movie_id,
                    "message": "recommended a movie to you",
                }
                for fid in friend_ids
            ]
            supabase.table("notifications").insert(notif_rows).execute()

        return jsonify(review), 201
    except Exception as exc:
        return jsonify({"error": "Failed to save review", "detail": str(exc)}), 500


@reviews_bp.put("/<review_id>")
@require_auth
@limiter.limit("30 per hour")
def update_review(review_id: str):
    user = request.current_user
    body = request.get_json(silent=True)
    if not body:
        return jsonify({"error": "JSON body required"}), 400

    supabase = get_supabase()

    # Verify ownership
    existing_result = (
        supabase.table("reviews")
        .select("id, user_id, movie_id")
        .eq("id", review_id)
        .eq("user_id", str(user.id))
        .execute()
    )
    if not existing_result.data:
        return jsonify({"error": "Review not found or not owned by user"}), 404
    existing = existing_result.data[0]

    update_data = {}

    rating = body.get("rating")
    if rating is not None:
        if not (1 <= float(rating) <= 5):
            return jsonify({"error": "Rating must be between 1 and 5"}), 400
        update_data["rating"] = float(rating)

    if "review_text" in body:
        update_data["review_text"] = sanitize_text(body.get("review_text", ""))

    if "category_id" in body:
        update_data["category_id"] = body.get("category_id") or None

    try:
        if "category_ids" in body:
            raw_cids = body.get("category_ids") or []
            if isinstance(raw_cids, list):
                update_data["category_ids"] = [str(c) for c in raw_cids if c]
                # Keep legacy category_id as the first element for backwards compat
                update_data["category_id"] = update_data["category_ids"][0] if update_data["category_ids"] else None

        if update_data:
            result = (
                supabase.table("reviews")
                .update(update_data)
                .eq("id", review_id)
                .eq("user_id", str(user.id))
                .execute()
            )
            review = result.data[0] if result.data else existing
        else:
            review = existing

        # Add group recommendations (new shares only — insert and ignore duplicates)
        raw_group_ids = body.get("group_ids") or []
        group_ids = [str(g) for g in raw_group_ids if g] if isinstance(raw_group_ids, list) else []
        if group_ids:
            rec_rows = [{"review_id": review_id, "group_id": gid} for gid in group_ids]
            supabase.table("group_recommendations").insert(rec_rows).execute()

        # Send individual friend notifications
        raw_friend_ids = body.get("friend_ids") or []
        friend_ids = [str(f) for f in raw_friend_ids if f] if isinstance(raw_friend_ids, list) else []
        if friend_ids:
            notif_rows = [
                {
                    "user_id": fid,
                    "sender_id": str(user.id),
                    "movie_id": existing["movie_id"],
                    "message": "recommended a movie to you",
                }
                for fid in friend_ids
            ]
            supabase.table("notifications").insert(notif_rows).execute()

        return jsonify(review), 200
    except Exception as exc:
        return jsonify({"error": "Failed to update review", "detail": str(exc)}), 500


@reviews_bp.patch("/<review_id>/rewatch")
@require_auth
@limiter.limit("120 per hour")
def increment_rewatch(review_id: str):
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("reviews")
            .select("rewatch_count")
            .eq("id", review_id)
            .eq("user_id", str(user.id))
            .execute()
        )
        if not result.data:
            return jsonify({"error": "Review not found"}), 404

        current = result.data[0].get("rewatch_count") or 0
        new_count = current + 1

        supabase.table("reviews").update({"rewatch_count": new_count}).eq("id", review_id).eq("user_id", str(user.id)).execute()
        return jsonify({"rewatch_count": new_count}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to update rewatch count", "detail": str(exc)}), 500


@reviews_bp.patch("/<review_id>/rewatch/decrement")
@require_auth
@limiter.limit("120 per hour")
def decrement_rewatch(review_id: str):
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("reviews")
            .select("rewatch_count")
            .eq("id", review_id)
            .eq("user_id", str(user.id))
            .execute()
        )
        if not result.data:
            return jsonify({"error": "Review not found"}), 404

        current = result.data[0].get("rewatch_count") or 0
        new_count = max(0, current - 1)

        supabase.table("reviews").update({"rewatch_count": new_count}).eq("id", review_id).eq("user_id", str(user.id)).execute()
        return jsonify({"rewatch_count": new_count}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to update rewatch count", "detail": str(exc)}), 500


@reviews_bp.get("/bulk-friend-ratings")
@require_auth
@limiter.limit("60 per minute")
def bulk_friend_ratings():
    """Return avg friend rating (including self) for all of the user's reviewed movies."""
    user = request.current_user
    supabase = get_supabase()
    try:
        # Get the calling user's reviewed movie IDs
        my_reviews_result = (
            supabase.table("reviews")
            .select("movie_id, rating")
            .eq("user_id", str(user.id))
            .execute()
        )
        my_ratings = {r["movie_id"]: r["rating"] for r in my_reviews_result.data}
        if not my_ratings:
            return jsonify({})

        movie_ids = list(my_ratings.keys())

        # Get friend IDs
        friends_result = (
            supabase.table("friendships")
            .select("friend_id")
            .eq("user_id", str(user.id))
            .execute()
        )
        friend_ids = [r["friend_id"] for r in friends_result.data]

        friend_ratings: dict[int, list[float]] = {mid: [] for mid in movie_ids}
        if friend_ids:
            fr_result = (
                supabase.table("reviews")
                .select("movie_id, rating")
                .in_("movie_id", movie_ids)
                .in_("user_id", friend_ids)
                .execute()
            )
            for r in fr_result.data:
                mid = r["movie_id"]
                if mid in friend_ratings:
                    friend_ratings[mid].append(r["rating"])

        # Build result: avg of (my rating + friend ratings) per movie
        result = {}
        for mid, my_r in my_ratings.items():
            all_r = [my_r] + friend_ratings.get(mid, [])
            result[str(mid)] = round(sum(all_r) / len(all_r), 2)

        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": "Failed to fetch bulk ratings", "detail": str(exc)}), 500


@reviews_bp.get("/me")
@require_auth
@limiter.limit("60 per minute")
def my_reviews():
    user = request.current_user
    page = request.args.get("page", 1, type=int)
    page_size = min(request.args.get("page_size", 20, type=int), 500)
    offset = (page - 1) * page_size

    supabase = get_supabase()
    try:
        result = (
            supabase.table("reviews")
            .select("*, movies(id, title, poster_path, release_date)")
            .eq("user_id", str(user.id))
            .order("created_at", desc=True)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        return jsonify({"reviews": result.data, "page": page, "page_size": page_size})
    except Exception as exc:
        return jsonify({"error": "Failed to fetch reviews", "detail": str(exc)}), 500


@reviews_bp.delete("/<review_id>")
@require_auth
def delete_review(review_id: str):
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("reviews")
            .delete()
            .eq("id", review_id)
            .eq("user_id", str(user.id))
            .execute()
        )
        if not result.data:
            return jsonify({"error": "Review not found or not owned by user"}), 404
        return jsonify({"deleted": True}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to delete review", "detail": str(exc)}), 500


@reviews_bp.get("/movie/<int:movie_id>")
@require_auth
@limiter.limit("60 per minute")
def movie_reviews(movie_id: int):
    """Returns the current user's review + friends' reviews for a given movie."""
    user = request.current_user
    supabase = get_supabase()
    try:
        # User's own review for this movie
        my_result = (
            supabase.table("reviews")
            .select("*")
            .eq("user_id", str(user.id))
            .eq("movie_id", movie_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        my_review = my_result.data[0] if my_result.data else None

        # Friend IDs
        friends_result = (
            supabase.table("friendships")
            .select("friend_id")
            .eq("user_id", str(user.id))
            .execute()
        )
        friend_ids = [r["friend_id"] for r in friends_result.data]

        friend_reviews = []
        avg_rating = None
        if friend_ids:
            fr_result = (
                supabase.table("reviews")
                .select("id, rating, review_text, created_at, user_id, rewatch_count, profiles(id, username)")
                .eq("movie_id", movie_id)
                .in_("user_id", friend_ids)
                .order("created_at", desc=True)
                .execute()
            )
            friend_reviews = fr_result.data

        # Include the user's own rating in the average (if they have a review)
        all_ratings = [r["rating"] for r in friend_reviews]
        if my_review:
            all_ratings.append(my_review["rating"])
        if all_ratings:
            avg_rating = round(sum(all_ratings) / len(all_ratings), 1)

        return jsonify({
            "my_review": my_review,
            "friend_reviews": friend_reviews,
            "avg_friend_rating": avg_rating,
        })
    except Exception as exc:
        return jsonify({"error": "Failed to fetch movie reviews", "detail": str(exc)}), 500
