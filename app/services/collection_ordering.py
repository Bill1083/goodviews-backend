"""Ordering a TMDB "collection" (franchise) for the "<Franchise> Universe"
row on the movie detail modal — direct sequels/prequels of the movie the
modal is open on first, spin-offs/shorts after.

TMDB's /collection/{id} response gives a flat `parts` list with no relation-
type field at all: no way to ask "is this a sequel or a spin-off" directly.
What follows is a heuristic, not ground truth — see order_collection_parts'
docstring for its known failure modes and why they're an acceptable tail
rather than something worth a bigger fix.
"""
import re

_ROMAN_OR_DIGIT = r"(\d+|i{1,3}|iv|v|vi{0,3}|ix|x)"
# A trailing bare numbering token — "2", "II" — stripped repeatedly so
# "Kung Fu Panda 3" stems to "kung fu panda".
_TRAILING_NUMERAL_RE = re.compile(rf"\s+{_ROMAN_OR_DIGIT}$", re.IGNORECASE)
# A colon-subtitle that is ITSELF just a numbering continuation —
# "Kung Fu Panda: Part II" — stripped the same way. Deliberately narrow:
# an arbitrary ": Subtitle" (a short's "Secrets of the Scroll", a spin-off's
# own name) is never stripped, only "Part N"/"Chapter N". A colon-subtitled
# sequel that isn't numbered at all (e.g. "Spider-Man: Into the Spider-Verse"
# followed by "...Across the Spider-Verse") won't share a stem under this
# rule — a known, accepted gap (see order_collection_parts' docstring): the
# alternative, treating every colon-subtitle as strippable, was tried first
# and wrongly matched every short/spin-off as a "direct" sequel too.
_NUMBERING_TAIL_RE = re.compile(rf"^(part|chapter)\s+{_ROMAN_OR_DIGIT}$", re.IGNORECASE)


def _stem(title: str) -> str:
    base = (title or "").strip()
    prev = None
    while prev != base:
        prev = base
        if ":" in base:
            head, _, tail = base.rpartition(":")
            if _NUMBERING_TAIL_RE.match(tail.strip()):
                base = head.strip()
                continue
        stripped = _TRAILING_NUMERAL_RE.sub("", base)
        if stripped != base:
            base = stripped.strip()
    return base.lower()


def _is_direct_sequel_or_prequel(title: str, seed_stem: str) -> bool:
    """True for "X 2", "X: Part II", "X II" and the self-match case (the
    seed itself, or a reboot sharing its exact title) — anything whose own
    stem equals the seed's. A differently-titled spin-off/short (e.g. "Po",
    or "X: Secrets of the Scroll") won't share the stem, so it falls to the
    "other" bucket instead — see the module docstring for why that's an
    acceptable soft mis-prioritization, not wrong data: TMDB scopes
    collections narrowly (one small franchise, not one giant shared-universe
    collection), so the worst case is a handful of titles in a single
    collection landing in the wrong half of the order, not a sprawling mess."""
    return _stem(title) == seed_stem


def order_collection_parts(parts: list[dict], *, seed_title: str) -> list[dict]:
    """Sorts a TMDB collection's `parts` so that direct sequels/prequels of
    `seed_title` sort first, each bucket internally ordered by release date
    (ascending; missing dates sort last within their bucket, not first).
    Spin-offs, shorts and anything else whose title doesn't share the seed's
    stem sort after. Does not distinguish "sequel" from "prequel" — the
    product requirement only asks for that pair ahead of spin-offs/shorts as
    a group, not sequel-vs-prequel labeling."""
    seed_stem = _stem(seed_title)

    def sort_key(p: dict):
        is_direct = _is_direct_sequel_or_prequel(p.get("title") or "", seed_stem)
        return (0 if is_direct else 1, p.get("release_date") or "9999-99-99")

    return sorted(parts, key=sort_key)
