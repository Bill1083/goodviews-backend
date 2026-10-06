from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_tz(raw: str | None) -> ZoneInfo | None:
    """An IANA zone name from a query string (?tz=Australia/Sydney), or UTC
    when absent. None means it was present but not a real zone — callers
    answer that with a 400."""
    name = (raw or "UTC").strip()
    if not name or len(name) > 64:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
