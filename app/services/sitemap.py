"""The film pages' sitemap.

Lists the public page of every film enough members have rated to show a
community rating (the same COMMUNITY_MIN_RATINGS threshold the page uses to
let itself be indexed). The site's robots.txt points search engines here.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from app.services.community import COMMUNITY_MIN_RATINGS
from app.services.pg import IN_CHUNK, chunked, paginate
from app.utils.slug import film_path

# The sitemap protocol's per-file limit.
MAX_URLS = 50_000


def indexable_films(supabase) -> list[dict]:
    """[{movie_id, title, lastmod}] for every film rated by at least
    COMMUNITY_MIN_RATINGS different members, most recently rated first."""
    rows = paginate(lambda: supabase.table("reviews").select("movie_id, user_id, created_at").order("created_at").order("id"))
    raters: dict[int, set] = {}
    latest: dict[int, str] = {}
    for row in rows:
        movie_id, user_id = row.get("movie_id"), row.get("user_id")
        if movie_id is None or user_id is None:
            continue
        raters.setdefault(movie_id, set()).add(user_id)
        created = row.get("created_at") or ""
        if created > latest.get(movie_id, ""):
            latest[movie_id] = created

    ids = [m for m, users in raters.items() if len(users) >= COMMUNITY_MIN_RATINGS]
    titles: dict[int, str | None] = {}
    for chunk in chunked(ids, IN_CHUNK):
        for movie in supabase.table("movies").select("id, title").in_("id", chunk).execute().data or []:
            titles[movie["id"]] = movie.get("title")

    films = [{"movie_id": m, "title": titles.get(m), "lastmod": latest.get(m, "")[:10]} for m in ids if m in titles]
    films.sort(key=lambda f: (f["lastmod"], f["movie_id"]), reverse=True)
    return films[:MAX_URLS]


def films_sitemap_xml(films: list[dict], site_url: str) -> str:
    base = site_url.rstrip("/")
    entries = []
    for film in films:
        loc = escape(f"{base}{film_path(film['movie_id'], film['title'])}")
        lastmod = f"\n    <lastmod>{film['lastmod']}</lastmod>" if film["lastmod"] else ""
        entries.append(f"  <url>\n    <loc>{loc}</loc>{lastmod}\n  </url>")
    body = "\n".join(entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{body}{chr(10) if body else ''}"
        "</urlset>\n"
    )
