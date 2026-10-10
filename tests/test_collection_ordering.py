"""order_collection_parts — pure function, no app context needed."""
from app.services.collection_ordering import (
    _stem,
    filter_by_franchise_prefix,
    franchise_display_name,
    order_by_release_date,
    order_collection_parts,
)


def part(mid, title, release_date=None):
    return {"id": mid, "title": title, "release_date": release_date}


def test_numbered_sequels_all_bucket_as_direct_in_release_order():
    parts = [
        part(3, "Kung Fu Panda 3", "2016-01-29"),
        part(1, "Kung Fu Panda", "2008-06-06"),
        part(2, "Kung Fu Panda 2", "2011-05-26"),
    ]
    ordered = order_collection_parts(parts, seed_title="Kung Fu Panda")
    assert [p["id"] for p in ordered] == [1, 2, 3]


def test_colon_subtitled_short_buckets_after_the_numbered_sequels():
    parts = [
        part(2, "Kung Fu Panda 2", "2011-05-26"),
        part(99, "Kung Fu Panda: Secrets of the Scroll", "2016-01-23"),
        part(1, "Kung Fu Panda", "2008-06-06"),
    ]
    ordered = order_collection_parts(parts, seed_title="Kung Fu Panda")
    assert [p["id"] for p in ordered] == [1, 2, 99]


def test_a_differently_titled_spin_off_buckets_as_other():
    parts = [
        part(1, "Shrek", "2001-05-18"),
        part(99, "Puss in Boots", "2011-10-28"),
    ]
    ordered = order_collection_parts(parts, seed_title="Shrek")
    assert [p["id"] for p in ordered] == [1, 99]


def test_missing_release_date_sorts_last_within_its_bucket_not_first():
    parts = [
        part(2, "Toy Story 2", None),
        part(3, "Toy Story 3", "2010-06-18"),
        part(1, "Toy Story", "1995-11-22"),
    ]
    ordered = order_collection_parts(parts, seed_title="Toy Story")
    assert [p["id"] for p in ordered] == [1, 3, 2]


def test_the_seed_itself_as_the_first_film_does_not_crash():
    parts = [part(1, "Toy Story", "1995-11-22")]
    ordered = order_collection_parts(parts, seed_title="Toy Story")
    assert [p["id"] for p in ordered] == [1]


def test_an_empty_parts_list_returns_empty():
    assert order_collection_parts([], seed_title="Toy Story") == []


def test_stem_strips_trailing_numerals_and_numbered_colon_subtitles():
    assert _stem("Kung Fu Panda 2") == "kung fu panda"
    assert _stem("Kung Fu Panda: Part II") == "kung fu panda"
    assert _stem("Toy Story") == "toy story"


def test_stem_leaves_a_non_numbered_colon_subtitle_alone():
    """A deliberate, accepted gap: an arbitrary ": Subtitle" is never
    stripped, only "Part N"/"Chapter N" — otherwise a short's own subtitle
    ("Kung Fu Panda: Secrets of the Scroll") would wrongly share its seed's
    stem too. The cost is that non-numbered sequel subtitles (e.g. a
    "Into the Spider-Verse" / "Across the Spider-Verse" pair) don't stem
    together either — see collection_ordering.py's module docstring."""
    assert _stem("Spider-Man: Into the Spider-Verse") == "spider-man: into the spider-verse"


# ─── Fallback for a franchise TMDB never collected ──────────────────────────

def test_franchise_prefix_filter_finds_colon_subtitled_siblings_tmdb_never_grouped():
    """The actual reported case: Spider-Man: Homecoming has no TMDB
    collection at all, but its TMDB recommendations do include other
    Spider-Man films — mixed in with a pile of unrelated MCU/DC noise that
    must be filtered out. "The Amazing Spider-Man 2" (a different reboot,
    different actor) is correctly excluded — "Amazing" makes its prefix
    "amazing spider-man", not "spider-man". The 2004 Raimi "Spider-Man 2" IS
    kept, by design: it shares the exact root title, and this fallback is
    deliberately loose about different reboots/continuities of the same
    named character — see filter_by_franchise_prefix's docstring."""
    candidates = [
        part(102382, "The Amazing Spider-Man 2", "2014-04-16"),  # different reboot — excluded (different prefix)
        part(634649, "Spider-Man: No Way Home", "2021-12-15"),
        part(100402, "Captain America: The Winter Soldier", "2014-03-20"),  # unrelated noise
        part(24428, "The Avengers", "2012-04-25"),  # unrelated noise
        part(558, "Spider-Man 2", "2004-06-25"),  # different continuity, same root title — kept
    ]
    siblings = filter_by_franchise_prefix(candidates, seed_title="Spider-Man: Homecoming")
    assert [p["id"] for p in siblings] == [634649, 558]


def test_franchise_prefix_filter_returns_nothing_for_a_standalone_film():
    candidates = [
        part(100, "Some Other Drama", "2010-01-01"),
        part(200, "Yet Another Unrelated Film", "2015-01-01"),
    ]
    assert filter_by_franchise_prefix(candidates, seed_title="The Shawshank Redemption") == []


def test_franchise_prefix_filter_handles_an_empty_seed_title():
    assert filter_by_franchise_prefix([part(1, "Anything")], seed_title="") == []


def test_order_by_release_date_sorts_chronologically_missing_last():
    parts = [
        part(2, "B", None),
        part(3, "C", "2019-01-01"),
        part(1, "A", "2017-01-01"),
    ]
    assert [p["id"] for p in order_by_release_date(parts)] == [1, 3, 2]


def test_franchise_display_name_strips_subtitle_article_and_numbering():
    assert franchise_display_name("Spider-Man: Homecoming") == "Spider-Man"
    assert franchise_display_name("The Fast and the Furious 2") == "Fast and the Furious"
    assert franchise_display_name("Toy Story") == "Toy Story"
