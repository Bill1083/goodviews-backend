import re

from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.utils.sanitize import sanitize_text
from app.services.supabase_client import get_supabase

friend_groups_bp = Blueprint("friend_groups", __name__)

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _valid_color(c) -> bool:
    return c is None or bool(_HEX_RE.match(str(c)))


def _serialize_group(g: dict) -> dict:
    members = [
        {"id": m["profiles"]["id"], "username": m["profiles"]["username"]}
        for m in (g.get("group_members") or [])
        if m.get("profiles")
    ]
    return {
        "id": g["id"],
        "name": g["name"],
        "outline_color": g.get("outline_color"),
        "fill_color": g.get("fill_color"),
        "description": g.get("description"),
        "created_at": g["created_at"],
        "members": members,
    }


@friend_groups_bp.get("/")
@require_auth
@limiter.limit("60 per minute")
def list_groups():
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("friend_groups")
            .select("*, group_members(user_id, profiles!group_members_user_id_fkey(id, username))")
            .eq("owner_id", str(user.id))
            .order("created_at")
            .execute()
        )
        return jsonify([_serialize_group(g) for g in result.data])
    except Exception as exc:
        return jsonify({"error": "Failed to fetch groups", "detail": str(exc)}), 500


@friend_groups_bp.post("/")
@require_auth
@limiter.limit("20 per hour")
def create_group():
    user = request.current_user
    body = request.get_json(silent=True) or {}
    name = sanitize_text(body.get("name", ""))
    if not name:
        return jsonify({"error": "Name is required"}), 400
    if len(name) > 100:
        return jsonify({"error": "Name too long"}), 400

    outline_color = body.get("outline_color") or None
    fill_color = body.get("fill_color") or None
    description = sanitize_text(body.get("description", "")) or None

    if not _valid_color(outline_color) or not _valid_color(fill_color):
        return jsonify({"error": "Invalid hex color format"}), 400

    supabase = get_supabase()
    try:
        result = (
            supabase.table("friend_groups")
            .insert({
                "owner_id": str(user.id),
                "name": name,
                "outline_color": outline_color,
                "fill_color": fill_color,
                "description": description,
            })
            .execute()
        )
        new_group = result.data[0]
        new_group["members"] = []
        return jsonify(new_group), 201
    except Exception as exc:
        return jsonify({"error": "Failed to create group", "detail": str(exc)}), 500


@friend_groups_bp.put("/<group_id>")
@require_auth
@limiter.limit("20 per hour")
def update_group(group_id):
    user = request.current_user
    body = request.get_json(silent=True) or {}

    supabase = get_supabase()
    existing = (
        supabase.table("friend_groups")
        .select("id")
        .eq("id", group_id)
        .eq("owner_id", str(user.id))
        .execute()
    )
    if not existing.data:
        return jsonify({"error": "Group not found"}), 404

    updates = {}
    if "name" in body:
        name = sanitize_text(body["name"])
        if not name:
            return jsonify({"error": "Name is required"}), 400
        if len(name) > 100:
            return jsonify({"error": "Name too long"}), 400
        updates["name"] = name
    if "outline_color" in body:
        c = body["outline_color"] or None
        if not _valid_color(c):
            return jsonify({"error": "Invalid hex color format"}), 400
        updates["outline_color"] = c
    if "fill_color" in body:
        c = body["fill_color"] or None
        if not _valid_color(c):
            return jsonify({"error": "Invalid hex color format"}), 400
        updates["fill_color"] = c
    if "description" in body:
        updates["description"] = sanitize_text(body.get("description", "")) or None

    if not updates:
        return jsonify({"error": "No fields to update"}), 400

    try:
        result = (
            supabase.table("friend_groups")
            .update(updates)
            .eq("id", group_id)
            .execute()
        )
        return jsonify(result.data[0])
    except Exception as exc:
        return jsonify({"error": "Failed to update group", "detail": str(exc)}), 500


@friend_groups_bp.delete("/<group_id>")
@require_auth
@limiter.limit("20 per hour")
def delete_group(group_id):
    user = request.current_user
    supabase = get_supabase()

    existing = (
        supabase.table("friend_groups")
        .select("id")
        .eq("id", group_id)
        .eq("owner_id", str(user.id))
        .execute()
    )
    if not existing.data:
        return jsonify({"error": "Group not found"}), 404

    try:
        supabase.table("friend_groups").delete().eq("id", group_id).execute()
        return jsonify({"message": "Deleted"}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to delete group", "detail": str(exc)}), 500


@friend_groups_bp.post("/<group_id>/members")
@require_auth
@limiter.limit("30 per hour")
def add_member(group_id):
    user = request.current_user
    body = request.get_json(silent=True) or {}
    member_id = body.get("user_id", "").strip()
    if not member_id:
        return jsonify({"error": "user_id is required"}), 400

    supabase = get_supabase()
    group = (
        supabase.table("friend_groups")
        .select("id")
        .eq("id", group_id)
        .eq("owner_id", str(user.id))
        .execute()
    )
    if not group.data:
        return jsonify({"error": "Group not found"}), 404

    try:
        supabase.table("group_members").insert({
            "group_id": group_id,
            "user_id": member_id,
        }).execute()
        return jsonify({"message": "Member added"}), 201
    except Exception as exc:
        return jsonify({"error": "Failed to add member", "detail": str(exc)}), 500


@friend_groups_bp.delete("/<group_id>/members/<member_id>")
@require_auth
@limiter.limit("30 per hour")
def remove_member(group_id, member_id):
    user = request.current_user
    supabase = get_supabase()

    group = (
        supabase.table("friend_groups")
        .select("id")
        .eq("id", group_id)
        .eq("owner_id", str(user.id))
        .execute()
    )
    if not group.data:
        return jsonify({"error": "Group not found"}), 404

    try:
        supabase.table("group_members").delete() \
            .eq("group_id", group_id).eq("user_id", member_id).execute()
        return jsonify({"message": "Member removed"}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to remove member", "detail": str(exc)}), 500
