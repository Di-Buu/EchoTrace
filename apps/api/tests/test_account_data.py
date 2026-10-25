from uuid import UUID

import pytest

from app.domain import UserContext
from app.routers.account import delete_personal_data


class RecordingDb:
    async def rpc(self, function, access_token, payload):
        assert function == "clear_personal_data" and access_token == "token" and payload == {}
        return [{"ok": True, "deleted": {"moments": 2}}]

    async def delete(self, *_args, **_kwargs):
        raise AssertionError("清空必须一次数据库事务，不能逐表调用 HTTP 删除")


@pytest.mark.asyncio
async def test_delete_personal_data_uses_single_atomic_rpc() -> None:
    user = UserContext(id=UUID("11111111-1111-1111-1111-111111111111"), access_token="token")
    result = await delete_personal_data(user, RecordingDb())
    assert result == {"ok": True, "deleted": {"moments": 2}}
