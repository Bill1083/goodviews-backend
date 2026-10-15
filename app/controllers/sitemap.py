from flask import Blueprint, Response, current_app

from app import limiter
from app.services import sitemap
from app.services.cache import cache_get, cache_set
from app.services.supabase_client import get_supabase
from app.utils.errors import server_error

sitemap_bp = Blueprint("sitemap", __name__)

SITEMAP_CACHE_TTL_SECONDS = 6 * 60 * 60


@sitemap_bp.get("/films.xml")
@limiter.limit("30 per minute")
def films():
    """Public: the film pages search engines should index. The website's
    robots.txt points here (a sitemap may live on another host when the
    site's own robots.txt references it)."""
    site_url = current_app.config["PUBLIC_SITE_URL"]
    key = f"sitemap:films:{site_url}"
    xml = cache_get(key)
    if xml is None:
        try:
            xml = sitemap.films_sitemap_xml(sitemap.indexable_films(get_supabase()), site_url)
        except Exception as exc:
            return server_error("Failed to build the sitemap", exc, 500)
        cache_set(key, xml, SITEMAP_CACHE_TTL_SECONDS)
    return Response(xml, mimetype="application/xml", headers={"Cache-Control": "public, max-age=3600"})
