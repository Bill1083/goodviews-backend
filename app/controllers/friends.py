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

        results = [
            {
                "id": p["id"],
                "username": p["username"],
                "is_friend": p["id"] in friend_ids,
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
def add_friend():
    user = request.current_user
    body = request.get_json(silent=True) or {}
    friend_id = body.get("friend_id", "").strip()
    if not friend_id or friend_id == str(user.id):
        return jsonify({"error": "Invalid friend_id"}), 400

    supabase = get_supabase()
    target = supabase.table("profiles").select("id").eq("id", friend_id).execute()
    if not target.data:
        return jsonify({"error": "User not found"}), 404

    existing = (
        supabase.table("friendships")
        .select("id")
        .eq("user_id", str(user.id))
        .eq("friend_id", friend_id)
        .execute()
    )
    if existing.data:
        return jsonify({"message": "Already friends"}), 200

    try:
        supabase.table("friendships").insert([
            {"user_id": str(user.id), "friend_id": friend_id},
            {"user_id": friend_id, "friend_id": str(user.id)},
        ]).execute()
        return jsonify({"message": "Friend added"}), 201
    except Exception as exc:
        return jsonify({"error": "Failed to add friend", "detail": str(exc)}), 500


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
