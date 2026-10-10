"""order_collection_parts — pure function, no app context needed."""
from app.services.collection_ordering import _stem, order_collection_parts


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
