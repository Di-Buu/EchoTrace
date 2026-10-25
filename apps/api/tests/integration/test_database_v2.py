"""Real PostgreSQL/RLS tests. ONLY an explicitly isolated local Supabase DB.

Apply migrations first. No model calls. No .env loading. Synthetic users are
created per test and only those exact UUIDs are removed in fixture teardown.
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import pytest

DSN = os.getenv("ECHO_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="未配置隔离本地 Supabase 数据库，不能假装集成测试通过")


@contextmanager
def connection(application_name="echotrace-integration"):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict

    if os.getenv("ECHO_TEST_DB_CONFIRM") != "isolated":
        raise RuntimeError("需要 ECHO_TEST_DB_CONFIRM=isolated")
    config = conninfo_to_dict(DSN)
    if config.get("host") not in {"127.0.0.1", "localhost", "::1"} or str(config.get("port")) != "55322":
        raise RuntimeError("只允许独立本地测试数据库")
    with psycopg.connect(DSN, autocommit=True, application_name=application_name) as conn:
        yield conn


@pytest.fixture(scope="session", autouse=True)
def require_empty_isolated_database():
    if not DSN:
        return
    with connection() as conn:
        if conn.execute("select count(*) from auth.users").fetchone()[0]:
            raise RuntimeError("集成验收要求全新空测试库，拒绝在已有账号的数据环境运行；不会自动清空")


def rpc(uid, function, *args, service=False):
    from psycopg import sql
    from psycopg.types.json import Jsonb

    args = tuple(
        Jsonb(x) if isinstance(x, dict) or (function == "commit_insight" and index == 1) else x
        for index, x in enumerate(args)
    )
    with connection() as conn, conn.transaction():
        conn.execute("set local role service_role" if service else "set local role authenticated")
        conn.execute("select set_config('request.jwt.claims',%s,true)", (f'{{"sub":"{uid}","role":"authenticated"}}',))
        command = sql.SQL("select * from public.{}({})").format(
            sql.Identifier(function),
            sql.SQL(",").join(
                sql.SQL("%s::uuid[]")
                if (function == "evidence_snapshot" and index == 0) or (function == "worker_read" and index == 3)
                else sql.Placeholder()
                for index, _ in enumerate(args)
            ),
        )
        return conn.execute(command, args).fetchall()


@pytest.fixture
def users():
    ids = [str(uuid4()), str(uuid4())]
    with connection() as conn:
        for uid in ids:
            conn.execute("insert into auth.users(id,email) values(%s,%s)", (uid, f"test-{uid}@example.invalid"))
    try:
        yield ids
    finally:
        with connection() as conn:
            for uid in ids:
                conn.execute("delete from auth.users where id=%s", (uid,))


def moment(uid, key="save", content="合成测试记录"):
    return rpc(uid, "submit_operation", "moment", key, "not-trusted", {"content": content})[0][0]


def count(uid, table):
    from psycopg import sql

    with connection() as conn:
        return conn.execute(
            sql.SQL("select count(*) from public.{} where user_id=%s").format(sql.Identifier(table)), (uid,)
        ).fetchone()[0]


def draft():
    return {
        "insight_type": "fact",
        "trigger_type": "user_query",
        "title": "测试",
        "body": "仅用于测试",
        "verification_status": "PASS",
        "evidence_version": "v2-test",
        "agent_version": "v2-test",
    }


def commit(uid, mid, revision=1, stance="support"):
    return rpc(uid, "commit_insight", draft(), [{"moment_id": mid, "stance": stance}], {mid: revision})


def test_concurrent_same_key_creates_one_moment_and_one_job(users):
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda _: moment(users[0]), range(12)))
    assert len({row["id"] for row in rows}) == 1
    assert count(users[0], "moments") == count(users[0], "background_jobs") == 1


def test_key_conflict_and_user_operation_isolation(users):
    import psycopg

    first = moment(users[0])
    assert moment(users[1])["id"] != first["id"]
    with pytest.raises(psycopg.Error, match="IDEMPOTENCY_CONFLICT"):
        moment(users[0], content="不同内容")
    assert rpc(users[0], "submit_operation", "analysis", "save", "x", {"question": "测试历史"})[0][0]["id"]


def test_rls_denies_foreign_evidence_and_task_lookup(users):
    foreign = moment(users[1])
    assert rpc(users[0], "evidence_snapshot", [foreign["id"]]) == []
    with connection() as conn, conn.transaction():
        conn.execute("set local role authenticated")
        conn.execute("select set_config('request.jwt.claims',%s,true)", (f'{{"sub":"{users[0]}"}}',))
        assert conn.execute("select id from public.background_jobs where user_id=%s", (users[1],)).fetchall() == []


def test_invalid_evidence_rolls_back_insight(users):
    import psycopg

    row = moment(users[0])
    with pytest.raises(psycopg.Error):
        commit(users[0], row["id"], stance="illegal")
    assert count(users[0], "insights") == 0
    assert count(users[0], "insight_evidence") == 0
    foreign = moment(users[1])
    with pytest.raises(psycopg.Error):
        commit(users[0], foreign["id"])


def test_revision_and_revocation_invalidate_results(users):
    import psycopg

    row = moment(users[0])
    commit(users[0], row["id"])
    with connection() as conn:
        conn.execute("update public.moments set content='已修订的合成数据' where id=%s", (row["id"],))
        assert conn.execute("select revision from public.moments where id=%s", (row["id"],)).fetchone()[0] == 2
        assert conn.execute("select status from public.insights where user_id=%s", (users[0],)).fetchone()[0] == "stale"
    with pytest.raises(psycopg.Error, match="EVIDENCE_CHANGED"):
        commit(users[0], row["id"])


def test_concurrent_publish_and_revoke_never_leaves_active_result(users):
    import psycopg

    row = moment(users[0])

    def publish():
        try:
            commit(users[0], row["id"])
        except psycopg.Error as exc:
            assert "EVIDENCE_CHANGED" in str(exc)

    def revoke():
        with connection() as conn:
            conn.execute("update public.moments set memory_enabled=false where id=%s", (row["id"],))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish), pool.submit(revoke)]
        for future in futures:
            future.result(timeout=10)
    with connection() as conn:
        assert (
            conn.execute(
                "select count(*) from public.insights where user_id=%s and status='active'", (users[0],)
            ).fetchone()[0]
            == 0
        )


def test_atomic_claim_lease_recovery_and_old_worker_fencing(users):
    job = rpc(users[0], "submit_operation", "analysis", "a", "x", {"question": "合成测试"})[0][0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: rpc(users[0], "worker_claim", 45, 600, service=True), range(2)))
    assert sum(len(rows) for rows in claims) == 1
    # SETOF returns columns, not a JSON object. Read canonical row via admin.
    with connection() as conn:
        old = conn.execute("select lease_token from public.background_jobs where id=%s", (job["id"],)).fetchone()[0]
        conn.execute(
            "update public.background_jobs set lease_until=now()-interval '1 second' where id=%s", (job["id"],)
        )
    assert rpc(users[0], "worker_claim", 45, 600, service=True)
    assert rpc(users[0], "worker_control", "check", job["id"], old, {}, service=True) == []
    import psycopg

    with pytest.raises(psycopg.Error, match="LEASE_LOST"):
        rpc(users[0], "worker_evidence_rpc", job["id"], old, "commit_insight", {}, service=True)


def test_normal_user_cannot_claim_or_supply_worker_owner(users):
    import psycopg

    with pytest.raises(psycopg.Error):
        rpc(users[0], "worker_claim", 45, 600)
    with pytest.raises(psycopg.Error):
        rpc(users[0], "worker_evidence_rpc", str(uuid4()), str(uuid4()), "evidence_snapshot", {"p_ids": []})


def test_worker_fixed_read_cannot_read_foreign_record(users):
    own = moment(users[0])
    foreign = moment(users[1])
    rpc(users[0], "worker_claim", 45, 600, service=True)
    with connection() as conn:
        job = conn.execute(
            "select id,lease_token from public.background_jobs where user_id=%s and status='running'", (users[0],)
        ).fetchone()
    assert job is not None
    rows = rpc(users[0], "worker_read", job[0], job[1], "moments", [foreign["id"]], service=True)
    assert rows[0][0] == []
    assert own["user_id"] == users[0]


def test_cancel_blocks_finish_and_expired_retry_limit(users):
    job = rpc(users[0], "submit_operation", "analysis", "a", "x", {"question": "合成测试"})[0][0]
    rpc(users[0], "worker_claim", 45, 600, service=True)
    with connection() as conn:
        lease = conn.execute("select lease_token from public.background_jobs where id=%s", (job["id"],)).fetchone()[0]
    rpc(users[0], "cancel_job", job["id"])
    assert rpc(users[0], "worker_control", "complete", job["id"], lease, {}, service=True) == []
    with connection() as conn:
        conn.execute(
            "update public.background_jobs set status='running',attempts=max_attempts,"
            "lease_until=now()-interval '1 second' where id=%s",
            (job["id"],),
        )
    assert rpc(users[0], "worker_claim", 45, 600, service=True) == []


def test_telemetry_failure_does_not_rollback_required_data(users):
    # Isolated test DB only; transactionally installs/removes the synthetic failure trigger.
    # It affects one synthetic user, not unrelated requests.
    with connection() as conn:
        conn.execute(
            "create function public.test_event_fail() returns trigger language plpgsql as $$ "
            "begin if new.user_id::text=TG_ARGV[0] then raise exception 'synthetic'; end if; return new; end $$"
        )
        conn.execute(
            "create trigger test_event_fail before insert on public.product_events "
            "for each row execute function public.test_event_fail('" + users[0] + "')"
        )
    try:
        moment(users[0])
        assert count(users[0], "moments") == count(users[0], "background_jobs") == 1
    finally:
        with connection() as conn:
            conn.execute("drop trigger test_event_fail on public.product_events")
            conn.execute("drop function public.test_event_fail()")


def wait_for_advisory_wait(application_name):
    """Require an observed DB lock wait, rather than assume scheduling overlaps."""
    with connection() as monitor:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if monitor.execute(
                "select exists(select 1 from pg_stat_activity where application_name=%s "
                "and wait_event_type='Lock' and wait_event='advisory')",
                (application_name,),
            ).fetchone()[0]:
                return
            time.sleep(0.02)
    raise AssertionError("没有观察到真实锁等待；不能宣称已验证交错执行")


@pytest.mark.parametrize("first", ["revoke", "publish"])
def test_forced_publish_revoke_interleaving_both_orders(users, first):
    import psycopg
    from psycopg.types.json import Jsonb

    row = moment(users[0])
    app_name = "echo-race-" + uuid4().hex

    def publish(conn):
        conn.execute("set local role authenticated")
        conn.execute("select set_config('request.jwt.claims',%s,true)", (f'{{"sub":"{users[0]}"}}',))
        conn.execute(
            "select public.commit_insight(%s,%s,%s)",
            (Jsonb(draft()), Jsonb([{"moment_id": row["id"], "stance": "support"}]), Jsonb({row["id"]: 1})),
        )

    def revoke(conn):
        conn.execute("update public.moments set memory_enabled=false where id=%s", (row["id"],))

    def second():
        with connection(app_name) as conn, conn.transaction():
            # Fail promptly on an unexpected deadlock / missing release.
            conn.execute("set local statement_timeout='8s'")
            if first == "revoke":
                with pytest.raises(psycopg.Error, match="EVIDENCE_CHANGED"):
                    publish(conn)
            else:
                revoke(conn)

    with connection() as held, ThreadPoolExecutor(max_workers=1) as pool:
        held.execute("begin")
        try:
            (revoke if first == "revoke" else publish)(held)
            future = pool.submit(second)
            wait_for_advisory_wait(app_name)
            assert not future.done()
            held.execute("commit")
            future.result(timeout=10)
        finally:
            held.execute("rollback")
    with connection() as conn:
        assert (
            conn.execute(
                "select count(*) from public.insights where user_id=%s and status='active'", (users[0],)
            ).fetchone()[0]
            == 0
        )


def test_claim_skips_a_task_locked_by_another_worker_transaction(users):
    job = rpc(users[0], "submit_operation", "analysis", "claim", "x", {"question": "合成领取测试"})[0][0]
    with connection() as held, held.transaction():
        held.execute("set local role service_role")
        assert held.execute("select id from public.worker_claim(45,600)").fetchone()[0]
        # First claim is uncommitted and holds the row lock. Second must skip it.
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(lambda: rpc(users[0], "worker_claim", 45, 600, service=True)).result(timeout=5) == []
    with connection() as conn:
        assert conn.execute("select attempts from public.background_jobs where id=%s", (job["id"],)).fetchone()[0] == 1
