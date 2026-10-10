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


# ─── Fallback for a franchise TMDB never collected ──────────────────────────
# Not every real franchise has a TMDB "collection" object — e.g. the Tom
# Holland Spider-Man trilogy (Homecoming / Far From Home / No Way Home) isn't
# grouped into one. When a movie has no collection_id at all, movie_cache's
# get_movie_collection falls back to TMDB's own /recommendations for that
# film (already a reasonable "people who liked this also liked" signal, and
# in practice does surface real sequels alongside unrelated noise), filtered
# down to plausible franchise siblings by title.
_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def franchise_display_name(title: str) -> str:
    """The human-readable franchise name for the fallback path's synthetic
    "collection" — same trimming as _franchise_prefix (colon-subtitle,
    leading article, trailing numbering stripped) but case-preserved, since
    this is shown to the user (e.g. "Spider-Man", not "spider-man")."""
    base = (title or "").split(":")[0].strip()
    base = _LEADING_ARTICLE_RE.sub("", base)
    return _TRAILING_NUMERAL_RE.sub("", base).strip()


def _franchise_prefix(title: str) -> str:
    """A looser cousin of _stem, used only for this fallback. Unlike _stem,
    this strips ANY colon-subtitle (not just a numbering one) and a leading
    article — "Spider-Man: Homecoming", "Spider-Man: Far From Home" and
    "Spider-Man: No Way Home" all reduce to "spider-man". That looseness
    would be wrong inside an *official* collection's own parts list (it's
    exactly what over-matched shorts/spin-offs as "direct sequels" when
    first tried in order_collection_parts/_stem above) — but here the
    candidate pool is already TMDB's own curated recommendations for one
    specific film, not "every movie with this word in the title", so a
    false positive is rare and far cheaper than the alternative of finding
    nothing at all."""
    return franchise_display_name(title).lower()


def _normalized_title(title: str) -> str:
    """Lowercased, leading-article-stripped only — unlike _franchise_prefix,
    deliberately does NOT strip a trailing word/colon-subtitle, since
    _is_franchise_sibling below needs the full title to test one against
    the other as a prefix."""
    return _LEADING_ARTICLE_RE.sub("", (title or "").strip()).lower()


def _is_title_prefix_of(shorter: str, longer: str) -> bool:
    """True if `shorter` is `longer` up to a word boundary — "dark knight"
    is a prefix of "dark knight rises", but not of "dark knightmare"."""
    if not longer.startswith(shorter):
        return False
    rest = longer[len(shorter):]
    return rest == "" or rest.startswith(" ")


_MIN_PREFIX_LEN = 4  # a word this short alone is too generic to trust as a franchise signal


def _is_franchise_sibling(title: str, seed_title: str, seed_prefix: str) -> bool:
    if _franchise_prefix(title) == seed_prefix:
        return True
    # Catches a sequel named by just appending a plain word, with no
    # number and no colon at all — "The Dark Knight" -> "The Dark Knight
    # Rises" — which _franchise_prefix's own numbering/colon stripping has
    # nothing to grab onto. Checked as a whole-word prefix either
    # direction, so "Dark Knight"/"Dark Knight Rises" matches but "Dark
    # Knight"/"Dark Knightmare" doesn't. Same reasoning as _franchise_prefix's
    # own docstring for why looseness is fine here: this is still only
    # run against one film's own TMDB-recommended pool, not a catalog scan.
    norm_title, norm_seed = _normalized_title(title), _normalized_title(seed_title)
    if len(norm_seed) < _MIN_PREFIX_LEN or len(norm_title) < _MIN_PREFIX_LEN:
        return False
    return _is_title_prefix_of(norm_seed, norm_title) or _is_title_prefix_of(norm_title, norm_seed)


def filter_by_franchise_prefix(candidates: list[dict], *, seed_title: str) -> list[dict]:
    """From a film's TMDB-recommended movies, keep only the ones that share
    the seed's franchise prefix. Returns [] (not an error) when the seed
    title itself reduces to nothing usable, or when nothing matches —
    the overwhelmingly common case for a genuinely standalone film."""
    seed_prefix = _franchise_prefix(seed_title)
    if not seed_prefix:
        return []
    return [c for c in candidates if _is_franchise_sibling(c.get("title") or "", seed_title, seed_prefix)]


def order_by_release_date(parts: list[dict]) -> list[dict]:
    """Chronological order, missing dates sorted last rather than first —
    same convention as order_collection_parts. Used for the fallback path
    above, where every surviving candidate already passed the franchise
    filter, so there's no separate "direct vs. other" bucket to sort by."""
    return sorted(parts, key=lambda p: p.get("release_date") or "9999-99-99")
