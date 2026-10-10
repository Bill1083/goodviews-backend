"""A random.Random seeded deterministically from (user, day, salt).

Stable across processes/reloads — random.Random hashes a str seed with
SHA-512, unlike hash(), which Python salts per-process — so the same user
reloading the same page on the same day sees the same random slice, not a
reshuffle on every request. Distinct per user (and per call-site, via
`salt`), so two users whose inputs otherwise collide (e.g. the same top-3
genre ranking) still diverge.

Shared by daily_picks (Movies of the Day), streaming_worlds (Streaming
Worlds' For You/Different), and recommendations (For You's cold-start
genre-fallback candidates) — one implementation, not three.
"""
from __future__ import annotations

import random
from datetime import date


def seeded_rng(user_id: str, day: date, salt: str) -> random.Random:
    return random.Random(f"{user_id}:{day.isoformat()}:{salt}")


def weighted_sample(scored_items: list[tuple[float, object]], n: int, rng: random.Random) -> list:
    """n items from `scored_items` (weight, item) pairs, without replacement,
    biased toward higher weight via the Efraimidis-Spirakis trick: each
    item's key is u ** (1/weight), highest keys win. Keys are drawn for
    every item up front, so one item's chances don't shift as others are
    picked. Non-positive weights are floored to a tiny epsilon rather than
    excluded, so a zero-scored item can still (rarely) surface."""
    keyed = [(rng.random() ** (1 / max(weight, 1e-6)), item) for weight, item in scored_items]
    keyed.sort(key=lambda t: t[0], reverse=True)
    return [item for _, item in keyed[:n]]
