#!/usr/bin/env python3
"""
doctor.py — blank-state health check for the autoresearch harness.

Run after a fresh clone (or any time something feels off). Prints a table
of pass/fail/warn per check and exits non-zero if any hard check fails.

Usage:
    python scripts/doctor.py              # normal
    python scripts/doctor.py --quick      # skip the live evaluate.py check
    python scripts/doctor.py --json       # machine-readable output

No network. No side-effects beyond stdout + an optional smoke-score write to
the score cache (reverted if it happened).
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


SMOKE_KEYWORD = "aeo-vs-seo"  # smallest article, fastest cold score
_KEYWORDS_CFG_CACHE: dict | None = None  # populated on first _load_keywords_cfg()


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str | None = None
    warn: bool = False  # warn, don't fail

    @property
    def status(self) -> str:
        if self.ok:
            return "PASS"
        if self.warn:
            return "WARN"
        return "FAIL"


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, c: Check) -> None:
        self.checks.append(c)
        glyph = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}[c.status]
        color = {"PASS": "", "WARN": "", "FAIL": ""}[c.status]  # no colors for portability
        print(f"  [{glyph}] {c.name:40s} {c.detail}")
        if c.hint and c.status != "PASS":
            print(f"        hint: {c.hint}")

    def hard_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and not c.warn]


# ----- individual checks -----

def check_python(report: Report) -> None:
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 12)
    report.add(Check(
        name="Python >= 3.12",
        ok=ok,
        detail=f"running {sys.version.split()[0]}",
        hint="install python3.12 (Fedora: `sudo dnf install python3.12`)",
    ))


def check_venv(report: Report) -> None:
    venv = PROJECT_ROOT / ".venv"
    active = os.environ.get("VIRTUAL_ENV") == str(venv)
    ok = venv.exists() and active
    detail = "active" if active else ("exists but not activated" if venv.exists() else "missing")
    report.add(Check(
        name=".venv activated",
        ok=ok,
        detail=detail,
        hint="`source .venv/bin/activate` (or `bash scripts/install_deps.sh` for a fresh setup)",
    ))


def check_nvidia_smi(report: Report) -> None:
    smi = shutil.which("nvidia-smi")
    if not smi:
        report.add(Check(
            name="nvidia-smi",
            ok=False,
            detail="not found",
            hint="install NVIDIA drivers; the harness is GPU-only by design",
        ))
        return
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as e:
        report.add(Check(
            name="nvidia-smi runs",
            ok=False, detail=f"error: {e}",
            hint="`nvidia-smi` must return a device row",
        ))
        return
    lines = out.splitlines()
    if not lines:
        report.add(Check(name="GPU present", ok=False, detail="no devices listed"))
        return
    # Parse first GPU
    name, free_mib, total_mib = [p.strip() for p in lines[0].split(",")]
    free_gib = int(free_mib) / 1024
    total_gib = int(total_mib) / 1024
    # Daemon needs ~5 GB. Warn below 7 GB free; fail below 2 GB.
    ok = free_gib >= 2.0
    warn = free_gib < 7.0 and free_gib >= 2.0
    report.add(Check(
        name="GPU free VRAM",
        ok=ok,
        warn=warn,
        detail=f"{name}: {free_gib:.1f} / {total_gib:.1f} GiB free",
        hint="kill any stale processes holding VRAM (`pkill -f eval_server.py`)",
    ))


def check_env_file(report: Report) -> None:
    env = PROJECT_ROOT / ".env"
    if not env.exists():
        report.add(Check(
            name=".env present",
            ok=False, detail="missing",
            hint="copy .env.example and add GEMINI_API_KEY",
        ))
        return
    content = env.read_text(encoding="utf-8", errors="ignore")
    has_gemini = any(line.strip().startswith("GEMINI_API_KEY=")
                     and len(line.split("=", 1)[1].strip()) > 5
                     for line in content.splitlines())
    report.add(Check(
        name=".env has GEMINI_API_KEY",
        ok=has_gemini,  # propose.py + regen.py call Gemini (decisions #43)
        detail="set" if has_gemini else "not set",
        hint="set GEMINI_API_KEY in .env; the proposer and generator call Gemini",
    ))


def _load_keywords_cfg() -> dict | None:
    """Load data/keywords.json once; cached on the module for the run."""
    global _KEYWORDS_CFG_CACHE
    if _KEYWORDS_CFG_CACHE is not None:
        return _KEYWORDS_CFG_CACHE
    cfg_path = PROJECT_ROOT / "data" / "keywords.json"
    if not cfg_path.exists():
        return None
    _KEYWORDS_CFG_CACHE = json.loads(cfg_path.read_text(encoding="utf-8"))
    return _KEYWORDS_CFG_CACHE


def check_content_files(report: Report) -> None:
    cfg = _load_keywords_cfg()
    if cfg is None:
        report.add(Check(name="data/keywords.json", ok=False, detail="missing"))
        return
    missing = []
    thin = []
    for kw in cfg["keywords"]:
        cpath = PROJECT_ROOT / kw["content_file"]
        if not cpath.exists():
            missing.append(kw["content_file"])
            continue
        text = cpath.read_text(encoding="utf-8", errors="ignore")
        if not text.lstrip().startswith("---"):
            thin.append(f"{kw['slug']} (no frontmatter)")
        elif len(text.split()) < 500:
            thin.append(f"{kw['slug']} ({len(text.split())}w < 500)")
    if missing:
        report.add(Check(name="content/*.md present", ok=False,
                         detail=f"missing: {', '.join(missing)}",
                         hint="regenerate via scripts/regen.py or the content-generation skill"))
    else:
        report.add(Check(name="content/*.md present", ok=True,
                         detail=f"{len(cfg['keywords'])} files OK"))
    if thin:
        report.add(Check(name="content/*.md bodies look healthy",
                         ok=False, warn=True,
                         detail="; ".join(thin),
                         hint="thin article will score poorly; consider regenerating"))


def check_profiles(report: Report) -> None:
    cfg = _load_keywords_cfg()
    if cfg is None:
        return  # already reported by check_content_files
    missing = []
    bad = []
    for kw in cfg["keywords"]:
        p = PROJECT_ROOT / "data" / "profiles" / f"{kw['slug']}-profile.json"
        if not p.exists():
            missing.append(p.name)
            continue
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            bad.append(f"{p.name}: {e}")
    if missing:
        report.add(Check(name="data/profiles/*.json present", ok=False,
                         detail=f"missing: {', '.join(missing)}",
                         hint="`python scripts/analyze_competitors.py`"))
    elif bad:
        report.add(Check(name="data/profiles/*.json parse", ok=False,
                         detail="; ".join(bad[:3])))
    else:
        report.add(Check(name="data/profiles/*.json parse", ok=True,
                         detail=f"{len(cfg['keywords'])} files OK"))


def check_profile_geo_fields(report: Report) -> None:
    """v3.3 profiles must carry geo_sim_queries for dim 13. Dim 12 uses
    absolute research-derived targets (no competitor-side GEO aggregates).
    Missing queries are a warn, not a fail — they're only populated after a
    post-v3.3 analyze_competitors.py run."""
    cfg = _load_keywords_cfg()
    if cfg is None:
        return
    bad: list[str] = []
    for kw in cfg["keywords"]:
        p = PROJECT_ROOT / "data" / "profiles" / f"{kw['slug']}-profile.json"
        if not p.exists():
            continue  # covered by check_profiles
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        queries = data.get("geo_sim_queries") or []
        if len(queries) < 2:
            bad.append(f"{kw['slug']}: geo_sim_queries len={len(queries)}")
    if bad:
        report.add(Check(
            name="profiles carry v3.3 geo_sim_queries", ok=True, warn=True,
            detail="; ".join(bad[:3]),
            hint="rebuild profiles: `python scripts/analyze_competitors.py`",
        ))
    else:
        report.add(Check(name="profiles carry v3.3 geo_sim_queries", ok=True,
                         detail=f"{len(cfg['keywords'])} profiles OK"))


def check_profile_freshness(report: Report) -> None:
    try:
        import profile_freshness
    except ImportError as e:
        report.add(Check(name="profile_freshness import", ok=False, detail=str(e)))
        return
    fresh, current, stored = profile_freshness.is_fresh()
    detail = f"hash={current[:12]}…" + ("" if fresh else f" (stored={str(stored)[:12]}…)")
    report.add(Check(
        name="profile_freshness",
        ok=fresh,
        warn=not fresh,   # warn, not fail — may simply be first run
        detail=detail,
        hint="`python scripts/profile_freshness.py` to rebuild if needed",
    ))


def check_eval_server_importable(report: Report) -> None:
    try:
        importlib.import_module("eval_server")
        report.add(Check(name="eval_server importable", ok=True, detail="OK"))
    except Exception as e:
        report.add(Check(name="eval_server importable", ok=False, detail=str(e)))


def check_git_clean(report: Report) -> None:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as e:
        report.add(Check(name="git status", ok=False, detail=str(e)))
        return
    if not out.strip():
        report.add(Check(name="git tree clean", ok=True, detail="no unstaged changes"))
    else:
        n = len(out.splitlines())
        report.add(Check(name="git tree clean", ok=True, warn=True,
                         detail=f"{n} changed file(s)"))


def check_worktrees(report: Report) -> None:
    try:
        out = subprocess.run(
            ["git", "worktree", "list"], cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as e:
        report.add(Check(name="git worktree list", ok=False, detail=str(e)))
        return
    lines = out.splitlines()
    ok = len(lines) == 1  # only master
    report.add(Check(
        name="no stale worktrees",
        ok=ok,
        warn=not ok,
        detail=f"{len(lines)} worktree(s)",
        hint="`git worktree prune && rm -rf .autoresearch/worktrees`",
    ))


def check_state_json(report: Report) -> None:
    p = PROJECT_ROOT / "autoresearch-state.json"
    if not p.exists():
        report.add(Check(name="autoresearch-state.json", ok=True, warn=True,
                         detail="missing (will be created on first run)"))
        return
    try:
        json.loads(p.read_text(encoding="utf-8"))
        report.add(Check(name="autoresearch-state.json parses", ok=True, detail="OK"))
    except json.JSONDecodeError as e:
        report.add(Check(
            name="autoresearch-state.json parses",
            ok=False,
            detail=f"invalid JSON: {e}",
            hint="delete it and re-run `python scripts/parallel_loop.py --iterations 1 --establish-baselines`",
        ))


def check_smoke_evaluate(report: Report, quick: bool) -> None:
    """Live evaluate one article; sanity-check the composite is in [0, 1].
    Expensive (~30-90s cold); skip with --quick."""
    if quick:
        report.add(Check(name="evaluate smoke (skipped)", ok=True, warn=True,
                         detail="--quick set"))
        return
    content = PROJECT_ROOT / "content" / f"{SMOKE_KEYWORD}.md"
    profile = PROJECT_ROOT / "data" / "profiles" / f"{SMOKE_KEYWORD}-profile.json"
    if not content.exists() or not profile.exists():
        report.add(Check(name="evaluate smoke", ok=False,
                         detail="content or profile missing; skipping"))
        return
    t0 = time.time()
    try:
        out = subprocess.run(
            [sys.executable, str(_HERE / "evaluate.py"),
             str(content), "--profile", str(profile), "--no-daemon"],
            capture_output=True, text=True, timeout=300, cwd=str(PROJECT_ROOT),
        )
    except subprocess.TimeoutExpired:
        report.add(Check(name="evaluate smoke", ok=False, detail="timeout > 300s"))
        return
    elapsed = time.time() - t0
    if out.returncode != 0:
        report.add(Check(name="evaluate smoke", ok=False,
                         detail=f"exit {out.returncode}; {out.stderr[-200:]}"))
        return
    try:
        score = float(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        report.add(Check(name="evaluate smoke", ok=False,
                         detail=f"unparseable stdout: {out.stdout[:100]!r}"))
        return
    # v3.3 invalidates the v3.1 baselines (new dims + weight rebalance). Pin
    # the check to a sanity range — any finite composite in [0, 1]. Real
    # baseline drift detection happens in `parallel_loop.py --establish-baselines`
    # and the per-keyword regression gate, not here.
    ok = 0.0 <= score <= 1.0
    report.add(Check(
        name=f"evaluate smoke ({SMOKE_KEYWORD})",
        ok=ok,
        detail=f"got {score:.4f} in {elapsed:.0f}s",
        hint="re-establish baselines: `python scripts/parallel_loop.py --iterations 1 --establish-baselines`",
    ))


# ----- driver -----

def run_all(quick: bool) -> Report:
    r = Report()
    print("autoresearch harness doctor\n---------------------------")
    check_python(r)
    check_venv(r)
    check_nvidia_smi(r)
    check_env_file(r)
    check_content_files(r)
    check_profiles(r)
    check_profile_geo_fields(r)
    check_profile_freshness(r)
    check_eval_server_importable(r)
    check_git_clean(r)
    check_worktrees(r)
    check_state_json(r)
    check_smoke_evaluate(r, quick=quick)
    print()
    return r


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true",
                        help="skip the live evaluate.py smoke (faster, less confidence)")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON report to stdout")
    args = parser.parse_args()

    report = run_all(quick=args.quick)
    hard = report.hard_failures()

    if args.json:
        print(json.dumps({
            "ok": not hard,
            "checks": [
                {"name": c.name, "status": c.status,
                 "detail": c.detail, "hint": c.hint} for c in report.checks
            ],
        }, indent=2))
    else:
        warns = [c for c in report.checks if c.warn and not c.ok]
        summary = (
            f"{len(report.checks) - len(hard) - len(warns)} pass, "
            f"{len(warns)} warn, {len(hard)} fail"
        )
        print(summary)
        if hard:
            print("\nHARD FAILURES — resolve before starting the loop:")
            for c in hard:
                print(f"  - {c.name}: {c.detail}")

    return 0 if not hard else 1


if __name__ == "__main__":
    sys.exit(main())
