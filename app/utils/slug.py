import re
import unicodedata

_COMBINING_MARKS = re.compile(r"[̀-ͯ]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(title: str | None) -> str:
    """URL slug for a film title: "Léon: The Professional" → "leon-the-professional".

    The client builds film page URLs with the same rule (src/utils/filmUrl.ts),
    and the sitemap's URLs have to match the pages' canonical URLs exactly —
    change both together. Titles with no Latin letters give "" (the page URL
    is then just /film/{id}).
    """
    text = _COMBINING_MARKS.sub("", unicodedata.normalize("NFKD", title or "")).lower()
    text = _NON_ALNUM.sub("-", text.replace("&", " and ")).strip("-")
    return text[:80].rstrip("-")


def film_path(movie_id: int, title: str | None) -> str:
    slug = slugify(title)
    return f"/film/{movie_id}-{slug}" if slug else f"/film/{movie_id}"
