"""
Tests for scripts/iter_worker.py — the per-keyword iteration body.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))


def test_stub_rule_tweak_only_touches_skill_md(tmp_path: Path):
    """Dry-run rule tweak must target exactly the consolidated SKILL.md and leave others alone."""
    import iter_worker
    # Mirror the relevant subdirectory into tmp_path
    target_dir = tmp_path / "rules" / "content-generation"
    target_dir.mkdir(parents=True)
    src = PROJECT_ROOT / "rules" / "content-generation" / "SKILL.md"
    (target_dir / "SKILL.md").write_text(src.read_text(encoding="utf-8"),
                                          encoding="utf-8")
    # A bystander file the tweak should NOT modify.
    (target_dir / "other.md").write_text("# do not touch\n", encoding="utf-8")

    touched, note = iter_worker.stub_rule_tweak(tmp_path)
    assert len(touched) == 1
    assert touched[0].endswith("SKILL.md")
    assert "other.md" not in "|".join(touched)
    # File was actually extended
    modified = (target_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "iter_worker stub tweak" in modified
    # note is a short human-readable tag
    assert "stub tweak" in note


def test_stub_rule_tweak_raises_if_target_missing(tmp_path: Path):
    import iter_worker
    with pytest.raises(FileNotFoundError):
        iter_worker.stub_rule_tweak(tmp_path)


def test_slug_to_paths(tmp_path: Path):
    import iter_worker
    kw_cfg = {"slug": "kw-x", "content_file": "content/kw-x.md"}
    c, p = iter_worker.slug_to_paths(kw_cfg, tmp_path)
    assert c.endswith("content/kw-x.md")
    assert p.endswith("data/profiles/kw-x-profile.json")


def test_iter_worker_exits_nonzero_on_daemon_unavailable(tmp_path: Path):
    """Run iter_worker in a subprocess against a bogus socket path.
    Expect non-zero exit and a DaemonUnavailable path.
    """
    # Mirror the tiny pieces of the project that iter_worker reads:
    keywords = {"keywords": [{"slug": "aeo-vs-seo",
                              "content_file": "content/aeo-vs-seo.md"}]}
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "keywords.json").write_text(json.dumps(keywords), encoding="utf-8")
    (tmp_path / "content").mkdir()
    (tmp_path / "content" / "aeo-vs-seo.md").write_text("# stub\n\nbody\n", encoding="utf-8")
    (tmp_path / "data" / "profiles").mkdir()
    (tmp_path / "data" / "profiles" / "aeo-vs-seo-profile.json").write_text("{}", encoding="utf-8")

    output_dir = tmp_path / "proposals"
    env = {"EVAL_SERVER_SOCKET": "/tmp/nonexistent-sock-for-test"}
    # Preserve PATH so python is found.
    import os
    env.update({"PATH": os.environ["PATH"]})

    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "iter_worker.py"),
         "--keyword", "aeo-vs-seo",
         "--worktree", str(tmp_path),
         "--baseline", "0.88",
         "--output", str(output_dir),
         "--dry-run",
         "--socket", "/tmp/nonexistent-sock-for-test"],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode != 0, f"expected non-zero exit, got 0\nSTDERR: {proc.stderr}"
    # Empty proposals dir — nothing should have been written.
    if output_dir.exists():
        assert not list(output_dir.glob("*.json")), "no proposal file should be written on failure"


def test_proposal_schema_required_keys():
    """Describe the proposal schema so merge-queue consumers (tests + code)
    stay in sync. If a key is renamed in iter_worker, this test pins it."""
    REQUIRED = {
        "uuid", "keyword", "worktree", "baseline",
        "pre_composite", "post_composite", "delta_vs_baseline",
        "touched_paths", "note", "dry_run", "worker_pid", "wall_seconds",
    }
    # Read the source to assert the keys exist in the dict literal
    src = (PROJECT_ROOT / "scripts" / "iter_worker.py").read_text(encoding="utf-8")
    for key in REQUIRED:
        assert f'"{key}":' in src, f"proposal dict missing key {key!r} in iter_worker.py"
