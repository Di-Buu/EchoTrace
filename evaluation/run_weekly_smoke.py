"""Minimal real local smoke for the weekly report workflow.

It uses the dedicated evaluation account, creates two Moments in the last
closed Beijing week, validates the report and evidence links, then clears the
account's product data. It is not a formal product evaluation.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
API_ROOT = PROJECT_ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT))
sys.path.insert(0, str(ROOT))

from run_eval import EvaluationError, ProductClient, load_environment  # noqa: E402

from app.services.weekly import REPORT_ZONE, last_closed_week  # noqa: E402


async def main() -> int:
    values = load_environment()
    client = ProductClient(values, "http://127.0.0.1:8000", timeout=240)
    try:
        await client.login()
        await client.clear_data()
        status, saved = await client.api(
            "POST",
            "/moments",
            {
                "content": "后台索引冒烟：今晚准备在晚饭后阅读二十分钟。",
                "mode": "capture",
                "input_type": "text",
                "memory_enabled": True,
            },
        )
        if status != 200:
            raise EvaluationError(f"保存 Moment 失败 ({status}): {client._safe(saved)}")
        indexed = []
        for _ in range(30):
            response = await client.http.get(
                f"{client.supabase_url}/rest/v1/moment_index_chunks",
                params={"select": "id", "moment_id": f"eq.{saved['id']}"},
                headers={
                    "apikey": client.anon_key,
                    "Authorization": f"Bearer {client.token}",
                },
            )
            indexed = client._decode(response)
            if response.status_code == 200 and indexed:
                break
            await asyncio.sleep(2)
        if not indexed:
            raise EvaluationError("保存后的后台原文索引在 60 秒内没有完成")
        await client.clear_data()

        start = last_closed_week()
        history = [
            {
                "at": datetime.combine(start + timedelta(days=1), time(20), REPORT_ZONE).isoformat(),
                "text": "这周第一次在晚饭后读了二十分钟，开始时还是很想刷手机。",
            },
            {
                "at": datetime.combine(start + timedelta(days=4), time(20), REPORT_ZONE).isoformat(),
                "text": "今晚又在晚饭后读了二十分钟，手机放远以后更容易继续。",
            },
        ]
        moment_ids = await client.seed_history("weekly-smoke-v1", history)
        for moment_id in moment_ids:
            status, payload = await client.api("POST", f"/moments/{moment_id}/process")
            if status != 200:
                raise EvaluationError(f"原文索引失败 ({status}): {client._safe(payload)}")
        status, ensured = await client.api("POST", "/weekly-reports/ensure")
        if status != 200 or ensured.get("scheduled", 0) < 1:
            raise EvaluationError(f"周报没有进入后台任务: {client._safe(ensured)}")

        reports = []
        for _ in range(80):
            status, reports = await client.api("GET", "/weekly-reports")
            if status != 200:
                raise EvaluationError(f"读取周报失败 ({status}): {client._safe(reports)}")
            target = next((item for item in reports if item["week_start"] == start.isoformat()), None)
            if target and target["status"] != "processing":
                break
            await asyncio.sleep(2)
        else:
            raise EvaluationError("周报在 160 秒内没有完成")

        if target["status"] not in {"insufficient", "completed"}:
            raise EvaluationError(f"周报状态异常: {target['status']}")
        source_ids = {
            str(source_id)
            for card in target.get("digest", [])
            for source_id in card.get("source_moment_ids", [])
        }
        if not set(moment_ids).issubset(source_ids):
            raise EvaluationError("周摘要没有回溯到全部测试 Moment")
        print(
            {
                "status": "pass",
                "week_start": start.isoformat(),
                "report_status": target["status"],
                "moments": len(moment_ids),
                "cards": len(target.get("digest", [])),
                "evidence_sources": len(source_ids),
            }
        )
        return 0
    finally:
        if client.token:
            await client.clear_data()
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
