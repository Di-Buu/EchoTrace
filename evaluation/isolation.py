"""Five real A/B account checks for cross-user data leakage.

Only dedicated evaluation accounts are allowed. This module is imported by the
core runner; it does not run on import and never removes Auth users.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5


class IsolationError(RuntimeError):
    pass

PAIRS = (
    ("isolation_reading", "我在整理蓝色海鸥读书笔记。", "蓝色海鸥", "我在练习绿色山茶烘焙。", "烘焙"),
    ("isolation_sport", "我最近参加紫色灯塔攀岩。", "紫色灯塔", "我最近练习橙色纸船游泳。", "游泳"),
    ("isolation_city", "我想去银色松果杭州生活。", "银色松果", "我想去金色雨滴成都生活。", "成都"),
    ("isolation_work", "我正在考虑青色风铃插画工作。", "青色风铃", "我正在考虑红色星河园艺工作。", "园艺"),
    ("isolation_hobby", "我喜欢黑色珊瑚陶艺。", "黑色珊瑚", "我喜欢白色月桂跑步。", "跑步"),
)


def second_account_values(values: dict[str, str]) -> dict[str, str]:
    email = values.get("ECHOTRACE_EVAL_B_EMAIL", "")
    password = values.get("ECHOTRACE_EVAL_B_PASSWORD", "")
    if not email or not password:
        raise IsolationError("核心评测需要第二个专用测试账号：ECHOTRACE_EVAL_B_EMAIL / PASSWORD；尚未清理任何数据")
    return {
        **values,
        "ECHOTRACE_EVAL_EMAIL": email,
        "ECHOTRACE_EVAL_PASSWORD": password,
    }


async def prepare_pair(a: Any, b: Any) -> None:
    """Authenticate both users before any destructive test-data reset."""
    await a.login()
    await b.login()
    if a.user_id == b.user_id or a.email.casefold() == b.email.casefold():
        raise IsolationError("A/B 隔离测试必须使用两个不同的专用测试账号；尚未清理任何数据")


def contains_foreign_data(payload: Any, foreign_ids: set[str], foreign_marker: str) -> bool:
    raw = json.dumps(payload, ensure_ascii=False)
    return foreign_marker in raw or any(value in raw for value in foreign_ids)


def rest_headers(client: Any) -> dict[str, str]:
    return {
        "apikey": client.anon_key,
        "Authorization": f"Bearer {client.token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


async def insert_row(client: Any, table: str, payload: dict[str, Any]) -> None:
    response = await client.http.post(
        f"{client.supabase_url}/rest/v1/{table}",
        headers=rest_headers(client),
        json=payload,
    )
    if response.status_code >= 400:
        raise IsolationError(f"初始化 A 的 {table} 失败 ({response.status_code})")


async def foreign_row_hidden(client: Any, table: str, row_id: str) -> bool:
    response = await client.http.get(
        f"{client.supabase_url}/rest/v1/{table}",
        params={"select": "*", "id": f"eq.{row_id}"},
        headers=rest_headers(client),
    )
    if response.status_code != 200:
        raise IsolationError(f"B 查询 {table} 失败 ({response.status_code})；本轮不能判定泄漏率")
    return response.json() == []


async def seed_owned_relations(a: Any, case_id: str, moment_id: str, text: str) -> dict[str, str]:
    """Create real A-owned Thread, Memory, Insight and evidence rows without LLM calls."""
    ids = {
        table: str(uuid5(NAMESPACE_URL, f"echotrace:{case_id}:a:{table}"))
        for table in ("threads", "memories", "insights")
    }
    await insert_row(a, "threads", {
        "id": ids["threads"], "user_id": a.user_id, "source_moment_id": moment_id,
        "summary": text,
    })
    await insert_row(a, "thread_messages", {
        "user_id": a.user_id, "thread_id": ids["threads"],
        "role": "user", "content": text, "input_type": "text",
    })
    await insert_row(a, "memories", {
        "id": ids["memories"], "user_id": a.user_id, "memory_type": "interest",
        "content": text, "confidence": 1.0,
    })
    await insert_row(a, "memory_sources", {
        "user_id": a.user_id, "memory_id": ids["memories"], "moment_id": moment_id,
    })
    await insert_row(a, "insights", {
        "id": ids["insights"], "user_id": a.user_id, "insight_type": "fact",
        "trigger_type": "user_query", "title": text, "body": text,
        "verification_status": "PASS", "evidence_version": "isolation-test",
        "agent_version": "isolation-test",
    })
    await insert_row(a, "insight_evidence", {
        "user_id": a.user_id, "insight_id": ids["insights"],
        "moment_id": moment_id, "memory_id": ids["memories"], "stance": "support",
    })
    return ids


async def run_isolation_pairs(a: Any, b: Any) -> list[dict[str, Any]]:
    """Check five separate A/B histories across records, retrieval, Companion and Insight."""
    results: list[dict[str, Any]] = []
    for index, (case_id, a_text, a_marker, b_text, b_topic) in enumerate(PAIRS):
        await a.clear_data()
        await b.clear_data()
        a_ids = await a.seed_history(case_id + "-a", [{"text": a_text, "at": "2026-08-01T12:00:00Z"}])
        b_ids = await b.seed_history(case_id + "-b", [{"text": b_text, "at": "2026-08-02T12:00:00Z"}])
        for client, moment_id in ((a, a_ids[0]), (b, b_ids[0])):
            status, payload = await client.api("POST", f"/moments/{moment_id}/process")
            if status != 200:
                raise IsolationError(f"{case_id} 原文索引失败 ({status}): {json.dumps(payload, ensure_ascii=False)[:200]}")

        owned = await seed_owned_relations(a, case_id, a_ids[0], a_text)
        table_checks = {
            table: await foreign_row_hidden(b, table, owned[table])
            for table in ("threads", "memories", "insights")
        }

        status_list, moments = await b.api("GET", "/moments")
        status_detail, _detail = await b.api("GET", f"/moments/{a_ids[0]}")
        status_thread, _thread = await b.api("GET", f"/threads/{owned['threads']}/messages")
        status_insight, _insight = await b.api("GET", f"/insights/{owned['insights']}")
        if index in (0, 2, 4):
            workflow = "companion"
            status_ai, output = await b.api("POST", "/chat", {
                "content": f"我之前聊过{b_topic}吗？", "thread_id": None,
                "source_moment_id": None, "input_type": "text",
            })
        else:
            workflow = "insight"
            status_ai, output = await b.api("POST", "/insights/query", {
                "question": f"从我以前的记录看，关于{b_topic}能说什么？"
            })
        traces = await b.retrieval_trace()
        if (
            status_list != 200 or status_detail not in {200, 404}
            or status_thread not in {200, 404} or status_insight not in {200, 404}
            or status_ai not in {200, 422} or any(item.get("error") for item in traces)
        ):
            raise IsolationError(f"{case_id} 接口/遥测异常；本轮不能判定泄漏率")
        if b_ids[0] not in json.dumps(moments, ensure_ascii=False) or b_ids[0] not in json.dumps(traces):
            raise IsolationError(f"{case_id} 未证明 B 自有记录可见且进入检索；不能以空结果宣称隔离成功")
        foreign_ids = set(a_ids)
        leak_checks = {
            "moment_list": not contains_foreign_data(moments, foreign_ids, a_marker),
            "moment_detail_denied": status_detail == 404,
            "thread_detail_denied": status_thread == 404,
            "insight_detail_denied": status_insight == 404,
            "a_owned_tables_hidden": all(table_checks.values()),
            "generated_output": not contains_foreign_data(output, foreign_ids, a_marker),
            "retrieval_top_k": not contains_foreign_data(traces, foreign_ids, a_marker),
        }
        results.append({
            "case_id": case_id,
            "workflow": workflow,
            "status": "pass" if all(leak_checks.values()) else "fail",
            "checks": leak_checks,
            "http_status": {
                "moments": status_list, "foreign_detail": status_detail,
                "thread_detail": status_thread, "insight_detail": status_insight,
                "workflow": status_ai,
            },
            "a_moment_ids": a_ids,
            "b_moment_ids": b_ids,
            "a_owned_relation_ids": owned,
            "b_retrieval_trace": traces,
        })
    return results
