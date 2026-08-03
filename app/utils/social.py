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
