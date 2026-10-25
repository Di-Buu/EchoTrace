import json
from pathlib import Path

from app.config import Settings
from app.worker import standalone_worker_settings


def test_serverless_api_requires_explicit_worker_configuration():
    assert not Settings(_env_file=None, vercel=True).background_tasks_available
    assert Settings(_env_file=None, vercel=True, durable_worker_enabled=True).background_tasks_available
    assert Settings(_env_file=None).background_tasks_available


def test_daemon_keeps_full_quality_even_when_platform_sets_vercel():
    api = Settings(_env_file=None, vercel=True, insight_execution_profile="online_demo")
    worker = standalone_worker_settings(api)
    assert worker.use_quality_insight_reasoning
    assert worker.task_timeout_seconds == 600
    assert worker.agent_max_calls == 24
    assert api.vercel and api.insight_execution_profile == "online_demo"


def test_worker_deployment_uses_existing_image_and_no_embedded_secrets():
    root = Path(__file__).resolve().parents[3]
    config = json.loads((root / "vercel.worker.json").read_text(encoding="utf-8"))
    daemon = config["daemons"]["echotrace-worker"]
    assert daemon["root"] == "." and daemon["entrypoint"] == "Dockerfile"
    assert daemon["command"] == ["python", "-m", "app.worker"]
    assert config["outputDirectory"] != daemon["root"]
    assert "env" not in config and "build" not in daemon
