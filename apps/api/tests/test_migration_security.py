from pathlib import Path

MIGRATION = (Path(__file__).resolve().parents[3] / "supabase" / "migrations" / "202609220001_initial.sql").read_text(
    encoding="utf-8"
)
WEEKLY_MIGRATION = (
    Path(__file__).resolve().parents[3]
    / "supabase"
    / "migrations"
    / "202609230001_evidence_preserving_weekly_memory.sql"
).read_text(encoding="utf-8")


def test_all_personal_tables_enable_rls() -> None:
    tables = {
        "moments",
        "threads",
        "thread_messages",
        "memories",
        "memory_sources",
        "insights",
        "insight_evidence",
        "product_events",
        "ai_runs",
    }
    for table in tables:
        assert f"alter table public.{table} enable row level security;" in MIGRATION
        assert f"on public.{table}\nfor all using (user_id = auth.uid())" in MIGRATION


def test_authenticated_role_has_explicit_table_grants() -> None:
    assert "grant usage on schema public to authenticated;" in MIGRATION
    grant_sql = MIGRATION.split("grant select, insert, update, delete on table", 1)[1].split(
        "to authenticated;", 1
    )[0]
    for table in {
        "moments",
        "threads",
        "thread_messages",
        "memories",
        "memory_sources",
        "insight_candidates",
        "insights",
        "insight_evidence",
        "product_events",
        "ai_runs",
    }:
        assert f"public.{table}" in grant_sql


def test_vector_search_is_scoped_before_ranking() -> None:
    function_sql = MIGRATION.split("create or replace function public.search_personal_memory", 1)[1]
    assert "m.user_id = auth.uid()" in function_sql
    assert "ms.user_id = auth.uid()" in function_sql
    assert "mo.user_id = auth.uid()" in function_sql


def test_cross_owner_references_have_database_guards() -> None:
    required_triggers = {
        "moments_enforce_thread_owner",
        "threads_enforce_source_owner",
        "messages_enforce_thread_owner",
        "memories_enforce_relation_owner",
        "memory_sources_enforce_owner",
        "insight_evidence_enforce_owner",
    }
    for trigger in required_triggers:
        assert f"create trigger {trigger}" in MIGRATION


def test_ai_runs_support_multi_agent_trace_correlation() -> None:
    ai_runs_sql = MIGRATION.split("create table public.ai_runs", 1)[1].split(");", 1)[0]
    assert "trace_id text not null" in ai_runs_sql
    assert "metadata jsonb not null" in ai_runs_sql
    assert "ai_runs_user_trace_idx" in MIGRATION


def test_raw_indexes_and_weekly_summaries_keep_owner_guards() -> None:
    for table in {"moment_index_chunks", "weekly_reports", "weekly_summary_cards"}:
        assert f"alter table public.{table} enable row level security;" in WEEKLY_MIGRATION
        assert f"on public.{table}\nfor all using (user_id = auth.uid())" in WEEKLY_MIGRATION
    for trigger in {
        "moment_index_chunks_enforce_owner",
        "weekly_reports_enforce_owner",
        "weekly_summary_cards_enforce_owner",
    }:
        assert f"create trigger {trigger}" in WEEKLY_MIGRATION
    assert "where m.user_id = auth.uid()" in WEEKLY_MIGRATION
    assert "where c.user_id = auth.uid()" in WEEKLY_MIGRATION
