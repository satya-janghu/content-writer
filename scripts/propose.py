#!/usr/bin/env python3
"""
propose.py — rule-change proposer, Gemini 3.1 Pro backend.

Called by iter_worker.py (real mode) before regen.regenerate_article.
Builds the full propose prompt (score table + per-dim semantics + git-log
memory + results-tail context via the existing `build_propose_prompt`) with
SKILL.md inlined, calls Gemini 3.1 Pro with thinking_level=HIGH, and writes
the returned rewritten SKILL.md into the worker's git worktree at
`rules/content-generation/SKILL.md`.

iter_worker.py picks up the diff via `git status --porcelain` on that
prefix and passes it to the merge queue along with the post-regen score.

The function does NOT commit, does NOT regenerate content, and does NOT
edit anything outside the rule file. If Gemini returns the current
SKILL.md verbatim, this is a 'no edit' outcome (rc=12 up the stack); the
iteration is dropped.

Public entry point (backend-agnostic signature):

    from propose import propose
    ok = propose(slug, worktree_path, weakest_dim_report, timeout_s=500)

Usage (debugging):
    python scripts/evaluate.py content/aeo-vs-seo.md --profile ... \\
        --suggest-weakest-dim > /tmp/wdr.json
    python scripts/propose.py --slug aeo-vs-seo --worktree . --report /tmp/wdr.json
"""
from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _propose_prompt import (  # noqa: E402
    RULE_DIR_REL, SKILL_FILE_REL, build_propose_prompt_for_gemini,
)
import gemini_backend  # noqa: E402


# Per-call ceiling lives in gemini_backend (1200s, ~5x the observed
# Gemini 3.1 Pro max of 211s on the 2026-04-16 bench).
DEFAULT_TIMEOUT_S = gemini_backend.PROPOSE_TIMEOUT_S


def _log(msg: str) -> None:
    print(f"[propose] {msg}", file=sys.stderr, flush=True)


def _dirty_paths(worktree: Path, prefix: str) -> list[str]:
    """Repo-relative paths modified in the worktree vs HEAD, filtered by prefix."""
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=str(worktree), capture_output=True, text=True, check=True,
    )
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].strip()
        if "->" in rel:
            rel = rel.split("->", 1)[1].strip()
        if rel.startswith(prefix):
            paths.append(rel)
    return paths


def _any_out_of_scope_edits(worktree: Path) -> list[str]:
    """Return any dirty paths OUTSIDE the rule dir. These are a protocol
    violation and cause the proposal to be rejected."""
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=str(worktree), capture_output=True, text=True, check=True,
    )
    out = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].strip()
        if "->" in rel:
            rel = rel.split("->", 1)[1].strip()
        if not rel.startswith(RULE_DIR_REL):
            out.append(rel)
    return out


