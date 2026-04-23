import bleach


ALLOWED_TAGS: list[str] = []  # Strip all HTML tags from review text
ALLOWED_ATTRIBUTES: dict = {}


def sanitize_text(value: str | None) -> str:
    if not value:
        return ""
    cleaned = bleach.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        strip=True,
    )
    return cleaned.strip()


def sanitize_str(value: str | None, max_length: int = 255) -> str:
    if not value:
        return ""
    cleaned = bleach.clean(
        str(value),
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        strip=True,
    )
    return cleaned.strip()[:max_length]
