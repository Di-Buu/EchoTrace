from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain import UserContext, WeeklyDigestOutput
from app.services.moment_index import split_moment
from app.services.weekly import WeeklyReportService, last_closed_week

USER_ID = UUID("11111111-1111-1111-1111-111111111111")
REPORT_ID = UUID("22222222-2222-2222-2222-222222222222")
MOMENT_ID = UUID("33333333-3333-3333-3333-333333333333")
USER = UserContext(id=USER_ID, access_token="test-token")


def test_last_closed_week_uses_beijing_monday_boundary() -> None:
    assert last_closed_week(datetime(2026, 9, 20, 15, 59, tzinfo=UTC)).isoformat() == "2026-09-07"
    assert last_closed_week(datetime(2026, 9, 20, 16, 0, tzinfo=UTC)).isoformat() == "2026-09-14"


def test_long_moment_chunking_preserves_all_text() -> None:
    raw = "一" * 2400 + "二" * 2600
    chunks = split_moment(raw)
    assert all(len(chunk) <= 2400 for chunk in chunks)
    assert chunks[0] == raw[:2400]
    assert chunks[1][120:] == raw[2400:4680]
    assert chunks[-1].endswith(raw[-120:])


class NoRecordDb:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def select(self, table: str, access_token: str, *, params: dict) -> list[dict]:
        assert access_token == USER.access_token
        assert params["user_id"] == f"eq.{USER_ID}"
        if table == "weekly_reports":
            return [{"id": str(REPORT_ID), "week_start": "2026-09-14", "week_end": "2026-09-21"}]
        if table == "moments":
            return []
        raise AssertionError(table)

    async def update(self, table: str, access_token: str, payload: dict, *, params: dict) -> list[dict]:
        assert table == "weekly_reports"
        self.updates.append(payload)
        return [payload]


@pytest.mark.asyncio
async def test_empty_week_finishes_without_any_ai_call() -> None:
    db = NoRecordDb()
    service = WeeklyReportService(db, None, None, None, None)  # type: ignore[arg-type]
    await service._process(USER, REPORT_ID)
    assert db.updates == [
        {
            "status": "no_records",
            "digest": [],
            "input_version": db.updates[0]["input_version"],
            "insight_id": None,
            "error_code": None,
        }
    ]


@pytest.mark.asyncio
async def test_one_moment_week_keeps_original_without_model_calls(monkeypatch) -> None:
    class OneMomentDb(NoRecordDb):
        async def select(self, table: str, access_token: str, *, params: dict) -> list[dict]:
            if table == "moments":
                if params["select"] == "id":
                    return [{"id": str(MOMENT_ID)}]
                return [
                    {
                        "id": str(MOMENT_ID),
                        "content": "这周有点累。",
                        "created_at": "2026-09-17T10:00:00+00:00",
                        "updated_at": "2026-09-17T10:00:00+00:00",
                    }
                ]
            return await super().select(table, access_token, params=params)

        async def delete(self, *args, **kwargs) -> list[dict]:
            return []

    async def all_eligible(db, user, ids):
        return ids

    monkeypatch.setattr("app.services.weekly.eligible_moment_ids", all_eligible)
    db = OneMomentDb()
    service = WeeklyReportService(db, None, None, None, None)  # type: ignore[arg-type]
    from app.config import Settings

    service.settings = Settings(_env_file=None)
    await service._process(USER, REPORT_ID)
    assert db.updates[0]["status"] == "insufficient"
    assert db.updates[0]["digest"][0]["summary"] == "这周有点累。"


class DigestAi:
    async def structured_chat(self, **kwargs):
        # An unsupported card must not be persisted; the original record remains.
        return WeeklyDigestOutput.model_validate(
            {
                "cards": [
                    {
                        "topic": "无效推断",
                        "summary": "没有来源",
                        "source_moment_ids": ["44444444-4444-4444-4444-444444444444"],
                    }
                ],
                "ungrouped_moment_ids": [str(MOMENT_ID)],
            }
        ), {"model": "fake", "usage": {}}


class DigestDb:
    async def insert(self, *args, **kwargs):
        return []


@pytest.mark.asyncio
async def test_digest_falls_back_to_original_when_model_gives_no_valid_card(monkeypatch) -> None:
    async def no_telemetry(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.weekly.Telemetry.ai_run", no_telemetry)
    service = WeeklyReportService(DigestDb(), DigestAi(), None, None, None)  # type: ignore[arg-type]
    cards = await service._digest(
        USER,
        [
            {
                "id": str(MOMENT_ID),
                "content": "今天心情很低落，但这只是今天发生的事。",
                "created_at": "2026-09-17T10:00:00+00:00",
            }
        ],
    )
    assert len(cards) == 1
    assert cards[0]["source_moment_ids"] == [str(MOMENT_ID)]
    assert cards[0]["summary"].startswith("今天心情很低落")