def propose(slug: str, worktree: Path, weakest_dim_report: dict,
            timeout_s: int = DEFAULT_TIMEOUT_S) -> bool:
    """Invoke Gemini 3.1 Pro to rewrite `rules/content-generation/SKILL.md`
    in `worktree`.

    Returns True if the returned SKILL.md differs from the current one AND
    the only dirty path under `worktree` afterwards is inside the rule dir.
    Returns False if Gemini returned the file unchanged (no edit), the call
    failed, or the diff touches unexpected paths.
    """
    from slog import slog

    worktree = Path(worktree).resolve()
    skill_path = worktree / SKILL_FILE_REL
    if not skill_path.exists():
        _log(f"skill file missing at {skill_path}; aborting")
        return False

    prompt = build_propose_prompt_for_gemini(slug, weakest_dim_report, worktree)

    t0 = time.time()
    slog("propose", slug, "START",
         timeout_s=timeout_s, backend="gemini-pro",
         model=gemini_backend.PRO_MODEL)
    _log(f"{slug}: asking {gemini_backend.PRO_MODEL} to propose a rule edit "
         f"(timeout {timeout_s}s)")

    gemini_backend.rate_gate_pro()
    call = gemini_backend.call(
        gemini_backend.PRO_MODEL,
        prompt,
        timeout_s=timeout_s,
        thinking_level="HIGH",
    )

    elapsed = time.time() - t0

    if not call["ok"]:
        slog("propose", slug, "FAIL",
             elapsed=elapsed, reason=call["error"], backend="gemini-pro")
        _log(f"{slug}: gemini call failed after {elapsed:.1f}s: {call['error']}")
        return False

    proposed = gemini_backend.strip_markdown_fence(call["text"])
    if not proposed.strip():
        slog("propose", slug, "EMPTY_RESPONSE",
             elapsed=elapsed, backend="gemini-pro")
        _log(f"{slug}: empty response after {elapsed:.1f}s")
        return False

    current = skill_path.read_text(encoding="utf-8")
    diff = list(difflib.unified_diff(
        current.splitlines(), proposed.splitlines(),
        fromfile="SKILL.md (current)",
        tofile="SKILL.md (proposed)",
        lineterm="",
    ))
    added = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))

    if added == 0 and removed == 0:
        slog("propose", slug, "NO_EDIT",
             elapsed=elapsed, backend="gemini-pro",
             prompt_token_count=call["prompt_token_count"],
             candidates_token_count=call["candidates_token_count"],
             thoughts_token_count=call["thoughts_token_count"])
        _log(f"{slug}: Pro returned SKILL.md unchanged ({elapsed:.1f}s)")
        return False

    # Write the proposed SKILL.md into the worktree. iter_worker will pick
    # up the diff via `git status --porcelain`.
    skill_path.write_text(proposed, encoding="utf-8")

    # Scope check: any dirty file outside the rule dir is a protocol
    # violation. For Gemini this is only possible if the proposed text
    # somehow caused us to edit the wrong file — shouldn't happen since
    # we wrote exactly one path — but the safety check stays in place.
    oos = _any_out_of_scope_edits(worktree)
    if oos:
        slog("propose", slug, "OUT_OF_SCOPE",
             elapsed=elapsed, paths=oos, backend="gemini-pro")
        _log(f"{slug}: out-of-scope dirty paths detected: {oos}")
        return False

    rule_edits = _dirty_paths(worktree, RULE_DIR_REL)
    if not rule_edits:
        slog("propose", slug, "NO_DIRTY_PATHS",
             elapsed=elapsed, backend="gemini-pro")
        _log(f"{slug}: proposed text differed but git sees no dirty paths "
             f"under {RULE_DIR_REL} — filesystem write may have failed")
        return False

    slog("propose", slug, "OK",
         elapsed=elapsed, files=len(rule_edits), edited=rule_edits,
         diff_added_lines=added, diff_removed_lines=removed,
         backend="gemini-pro",
         prompt_token_count=call["prompt_token_count"],
         candidates_token_count=call["candidates_token_count"],
         thoughts_token_count=call["thoughts_token_count"])
    _log(f"{slug}: OK — +{added}/-{removed} line(s) in {elapsed:.1f}s "
         f"({len(rule_edits)} file(s) edited: {rule_edits})")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True)
    parser.add_argument("--worktree", default=".")
    parser.add_argument("--report",
                        help="path to JSON file with --suggest-weakest-dim output; "
                             "omit to read from stdin")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args()

    if args.report:
        with open(args.report, "r", encoding="utf-8") as f:
            report = json.load(f)
    else:
        report = json.load(sys.stdin)

    if isinstance(report, list):
        match = [r for r in report if r.get("slug") == args.slug]
        if not match:
            _log(f"no report entry for slug {args.slug!r}; got "
                 f"{[r.get('slug') for r in report]}")
            return 2
        report = match[0]

    ok = propose(args.slug, Path(args.worktree), report, timeout_s=args.timeout)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
