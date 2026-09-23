from uuid import UUID

import pytest

from app.domain import UserContext
from app.routers.account import delete_personal_data


class RecordingDb:
    def __init__(self) -> None:
        self.tables: list[str] = []

    async def delete(self, table: str, access_token: str, *, params: dict) -> list[dict]:
        assert access_token == "token"
        assert params == {"user_id": "eq.11111111-1111-1111-1111-111111111111"}
        self.tables.append(table)
        return [{"id": table}]


@pytest.mark.asyncio
async def test_delete_personal_data_uses_current_user_scope_and_dependency_order() -> None:
    db = RecordingDb()
    user = UserContext(
        id=UUID("11111111-1111-1111-1111-111111111111"),
        access_token="token",
    )

    result = await delete_personal_data(user, db)  # type: ignore[arg-type]

    assert result["ok"] is True
    assert db.tables[-4:] == ["threads", "memories", "moment_index_chunks", "moments"]
    assert db.tables.index("weekly_summary_cards") < db.tables.index("weekly_reports")
    assert db.tables.index("weekly_reports") < db.tables.index("insights")
    assert all(count == 1 for count in result["deleted"].values())
