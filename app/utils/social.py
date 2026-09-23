def filter_friend_ids(supabase, user_id: str, friend_ids: list[str]) -> set[str]:
    """Restrict a client-supplied list of user IDs to ones the caller is actually
    friends with, so recommendation/review fan-out can't be used to spam or probe
    arbitrary users."""
    if not friend_ids:
        return set()
    result = (
        supabase.table("friendships")
        .select("friend_id")
        .eq("user_id", user_id)
        .in_("friend_id", friend_ids)
        .execute()
    )
    return {r["friend_id"] for r in result.data}


def filter_owned_group_ids(supabase, user_id: str, group_ids: list[str]) -> set[str]:
    """Restrict a client-supplied list of group IDs to ones the caller owns —
    matches the ownership check already used by every other friend_groups endpoint."""
    if not group_ids:
        return set()
    result = (
        supabase.table("friend_groups")
        .select("id")
        .eq("owner_id", user_id)
        .in_("id", group_ids)
        .execute()
    )
    return {r["id"] for r in result.data}


# What a visitor may see of someone else's profile. Deliberately excludes the
# private half of the row (mute flags, onboarding state, has_onboarded).
PUBLIC_PROFILE_COLUMNS = (
    "id, username, bio, avatar_url, avatar_color, avatar_focal_y, avatar_zoom, "
    "profile_visibility, hide_friends_list"
)
LEGACY_PUBLIC_PROFILE_COLUMNS = (
    "id, username, bio, avatar_url, avatar_color, avatar_focal_y, avatar_zoom, profile_visibility"
)

DEFAULT_VISIBILITY = "friends_only"


def are_friends(supabase, user_id: str, other_id: str) -> bool:
    """Friendships are stored as two directed rows written together, so one
    direction is enough to answer this."""
    if str(user_id) == str(other_id):
        return True
    result = (
        supabase.table("friendships")
        .select("id")
        .eq("user_id", str(user_id))
        .eq("friend_id", str(other_id))
        .limit(1)
        .execute()
    )
    return bool(result.data)


def load_viewable_profile(supabase, viewer_id: str, target_id: str) -> tuple[dict | None, str]:
    """The target's public profile row, if this viewer is allowed to see it.

    Returns (row, "ok") or (None, "not_found" | "private"). This is the single
    place profile_visibility is enforced — every public read (the profile
    header, its friends list, its stats) goes through it, so the rule can't
    drift between them. Your own profile is always viewable.
    """
    try:
        result = supabase.table("profiles").select(PUBLIC_PROFILE_COLUMNS).eq("id", str(target_id)).limit(1).execute()
    except Exception as exc:
        # sql/009 not applied yet: fall back rather than break the page, and
        # treat the friends list as visible (its pre-setting behaviour).
        if "hide_friends_list" not in str(exc):
            raise
        result = supabase.table("profiles").select(LEGACY_PUBLIC_PROFILE_COLUMNS).eq("id", str(target_id)).limit(1).execute()

    if not result.data:
        return None, "not_found"
    row = result.data[0]

    if str(target_id) == str(viewer_id):
        return row, "ok"

    visibility = row.get("profile_visibility") or DEFAULT_VISIBILITY
    if visibility == "everyone":
        return row, "ok"
    if visibility == DEFAULT_VISIBILITY and are_friends(supabase, viewer_id, target_id):
        return row, "ok"
    return None, "private"


def load_friends_of(supabase, user_id: str) -> list[dict]:
    """The user's friends, in the shape the client's FriendProfile expects —
    the same embed GET /api/friends/ uses."""
    result = (
        supabase.table("friendships")
        .select("friend_id, profiles!friendships_friend_id_fkey(id, username, avatar_url, avatar_color, avatar_focal_y, avatar_zoom)")
        .eq("user_id", str(user_id))
        .order("created_at")
        .execute()
    )
    return [
        {
            "id": r["profiles"]["id"],
            "username": r["profiles"].get("username"),
            "avatar_url": r["profiles"].get("avatar_url"),
            "avatar_color": r["profiles"].get("avatar_color"),
            "avatar_focal_y": r["profiles"].get("avatar_focal_y"),
            "avatar_zoom": r["profiles"].get("avatar_zoom"),
        }
        for r in result.data
        if r.get("profiles")
    ]
