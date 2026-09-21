"""Small PostgREST helpers shared by services that read whole per-user tables.
Deliberately free of Flask so the stats aggregation stays unit-testable."""
from collections.abc import Callable, Iterable, Iterator
from typing import Any, TypeVar

T = TypeVar("T")

# Supabase's default PostgREST max-rows: a single request never returns more
# than this, silently — so anything that wants "all rows" has to page.
PAGE_SIZE = 1000

# .in_() filters are encoded into the request URL; 200 integer ids is ~1.5 KB,
# comfortably under the ~8 KB where proxies start rejecting URLs.
IN_CHUNK = 200


def paginate(build: Callable[[], Any], page_size: int = PAGE_SIZE) -> list[dict]:
    """Fetches every row of a query in `page_size` chunks via .range(). `build`
    must return a FRESH query builder on each call — supabase-py builders are
    mutable, so reusing one would stack the .range() filters. Callers should
    include an .order() in the builder so pages don't overlap."""
    rows: list[dict] = []
    offset = 0
    while True:
        result = build().range(offset, offset + page_size - 1).execute()
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            return rows
        offset += page_size


def chunked(items: Iterable[T], size: int = IN_CHUNK) -> Iterator[list[T]]:
    """Yields consecutive lists of at most `size` items."""
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
