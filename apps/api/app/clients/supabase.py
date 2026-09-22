from typing import Any

import httpx

from app.config import Settings


class SupabaseError(RuntimeError):
    pass


class SupabaseClient:
    """Small PostgREST adapter that always executes under the user's JWT and RLS."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _headers(self, access_token: str, *, representation: bool = False) -> dict[str, str]:
        headers = {
            "apikey": self.settings.supabase_anon_key,
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        if representation:
            headers["Prefer"] = "return=representation"
        return headers

    async def resolve_user(self, access_token: str) -> dict[str, Any]:
        self._ensure_configured()
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(
                self.settings.supabase_auth_user_url,
                headers=self._headers(access_token),
            )
        return self._json_or_raise(response)

    async def select(
        self,
        table: str,
        access_token: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_configured()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{self.settings.supabase_rest_url}/{table}",
                params=params or {},
                headers=self._headers(access_token),
            )
        data = self._json_or_raise(response)
        return data if isinstance(data, list) else [data]

    async def insert(
        self, table: str, access_token: str, payload: dict[str, Any] | list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        self._ensure_configured()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{self.settings.supabase_rest_url}/{table}",
                json=payload,
                headers=self._headers(access_token, representation=True),
            )
        data = self._json_or_raise(response)
        return data if isinstance(data, list) else [data]

    async def update(
        self,
        table: str,
        access_token: str,
        payload: dict[str, Any],
        *,
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        self._ensure_configured()
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.patch(
                f"{self.settings.supabase_rest_url}/{table}",
                params=params,
                json=payload,
                headers=self._headers(access_token, representation=True),
            )
        data = self._json_or_raise(response)
        return data if isinstance(data, list) else [data]

    async def delete(self, table: str, access_token: str, *, params: dict[str, Any]) -> list[dict[str, Any]]:
        self._ensure_configured()
        headers = self._headers(access_token, representation=True)
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.delete(f"{self.settings.supabase_rest_url}/{table}", params=params, headers=headers)
        data = self._json_or_raise(response)
        return data if isinstance(data, list) else [data]

    async def rpc(self, function: str, access_token: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        self._ensure_configured()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.settings.supabase_rest_url}/rpc/{function}",
                json=payload,
                headers=self._headers(access_token),
            )
        data = self._json_or_raise(response)
        return data if isinstance(data, list) else [data]

    def _ensure_configured(self) -> None:
        if not self.settings.supabase_url or not self.settings.supabase_anon_key:
            raise SupabaseError("Supabase 尚未配置")

    @staticmethod
    def _json_or_raise(response: httpx.Response) -> Any:
        if response.is_error:
            detail = response.text[:1_000]
            raise SupabaseError(f"Supabase {response.status_code}: {detail}")
        if not response.content:
            return []
        return response.json()
