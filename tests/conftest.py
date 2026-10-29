"""
Shared pytest fixtures for the autoresearch harness test suite.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(scope="session")
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture
def tmp_state_file(tmp_path: Path, monkeypatch) -> Path:
    """Redirect scripts that read autoresearch-state.json to a tmp path.
    Any test that mutates state uses this; keeps the real state file safe.
    """
    p = tmp_path / "autoresearch-state.json"
    p.write_text("{}\n", encoding="utf-8")
    # Modules that cache STATE_PATH at import time need their attribute patched.
    import score_cache, profile_freshness  # local import so PROJECT_ROOT is on path
    monkeypatch.setattr(score_cache, "STATE_PATH", p, raising=False)
    monkeypatch.setattr(profile_freshness, "STATE_PATH", p, raising=False)
    return p


@pytest.fixture(scope="session")
def daemon_socket_path(tmp_path_factory) -> str:
    """Unique socket path so a parallel test run doesn't collide with a dev daemon."""
    return str(tmp_path_factory.mktemp("sock") / "eval.sock")


def _wait_for_socket(path: str, timeout_s: float = 90.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def eval_daemon(daemon_socket_path):
    """Session-scoped daemon for slow scoring tests.
    Marked `slow`; tests that don't need the daemon should skip it."""
    env = os.environ.copy()
    env["EVAL_SERVER_SOCKET"] = daemon_socket_path
    log = open("/tmp/pytest-eval-server.log", "a")
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPTS_DIR / "eval_server.py"),
         "--socket", daemon_socket_path],
        cwd=str(PROJECT_ROOT),
        stdout=log, stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    if not _wait_for_socket(daemon_socket_path, timeout_s=120):
        proc.kill()
        pytest.skip("eval_server did not come up in time")
    yield daemon_socket_path
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


@pytest.fixture
def sample_content_path(project_root: Path) -> str:
    """A known-good content file used in several tests."""
    return str(project_root / "content" / "aeo-vs-seo.md")


@pytest.fixture
def sample_profile_path(project_root: Path) -> str:
    return str(project_root / "data" / "profiles" / "aeo-vs-seo-profile.json")


@pytest.fixture
def throwaway_git_repo(tmp_path: Path) -> Path:
    """A fresh git repo rooted at tmp_path. Useful for commit/worktree tests."""
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(tmp_path), check=True)
    (tmp_path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(tmp_path), check=True)
    return tmp_path
