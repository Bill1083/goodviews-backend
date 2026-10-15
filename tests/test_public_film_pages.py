"""What the public film pages and the film sitemap expose: totals only, never
who rated what, and only once enough members have rated a film. Also the
slug rule the client mirrors, and the API's security headers."""
import xml.etree.ElementTree as ET

import pytest

from app import create_app
from app.controllers import movies as movies_controller
from app.controllers import sitemap as sitemap_controller
from app.services import community, sitemap
from app.utils.slug import film_path, slugify
from tests.fakes import FakeSupabase

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def review(user, movie, rating, created="2026-10-01T10:00:00+00:00", rid=None):
    return {"id": rid or f"{user}-{movie}-{created}", "user_id": user, "movie_id": movie, "rating": rating, "created_at": created}


# ─── Slugs (mirrored by src/utils/filmUrl.ts in the client) ──────────────────

@pytest.mark.parametrize("title, slug", [
    ("The Matrix", "the-matrix"),
    ("Léon: The Professional", "leon-the-professional"),
    ("Amélie", "amelie"),
    ("Fast & Furious", "fast-and-furious"),
    ("  WALL·E  ", "wall-e"),
    ("Spider-Man: Into the Spider-Verse", "spider-man-into-the-spider-verse"),
    ("千と千尋の神隠し", ""),
    (None, ""),
])
def test_slugify(title, slug):
    assert slugify(title) == slug


def test_long_titles_are_cut_without_a_trailing_hyphen():
    slug = slugify("A " * 60)
    assert len(slug) <= 80 and not slug.endswith("-")


def test_film_path():
    assert film_path(603, "The Matrix") == "/film/603-the-matrix"
    assert film_path(129, "千と千尋の神隠し") == "/film/129"


# ─── Community totals ────────────────────────────────────────────────────────

def test_below_the_threshold_only_the_count_is_shown():
    db = FakeSupabase({"reviews": [review("a", 1, 5), review("b", 1, 2)]})
    assert community.community_stats(db, 1) == {"movie_id": 1, "rating_count": 2, "average_rating": None, "distribution": None}


def test_totals_once_enough_members_have_rated():
    db = FakeSupabase({"reviews": [review("a", 1, 5), review("b", 1, 4), review("c", 1, 3.5), review("d", 2, 1)]})
    stats = community.community_stats(db, 1)
    assert stats["rating_count"] == 3
    assert stats["average_rating"] == 4.2
    assert stats["distribution"] == {"1": 0, "2": 0, "3": 0, "4": 2, "5": 1}  # 3.5 rounds up to 4


def test_each_member_counts_once_with_their_newest_rating():
    rows = [
        review("a", 1, 1, "2026-01-01T00:00:00+00:00"),
        review("a", 1, 5, "2026-06-01T00:00:00+00:00"),
        review("b", 1, 5),
        review("c", 1, 5),
    ]
    stats = community.community_stats(FakeSupabase({"reviews": rows}), 1)
    assert stats["rating_count"] == 3
    assert stats["average_rating"] == 5.0


def test_nothing_identifying_leaves_the_service():
    rows = [review(f"user-{i}", 1, 4) for i in range(5)]
    stats = community.community_stats(FakeSupabase({"reviews": rows}), 1)
    assert set(stats) == {"movie_id", "rating_count", "average_rating", "distribution"}
    assert "user-" not in repr(stats)


# ─── Sitemap ─────────────────────────────────────────────────────────────────

def sitemap_db():
    return FakeSupabase({
        "reviews": [
            review("a", 603, 5, "2026-09-01T00:00:00+00:00"),
            review("b", 603, 4, "2026-10-02T00:00:00+00:00"),
            review("c", 603, 4, "2026-09-05T00:00:00+00:00"),
            review("c", 603, 3, "2026-09-06T00:00:00+00:00"),  # same member twice
            review("a", 13, 5),
            review("b", 13, 5),
            review("b", 13, 4, "2026-08-01T00:00:00+00:00"),  # 2 members only
            review("a", 129, 5, "2026-07-01T00:00:00+00:00"),
            review("b", 129, 5, "2026-07-02T00:00:00+00:00"),
            review("c", 129, 5, "2026-07-03T00:00:00+00:00"),
        ],
        "movies": [
            {"id": 603, "title": "The Matrix"},
            {"id": 13, "title": "Forrest Gump"},
            {"id": 129, "title": "千と千尋の神隠し"},
        ],
    })


def test_sitemap_lists_only_films_with_enough_raters():
    films = sitemap.indexable_films(sitemap_db())
    assert [f["movie_id"] for f in films] == [603, 129]  # most recently rated first
    assert films[0]["lastmod"] == "2026-10-02"


def test_sitemap_xml_is_valid_and_uses_the_site_url():
    xml = sitemap.films_sitemap_xml(sitemap.indexable_films(sitemap_db()), "https://example.film/")
    root = ET.fromstring(xml)
    locs = [el.text for el in root.findall("sm:url/sm:loc", NS)]
    assert locs == ["https://example.film/film/603-the-matrix", "https://example.film/film/129"]
    assert root.find("sm:url/sm:lastmod", NS).text == "2026-10-02"


def test_an_empty_sitemap_is_still_valid():
    root = ET.fromstring(sitemap.films_sitemap_xml([], "https://example.film"))
    assert root.findall("sm:url", NS) == []


# ─── Routes ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    app = create_app()
    app.config.update(TESTING=True, RATELIMIT_ENABLED=False, PUBLIC_SITE_URL="https://example.film")
    db = sitemap_db()
    store = {}
    for module in (movies_controller, sitemap_controller):
        monkeypatch.setattr(module, "get_supabase", lambda: db)
        monkeypatch.setattr(module, "cache_get", lambda key: store.get(key))
        monkeypatch.setattr(module, "cache_set", lambda key, value, ttl=None: store.__setitem__(key, value))
    with app.test_client() as c:
        c.store = store
        yield c


def test_community_route_is_public_and_cached(client):
    res = client.get("/api/movies/603/community")
    assert res.status_code == 200
    assert res.get_json()["rating_count"] == 3
    assert "community:603" in client.store


def test_sitemap_route_serves_xml(client):
    res = client.get("/api/sitemap/films.xml")
    assert res.status_code == 200
    assert res.mimetype == "application/xml"
    assert b"https://example.film/film/603-the-matrix" in res.data


def test_api_responses_carry_security_headers(client):
    res = client.get("/api/health")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert "Strict-Transport-Security" not in res.headers  # plain http in tests

    secure = client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
    assert secure.headers["Strict-Transport-Security"].startswith("max-age=")
