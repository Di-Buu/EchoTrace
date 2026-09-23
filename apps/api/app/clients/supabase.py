from typing import Any

import httpx

from app.config import Settings


class SupabaseError(RuntimeError):
    pass


class SupabaseClient:
    """Small PostgREST adapter that always executes under the user's JWT and RLS."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.http = httpx.AsyncClient()

    async def aclose(self) -> None:
        await self.http.aclose()

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
        response = await self.http.get(
            self.settings.supabase_auth_user_url,
            headers=self._headers(access_token),
            timeout=15,
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
        response = await self.http.get(
            f"{self.settings.supabase_rest_url}/{table}",
            params=params or {},
            headers=self._headers(access_token),
            timeout=20,
        )
        data = self._json_or_raise(response)
        return data if isinstance(data, list) else [data]

    async def insert(
        self, table: str, access_token: str, payload: dict[str, Any] | list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        self._ensure_configured()
        response = await self.http.post(
            f"{self.settings.supabase_rest_url}/{table}",
            json=payload,
            headers=self._headers(access_token, representation=True),
            timeout=20,
        )
        data = self._json_or_raise(response)
        return data if isinstance(data, list) else [data]

    async def upsert(
        self,
        table: str,
        access_token: str,
        payload: dict[str, Any] | list[dict[str, Any]],
        *,
        on_conflict: str,
    ) -> list[dict[str, Any]]:
        self._ensure_configured()
        headers = self._headers(access_token, representation=True)
        headers["Prefer"] = "return=representation,resolution=merge-duplicates"
        response = await self.http.post(
            f"{self.settings.supabase_rest_url}/{table}",
            params={"on_conflict": on_conflict},
            json=payload,
            headers=headers,
            timeout=20,
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
        response = await self.http.patch(
            f"{self.settings.supabase_rest_url}/{table}",
            params=params,
            json=payload,
            headers=self._headers(access_token, representation=True),
            timeout=20,
        )
        data = self._json_or_raise(response)
        return data if isinstance(data, list) else [data]

    async def delete(self, table: str, access_token: str, *, params: dict[str, Any]) -> list[dict[str, Any]]:
        self._ensure_configured()
        headers = self._headers(access_token, representation=True)
        response = await self.http.delete(
            f"{self.settings.supabase_rest_url}/{table}", params=params, headers=headers, timeout=20
        )
        data = self._json_or_raise(response)
        return data if isinstance(data, list) else [data]

    async def rpc(self, function: str, access_token: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        self._ensure_configured()
        response = await self.http.post(
            f"{self.settings.supabase_rest_url}/rpc/{function}",
            json=payload,
            headers=self._headers(access_token),
            timeout=30,
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
