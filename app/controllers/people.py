from flask import Blueprint, jsonify, request

from app import limiter
from app.utils.auth import require_auth
from app.services.tmdb import search_people, get_person_details

people_bp = Blueprint("people", __name__)


@people_bp.get("/search")
@require_auth
@limiter.limit("60 per minute")
def search_people_endpoint():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"error": "Query must be at least 2 characters"}), 400
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    try:
        data = search_people(q, page)
        return jsonify(data), 200
    except Exception as exc:
        return jsonify({"error": "Failed to search people", "detail": str(exc)}), 500


@people_bp.get("/<int:person_id>")
@require_auth
@limiter.limit("60 per minute")
def get_person(person_id: int):
    try:
        data = get_person_details(person_id)
        return jsonify(data), 200
    except Exception as exc:
        return jsonify({"error": "Failed to fetch person details", "detail": str(exc)}), 500
