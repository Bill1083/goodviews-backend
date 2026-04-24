from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.services.supabase_client import get_supabase

friends_bp = Blueprint("friends", __name__)


@friends_bp.get("/search")
@require_auth
@limiter.limit("60 per minute")
def search_users():
    user = request.current_user
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])

    supabase = get_supabase()
    try:
        result = (
            supabase.table("profiles")
            .select("id, username")
            .ilike("username", f"%{q}%")
            .neq("id", str(user.id))
            .limit(10)
            .execute()
        )
        friends_result = (
            supabase.table("friendships")
            .select("friend_id")
            .eq("user_id", str(user.id))
            .execute()
        )
        friend_ids = {r["friend_id"] for r in friends_result.data}

        # Check for outgoing pending requests sent by the current user
        pending_result = (
            supabase.table("friend_requests")
            .select("receiver_id")
            .eq("sender_id", str(user.id))
            .execute()
        )
        pending_ids = {r["receiver_id"] for r in pending_result.data}

        results = [
            {
                "id": p["id"],
                "username": p["username"],
                "is_friend": p["id"] in friend_ids,
                "has_pending_request": p["id"] in pending_ids,
            }
            for p in result.data
        ]
        return jsonify(results)
    except Exception as exc:
        return jsonify({"error": "Search failed", "detail": str(exc)}), 500


@friends_bp.get("/")
@require_auth
@limiter.limit("60 per minute")
def list_friends():
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("friendships")
            .select("friend_id, profiles!friendships_friend_id_fkey(id, username)")
            .eq("user_id", str(user.id))
            .order("created_at")
            .execute()
        )
        friends = [
            {"id": r["profiles"]["id"], "username": r["profiles"]["username"]}
            for r in result.data
            if r.get("profiles")
        ]
        return jsonify(friends)
    except Exception as exc:
        return jsonify({"error": "Failed to fetch friends", "detail": str(exc)}), 500


@friends_bp.post("/")
@require_auth
@limiter.limit("20 per hour")
def send_friend_request():
    """Send a friend request. If the target has already sent one to us, auto-accept."""
    user = request.current_user
    body = request.get_json(silent=True) or {}
    friend_id = body.get("friend_id", "").strip()
    if not friend_id or friend_id == str(user.id):
        return jsonify({"error": "Invalid friend_id"}), 400

    supabase = get_supabase()
    target = supabase.table("profiles").select("id").eq("id", friend_id).execute()
    if not target.data:
        return jsonify({"error": "User not found"}), 404

    # Already friends?
    existing = (
        supabase.table("friendships")
        .select("id")
        .eq("user_id", str(user.id))
        .eq("friend_id", friend_id)
        .execute()
    )
    if existing.data:
        return jsonify({"message": "Already friends"}), 200

    # Already sent a request?
    already_sent = (
        supabase.table("friend_requests")
        .select("id")
        .eq("sender_id", str(user.id))
        .eq("receiver_id", friend_id)
        .execute()
    )
    if already_sent.data:
        return jsonify({"message": "Request already sent"}), 200

    # Reverse request exists? Auto-accept — create friendship and clean up.
    reverse = (
        supabase.table("friend_requests")
        .select("id")
        .eq("sender_id", friend_id)
        .eq("receiver_id", str(user.id))
        .execute()
    )
    if reverse.data:
        try:
            supabase.table("friendships").insert([
                {"user_id": str(user.id), "friend_id": friend_id},
                {"user_id": friend_id, "friend_id": str(user.id)},
            ]).execute()
            supabase.table("friend_requests").delete() \
                .eq("sender_id", friend_id).eq("receiver_id", str(user.id)).execute()
            return jsonify({"message": "Friend added"}), 201
        except Exception as exc:
            return jsonify({"error": "Failed to add friend", "detail": str(exc)}), 500

    # Create the request
    try:
        supabase.table("friend_requests").insert([
            {"sender_id": str(user.id), "receiver_id": friend_id},
        ]).execute()
        return jsonify({"message": "Friend request sent"}), 201
    except Exception as exc:
        return jsonify({"error": "Failed to send request", "detail": str(exc)}), 500


@friends_bp.get("/requests")
@require_auth
@limiter.limit("60 per minute")
def get_friend_requests():
    """Return pending incoming friend requests for the current user."""
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("friend_requests")
            .select("id, sender_id, created_at, profiles!friend_requests_sender_id_fkey(id, username)")
            .eq("receiver_id", str(user.id))
            .order("created_at")
            .execute()
        )
        requests_data = [
            {
                "id": r["id"],
                "sender_id": r["sender_id"],
                "sender_username": r["profiles"]["username"] if r.get("profiles") else r["sender_id"],
                "created_at": r["created_at"],
            }
            for r in result.data
        ]
        return jsonify(requests_data)
    except Exception as exc:
        return jsonify({"error": "Failed to fetch requests", "detail": str(exc)}), 500


@friends_bp.post("/requests/<request_id>/accept")
@require_auth
@limiter.limit("30 per hour")
def accept_friend_request(request_id):
    user = request.current_user
    supabase = get_supabase()

    req = (
        supabase.table("friend_requests")
        .select("id, sender_id, receiver_id")
        .eq("id", request_id)
        .eq("receiver_id", str(user.id))
        .execute()
    )
    if not req.data:
        return jsonify({"error": "Request not found"}), 404

    sender_id = req.data[0]["sender_id"]
    try:
        supabase.table("friendships").insert([
            {"user_id": str(user.id), "friend_id": sender_id},
            {"user_id": sender_id, "friend_id": str(user.id)},
        ]).execute()
        supabase.table("friend_requests").delete().eq("id", request_id).execute()
        return jsonify({"message": "Friend request accepted"}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to accept request", "detail": str(exc)}), 500


@friends_bp.delete("/requests/<request_id>")
@require_auth
@limiter.limit("30 per hour")
def deny_friend_request(request_id):
    user = request.current_user
    supabase = get_supabase()

    req = (
        supabase.table("friend_requests")
        .select("id")
        .eq("id", request_id)
        .eq("receiver_id", str(user.id))
        .execute()
    )
    if not req.data:
        return jsonify({"error": "Request not found"}), 404

    try:
        supabase.table("friend_requests").delete().eq("id", request_id).execute()
        return jsonify({"message": "Friend request denied"}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to deny request", "detail": str(exc)}), 500


@friends_bp.delete("/<friend_id>")
@require_auth
@limiter.limit("20 per hour")
def remove_friend(friend_id):
    user = request.current_user
    supabase = get_supabase()
    try:
        supabase.table("friendships").delete() \
            .eq("user_id", str(user.id)).eq("friend_id", friend_id).execute()
        supabase.table("friendships").delete() \
            .eq("user_id", friend_id).eq("friend_id", str(user.id)).execute()
        return jsonify({"message": "Friend removed"}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to remove friend", "detail": str(exc)}), 500
