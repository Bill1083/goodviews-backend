from flask import Blueprint, jsonify, request

from app import limiter
from app.services import tmdb

movies_bp = Blueprint("movies", __name__)


@movies_bp.get("/search")
@limiter.limit("30 per minute")
def search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    if len(query) > 200:
        return jsonify({"error": "Query too long"}), 400

    page = request.args.get("page", 1, type=int)
    page = max(1, min(page, 500))  # TMDB supports up to page 500

    try:
        data = tmdb.search_movies(query, page)
        return jsonify(data)
    except Exception as exc:
        return jsonify({"error": "Failed to fetch movies", "detail": str(exc)}), 502


@movies_bp.get("/<int:movie_id>")
@limiter.limit("60 per minute")
def details(movie_id: int):
    try:
        data = tmdb.get_movie_details(movie_id)
        return jsonify(data)
    except Exception as exc:
        return jsonify({"error": "Failed to fetch movie details", "detail": str(exc)}), 502
