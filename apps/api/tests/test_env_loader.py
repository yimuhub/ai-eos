"""env_loader：AIEOS_ENV_FILE 必须是唯一的加载目标。

回归背景：load_env() 曾经完全忽略 AIEOS_ENV_FILE，只读 apps/api/.env。
演示/便携环境因此加载了真实 .env，模型取到空密钥，上游报 401
"Token not provided"——配置看着都对，就是调不通。
"""
import os

from app.core import env_loader


def test_override_env_file_is_the_only_source(tmp_path, monkeypatch):
    demo = tmp_path / "demo.env"
    demo.write_text("DEMO_ONLY_KEY=demo-value\n")
    real = tmp_path / "real.env"
    real.write_text("REAL_ONLY_KEY=real-value\n")

    monkeypatch.setenv("AIEOS_ENV_FILE", str(demo))
    monkeypatch.setattr(env_loader, "_CANDIDATE_ENV_FILES", (real,))
    monkeypatch.delenv("DEMO_ONLY_KEY", raising=False)
    monkeypatch.delenv("REAL_ONLY_KEY", raising=False)

    loaded = env_loader.load_env()

    assert loaded == [str(demo)]
    assert os.environ["DEMO_ONLY_KEY"] == "demo-value"
    assert "REAL_ONLY_KEY" not in os.environ  # 真实 .env 不得混入隔离环境


def test_default_candidates_load_in_priority_order(tmp_path, monkeypatch):
    first = tmp_path / "api.env"
    first.write_text("FIRST_KEY=1\n")
    second = tmp_path / "root.env"
    second.write_text("SECOND_KEY=2\nSECOND_SAME=from-root\n")

    monkeypatch.delenv("AIEOS_ENV_FILE", raising=False)
    monkeypatch.setattr(env_loader, "_CANDIDATE_ENV_FILES", (first, second))
    monkeypatch.delenv("FIRST_KEY", raising=False)
    monkeypatch.delenv("SECOND_KEY", raising=False)

    loaded = env_loader.load_env()

    assert loaded == [str(first), str(second)]
    assert os.environ["FIRST_KEY"] == "1"
    assert os.environ["SECOND_KEY"] == "2"


def test_existing_process_env_wins(tmp_path, monkeypatch):
    path = tmp_path / "x.env"
    path.write_text("EXISTING_KEY=from-file\n")

    monkeypatch.setenv("AIEOS_ENV_FILE", str(path))
    monkeypatch.setenv("EXISTING_KEY", "from-process")

    env_loader.load_env()

    assert os.environ["EXISTING_KEY"] == "from-process"


def test_resolved_api_key_prefers_runtime_environ_over_startup_snapshot(monkeypatch):
    """运行期换 Key 必须立刻生效（/models/env-key 承诺 applied=true）。

    回归背景：settings.* 是导入期快照。`resolved_api_key` 曾经先读快照、
    再读 os.environ，导致用户在界面里存了新 Key 后仍用旧 Key 请求上游，
    一路 401 Invalid token，只有重启后端才恢复。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "openai_compat_api_key", "stale-startup-key")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "fresh-runtime-key")

    assert settings.resolved_api_key(None, "openai_compat") == "fresh-runtime-key"
    # 显式变量名同样以运行期环境为准
    assert settings.resolved_api_key(
        "OPENAI_COMPAT_API_KEY", "openai_compat") == "fresh-runtime-key"


def test_resolved_api_key_falls_back_to_snapshot_when_environ_empty(monkeypatch):
    from app.core.config import settings

    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)
    monkeypatch.setattr(settings, "openai_compat_api_key", "only-in-snapshot")

    assert settings.resolved_api_key(None, "openai_compat") == "only-in-snapshot"
