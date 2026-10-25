"""Synthetic-owner tests; safe in the marked local DB without resetting it.

The runner must pause the local Worker so synthetic index jobs never call models.
Only UUIDs created by this module are removed. No existing account is inspected.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import pytest

DSN = os.getenv("ECHO_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="需要明确隔离的本机数据库")


@contextmanager
def connection(name="product-reliability"):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    cfg = conninfo_to_dict(DSN)
    if (
        os.getenv("ECHO_TEST_DB_CONFIRM") != "isolated"
        or cfg.get("host") not in {"127.0.0.1", "localhost"}
        or str(cfg.get("port")) != "55322"
    ):
        raise RuntimeError("只允许明确确认的本机隔离数据库")
    with psycopg.connect(DSN, autocommit=True, application_name=name) as conn:
        yield conn


def call_on(conn, uid, function, *args):
    from psycopg import sql
    from psycopg.types.json import Jsonb

    conn.execute("set local role authenticated")
    conn.execute("select set_config('request.jwt.claims',%s,true)", (json.dumps({"sub": uid}),))
    args = tuple(Jsonb(x) if isinstance(x, dict) else x for x in args)
    query = sql.SQL("select public.{}({})").format(
        sql.Identifier(function), sql.SQL(",").join(sql.Placeholder() for _ in args)
    )
    return conn.execute(query, args).fetchone()[0]


def rpc(uid, function, *args):
    with connection() as conn, conn.transaction():
        return call_on(conn, uid, function, *args)


@pytest.fixture
def users():
    ids = [str(uuid4()), str(uuid4())]
    with connection() as conn:
        for uid in ids:
            conn.execute(
                "insert into auth.users(id,email) values(%s,%s)", (uid, f"product-test-{uid}@acceptance.local")
            )
    try:
        yield ids
    finally:
        with connection() as conn:
            for uid in ids:
                conn.execute("delete from auth.users where id=%s", (uid,))


def prepare(uid, key="one", **extra):
    return rpc(uid, "chat_prepare", key, {"content": "synthetic product test", **extra})


def finish(uid, prepared, key="one", **extra):
    return rpc(
        uid,
        "chat_control",
        key,
        prepared["lease"],
        "finish",
        {"content": "synthetic reply", "evidence_ids": [], **extra},
    )


def count(uid, table):
    from psycopg import sql

    with connection() as conn:
        return conn.execute(
            sql.SQL("select count(*) from public.{} where user_id=%s").format(sql.Identifier(table)), (uid,)
        ).fetchone()[0]


def test_parallel_duplicate_chat_has_one_saved_user_turn(users):
    import psycopg

    def attempt(_):
        try:
            return prepare(users[0])
        except psycopg.Error as exc:
            assert "CHAT_BUSY" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=6) as pool:
        result = [x for x in pool.map(attempt, range(12)) if x]
    assert len(result) == 1
    assert count(users[0], "moments") == count(users[0], "threads") == count(users[0], "thread_messages") == 1
    reply = finish(users[0], result[0])
    assert prepare(users[0])["cached"] == reply
    assert count(users[0], "thread_messages") == 2


def test_failed_or_expired_chat_resumes_without_duplicating_user_message(users):
    first = prepare(users[0])
    rpc(users[0], "chat_control", "one", first["lease"], "fail", {})
    second = prepare(users[0])
    assert first["moment_id"] == second["moment_id"] and first["lease"] != second["lease"]
    with connection() as conn:
        conn.execute(
            "update echotrace_private.chat_turns set lease_until=now()-interval '1s' where user_id=%s", (users[0],)
        )
    third = prepare(users[0])
    assert third["moment_id"] == first["moment_id"]
    assert count(users[0], "thread_messages") == 1
    finish(users[0], third)


def test_chat_key_conflict_and_other_user_thread_are_rejected(users):
    import psycopg

    p = prepare(users[0])
    with pytest.raises(psycopg.Error, match="IDEMPOTENCY_CONFLICT"):
        prepare(users[0], content="different synthetic content")
    with pytest.raises(psycopg.Error, match="SOURCE_UNAVAILABLE"):
        prepare(users[1], thread_id=p["thread_id"])
    with pytest.raises(psycopg.Error, match="CHAT_LEASE_LOST"):
        rpc(users[1], "chat_control", "one", p["lease"], "finish", {"content": "forged"})
    assert count(users[1], "moments") == 0


@pytest.mark.parametrize("lease", [None, "00000000-0000-0000-0000-000000000000"])
def test_missing_or_forged_lease_cannot_publish(users, lease):
    import psycopg

    prepare(users[0])
    with pytest.raises(psycopg.Error, match="CHAT_LEASE_LOST"):
        rpc(users[0], "chat_control", "one", lease, "finish", {"content": "forged"})
    assert count(users[0], "thread_messages") == 1


def test_foreign_or_changed_evidence_cannot_be_attached(users):
    import psycopg

    a, b = prepare(users[0]), prepare(users[1])
    with pytest.raises(psycopg.Error, match="EVIDENCE_CHANGED"):
        finish(users[0], a, evidence_ids=[b["moment_id"]], versions={b["moment_id"]: 1})
    with pytest.raises(psycopg.Error, match="EVIDENCE_CHANGED"):
        finish(users[0], a, evidence_ids=[a["moment_id"]], versions={a["moment_id"]: 99})
    assert count(users[0], "thread_messages") == 1


def test_editing_source_cancels_old_reply_and_replay(users):
    import psycopg

    p = prepare(users[0])
    with connection() as conn:
        conn.execute("update public.moments set content='synthetic correction' where id=%s", (p["moment_id"],))
    with pytest.raises(psycopg.Error, match="CHAT_LEASE_LOST"):
        finish(users[0], p)
    with pytest.raises(psycopg.Error, match="SOURCE_UNAVAILABLE"):
        prepare(users[0])


def test_clear_is_atomic_when_a_late_delete_fails(users):
    import psycopg
    from psycopg import sql

    p = prepare(users[0])
    name = "product_clear_guard_" + uuid4().hex
    with connection() as conn:
        conn.execute(
            sql.SQL("create table echotrace_private.{}(id uuid references public.moments(id))").format(
                sql.Identifier(name)
            )
        )
        try:
            conn.execute(
                sql.SQL("insert into echotrace_private.{} values(%s)").format(sql.Identifier(name)), (p["moment_id"],)
            )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                rpc(users[0], "clear_personal_data")
            assert count(users[0], "moments") == count(users[0], "thread_messages") == 1
            # Cancellation and lease updates must roll back with the failed deletion.
            assert (
                conn.execute(
                    "select status from echotrace_private.chat_turns where user_id=%s", (users[0],)
                ).fetchone()[0]
                == "running"
            )
            finish(users[0], p)
        finally:
            conn.execute(sql.SQL("drop table echotrace_private.{}").format(sql.Identifier(name)))


def test_single_delete_removes_linked_conversation_and_only_its_owner(users):
    root = prepare(users[0])
    finish(users[0], root)
    second = prepare(users[0], "two", thread_id=root["thread_id"])
    finish(users[0], second, "two")
    prepare(users[1])
    assert not rpc(users[1], "delete_moment_data", root["moment_id"])["deleted"]
    assert rpc(users[0], "delete_moment_data", second["moment_id"])["deleted"]
    assert count(users[0], "moments") == count(users[0], "threads") == count(users[0], "thread_messages") == 0
    assert count(users[1], "moments") == 1


def test_memory_permission_rpc_is_owned_and_reversible(users):
    p = prepare(users[0])
    finish(users[0], p)
    assert rpc(users[1], "set_moment_memory", p["moment_id"], False) == {}
    assert not rpc(users[0], "set_moment_memory", p["moment_id"], False)["memory_enabled"]
    assert rpc(users[0], "set_moment_memory", p["moment_id"], True)["memory_enabled"]


@pytest.mark.parametrize("first", ["clear", "finish"])
def test_clear_and_reply_real_lock_interleaving(users, first):
    import psycopg

    p = prepare(users[0])
    prepare(users[1])
    name = "product-race-" + uuid4().hex

    def operation(conn, action):
        if action == "clear":
            return call_on(conn, users[0], "clear_personal_data")
        return call_on(
            conn,
            users[0],
            "chat_control",
            "one",
            p["lease"],
            "finish",
            {"content": "synthetic reply", "evidence_ids": []},
        )

    def second():
        with connection(name) as conn, conn.transaction():
            conn.execute("set local statement_timeout='8s'")
            if first == "clear":
                with pytest.raises(psycopg.Error, match="CHAT_LEASE_LOST"):
                    operation(conn, "finish")
            else:
                operation(conn, "clear")

    with connection() as held, ThreadPoolExecutor(max_workers=1) as pool:
        held.execute("begin")
        try:
            operation(held, first)
            future = pool.submit(second)
            waiting = False
            with connection() as observer:
                for _ in range(60):
                    waiting = bool(
                        observer.execute(
                            "select count(*) from pg_stat_activity "
                            "where application_name=%s and wait_event_type='Lock'",
                            (name,),
                        ).fetchone()[0]
                    )
                    if waiting:
                        break
                    time.sleep(0.03)
            assert waiting and not future.done(), "必须真实观察到锁等待，不能把顺序调用说成并发"
            held.execute("commit")
            future.result(timeout=10)
        finally:
            held.execute("rollback")
    assert count(users[0], "moments") == count(users[0], "thread_messages") == 0
    assert count(users[1], "moments") == 1


def test_anonymous_cannot_call_privileged_product_functions():
    import psycopg

    with connection() as conn, conn.transaction():
        conn.execute("set local role anon")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("select public.clear_personal_data()")
