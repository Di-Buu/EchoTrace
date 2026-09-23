from app.clients.supabase import SupabaseClient
from app.domain import UserContext


async def eligible_moment_ids(
    db: SupabaseClient, user: UserContext, moment_ids: set[str]
) -> set[str]:
    """Apply the user's current opt-out and legacy Memory deletion choices."""
    if not moment_ids:
        return set()
    eligible: set[str] = set()
    ordered = sorted(moment_ids)
    for index in range(0, len(ordered), 50):
        batch = ordered[index : index + 50]
        rows = await db.select(
            "moments",
            user.access_token,
            params={
                "select": "id",
                "id": f"in.({','.join(batch)})",
                "user_id": f"eq.{user.id}",
                "memory_enabled": "eq.true",
            },
        )
        eligible.update(str(row["id"]) for row in rows)
    if not eligible:
        return set()
    sources: list[dict] = []
    ordered = sorted(eligible)
    for index in range(0, len(ordered), 50):
        batch = ordered[index : index + 50]
        sources.extend(
            await db.select(
                "memory_sources",
                user.access_token,
                params={
                    "select": "memory_id,moment_id",
                    "moment_id": f"in.({','.join(batch)})",
                    "user_id": f"eq.{user.id}",
                },
            )
        )
    memory_ids = sorted({str(source["memory_id"]) for source in sources})
    deleted: set[str] = set()
    for index in range(0, len(memory_ids), 50):
        batch = memory_ids[index : index + 50]
        rows = await db.select(
            "memories",
            user.access_token,
            params={
                "select": "id",
                "id": f"in.({','.join(batch)})",
                "user_id": f"eq.{user.id}",
                "status": "eq.deleted",
            },
        )
        deleted.update(str(row["id"]) for row in rows)
    excluded = {
        str(source["moment_id"])
        for source in sources
        if str(source["memory_id"]) in deleted
    }
    return eligible - excluded
