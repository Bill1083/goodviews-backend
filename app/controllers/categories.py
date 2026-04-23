import re

from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.utils.sanitize import sanitize_text
from app.services.supabase_client import get_supabase

categories_bp = Blueprint("categories", __name__)

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _valid_color(c) -> bool:
    return c is None or bool(_HEX_RE.match(str(c)))


@categories_bp.get("/")
@require_auth
@limiter.limit("60 per minute")
def list_categories():
    user = request.current_user
    supabase = get_supabase()
    try:
        result = (
            supabase.table("categories")
            .select("*")
            .eq("user_id", str(user.id))
            .order("created_at")
            .execute()
        )
        return jsonify(result.data)
    except Exception as exc:
        return jsonify({"error": "Failed to fetch categories", "detail": str(exc)}), 500


@categories_bp.post("/")
@require_auth
@limiter.limit("30 per hour")
def create_category():
    user = request.current_user
    body = request.get_json(silent=True) or {}
    name = sanitize_text(body.get("name", ""))
    if not name:
        return jsonify({"error": "Name is required"}), 400
    if len(name) > 100:
        return jsonify({"error": "Name too long (max 100 characters)"}), 400

    outline_color = body.get("outline_color") or None
    fill_color = body.get("fill_color") or None
    description = sanitize_text(body.get("description", "")) or None

    if not _valid_color(outline_color) or not _valid_color(fill_color):
        return jsonify({"error": "Invalid hex color format"}), 400

    supabase = get_supabase()
    try:
        result = (
            supabase.table("categories")
            .insert({
                "user_id": str(user.id),
                "name": name,
                "outline_color": outline_color,
                "fill_color": fill_color,
                "description": description,
            })
            .execute()
        )
        return jsonify(result.data[0]), 201
    except Exception as exc:
        return jsonify({"error": "Failed to create category", "detail": str(exc)}), 500


@categories_bp.put("/<category_id>")
@require_auth
@limiter.limit("30 per hour")
def update_category(category_id):
    user = request.current_user
    body = request.get_json(silent=True) or {}

    supabase = get_supabase()
    existing = (
        supabase.table("categories")
        .select("id")
        .eq("id", category_id)
        .eq("user_id", str(user.id))
        .execute()
    )
    if not existing.data:
        return jsonify({"error": "Category not found"}), 404

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
            supabase.table("categories")
            .update(updates)
            .eq("id", category_id)
            .execute()
        )
        return jsonify(result.data[0])
    except Exception as exc:
        return jsonify({"error": "Failed to update category", "detail": str(exc)}), 500


@categories_bp.delete("/<category_id>")
@require_auth
@limiter.limit("30 per hour")
def delete_category(category_id):
    user = request.current_user
    supabase = get_supabase()

    existing = (
        supabase.table("categories")
        .select("id")
        .eq("id", category_id)
        .eq("user_id", str(user.id))
        .execute()
    )
    if not existing.data:
        return jsonify({"error": "Category not found"}), 404

    try:
        supabase.table("categories").delete().eq("id", category_id).execute()
        return jsonify({"message": "Deleted"}), 200
    except Exception as exc:
        return jsonify({"error": "Failed to delete category", "detail": str(exc)}), 500
