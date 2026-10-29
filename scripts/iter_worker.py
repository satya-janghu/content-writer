#!/usr/bin/env python3
"""
iter_worker.py — single-keyword iteration body for the parallel loop.

One invocation = one proposal. The driver spawns 4 of these in parallel,
one per keyword, each in its own git worktree. Each worker:

  1. Reads the current score for its keyword (daemon + cache).
  2. Asks the daemon for the weakest dimension on that keyword.
  3. EITHER proposes a skill rule change (real mode: propose.py asks Gemini
     to rewrite rules/content-generation/SKILL.md in the worktree), OR stubs the change
     (dry-run mode: noop rule tweak for infrastructure testing).
  4. Regenerates the article for its keyword (real mode: invokes
     content-generation skill; dry-run mode: skipped — uses current content).
  5. Scores the resulting article.
  6. Writes a proposal JSON to <output_dir>/<uuid>.json for the merge queue.

The worker does NOT touch master; it operates entirely in its worktree.
Winning proposals are applied to master by the merge queue in parallel_loop.py.

Usage:
    python scripts/iter_worker.py \\
        --keyword seo-for-saas \\
        --worktree /path/to/worktree \\
        --baseline 0.8679 \\
        --output /path/to/proposals \\
        [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import eval_client  # noqa: E402


def _log(msg: str) -> None:
    print(f"[iter_worker pid={os.getpid()}] {msg}", file=sys.stderr, flush=True)


def load_keywords_cfg(project_root: Path) -> list[dict]:
    cfg_path = project_root / "data" / "keywords.json"
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)["keywords"]


def slug_to_paths(keyword_cfg: dict, project_root: Path) -> tuple[str, str]:
    """Return (content_path, profile_path) — absolute, rooted at the given project."""
    slug = keyword_cfg["slug"]
    return (
        str(project_root / keyword_cfg["content_file"]),
        str(project_root / "data" / "profiles" / f"{slug}-profile.json"),
    )


def _dirty_paths(worktree: Path, prefix: str | None = None) -> list[str]:
    """Return paths modified in the worktree (vs HEAD) as repo-relative posix strings.

    Used in real-mode to pick up the rule diff the orchestrator staged before
    launching this worker. Filters to `prefix` when given — e.g. only the
    skill directory (`rules/content-generation/`).
    """
    import subprocess
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=str(worktree), capture_output=True, text=True, check=True,
    )
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        # Porcelain v1 lines look like " M path" / "?? path" / "R  old -> new".
        if len(line) < 4:
            continue
        rel = line[3:].strip()
        if "->" in rel:
            rel = rel.split("->", 1)[1].strip()
        if prefix and not rel.startswith(prefix):
            continue
        paths.append(rel)
    return paths


def stub_rule_tweak(worktree: Path) -> tuple[list[str], str]:
    """Dry-run rule tweak: append a harmless comment to a rule file.

    Real mode replaces this with a propose.py (Gemini) call that reads the
    weakest-dim report and proposes a principled change. The stub exists to
    exercise the merge-queue / worktree / per-keyword-gate plumbing without
    burning skill-invocation tokens.
    """
    target = worktree / "rules" / "content-generation" / "SKILL.md"
    if not target.exists():
        raise FileNotFoundError(f"stub tweak target missing: {target}")
    ts = int(time.time())
    with open(target, "a", encoding="utf-8") as f:
        f.write(f"\n<!-- iter_worker stub tweak {ts} -->\n")
    return [str(target.relative_to(worktree))], f"stub tweak @ {ts}"


def main() -> int:
    from slog import slog, Timer
    parser = argparse.ArgumentParser()
    parser.add_argument("--keyword", required=True, help="keyword slug, e.g. seo-for-saas")
    parser.add_argument("--worktree", required=True, help="path to the worker's git worktree")
    parser.add_argument("--baseline", type=float, required=True, help="committed baseline score for this keyword")
    parser.add_argument("--output", required=True, help="directory to write proposal JSON into")
    parser.add_argument("--dry-run", action="store_true",
                        help="Use stub rule tweak; skip actual content regeneration.")
    parser.add_argument("--socket", default=None,
                        help="Override eval_server socket path (defaults to env / standard path)")
    args = parser.parse_args()

    worktree = Path(args.worktree).resolve()
    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = args.keyword

    if args.socket:
        os.environ["EVAL_SERVER_SOCKET"] = args.socket

    # Get HEAD commit for logging
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=str(worktree), capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        head = "unknown"

    slog("iter_worker", slug, "WORKER_START",
         worktree=str(worktree), head=head, baseline=args.baseline,
         dry_run=args.dry_run, pid=os.getpid())

    # --- 1. Identify paths in the worktree ---
    keywords = load_keywords_cfg(worktree)
    kw_cfg = next((k for k in keywords if k["slug"] == slug), None)
    if kw_cfg is None:
        slog("iter_worker", slug, "WORKER_END", status="error", reason="keyword_not_found")
        return 2
    content_path, profile_path = slug_to_paths(kw_cfg, worktree)

    if not os.path.exists(content_path):
        slog("iter_worker", slug, "WORKER_END", status="error", reason="content_missing")
        return 3
    if not os.path.exists(profile_path):
        slog("iter_worker", slug, "WORKER_END", status="error", reason="profile_missing")
        return 3

    t0 = time.time()

    # --- 2. Read current score (cache-hit if available) ---
    with Timer() as pre_timer:
        try:
            pre = eval_client.score_article(
                content_path, profile_path, verbose=False,
                slug=slug, use_cache=True,
            )
        except eval_client.DaemonUnavailable as e:
            slog("iter_worker", slug, "WORKER_END", status="error",
                 reason="daemon_unavailable", error=str(e)[:120], elapsed=time.time() - t0)
            return 4
    pre_composite = float(pre["composite"])
    pre_scores = pre.get("scores", {})
    slog("iter_worker", slug, "PRE_SCORE",
         composite=pre_composite, baseline=args.baseline,
         cache=pre.get("_cache", "unknown"), elapsed=pre_timer.elapsed,
         scores={k: round(v, 4) for k, v in pre_scores.items()} if pre_scores else {})

    # --- 3. Propose a rule change ---
    if args.dry_run:
        touched_paths, note = stub_rule_tweak(worktree)
        slog("iter_worker", slug, "PROPOSE_END",
             elapsed=0.0, result="stub_tweak", files=len(touched_paths))
    else:
        import propose
        import evaluate as _ev
        weakest_report = _ev._weakest_dim_report(slug, pre)
        weakest_dim = weakest_report.get("weakest_dim")
        weakest_score = weakest_report.get("weakest_score")
        weakest_headroom = weakest_report.get("dims", {}).get(weakest_dim, {}).get("headroom", 0)
        slog("iter_worker", slug, "PROPOSE_START",
             weakest_dim=weakest_dim, weakest_score=weakest_score,
             weakest_headroom=weakest_headroom)

        with Timer() as propose_timer:
            proposed = propose.propose(slug, worktree, weakest_report)
        if not proposed:
            slog("iter_worker", slug, "PROPOSE_END",
                 elapsed=propose_timer.elapsed, result="no_edit")
            slog("iter_worker", slug, "WORKER_END", status="no_edit",
                 elapsed=time.time() - t0)
            return 12
        changed = _dirty_paths(worktree, prefix="rules/content-generation/")
        if not changed:
            slog("iter_worker", slug, "PROPOSE_END",
                 elapsed=propose_timer.elapsed, result="no_dirty_paths")
            slog("iter_worker", slug, "WORKER_END", status="no_edit",
                 elapsed=time.time() - t0)
            return 12
        touched_paths = changed
        note = f"model-proposed rule diff ({len(changed)} file(s)) "
        note += f"targeting {weakest_dim}"
        # Log diff summary
        try:
            diff_stat = subprocess.run(["git", "diff", "--stat", "--", *changed],
                                       cwd=str(worktree), capture_output=True, text=True,
                                       timeout=5).stdout.strip().split("\n")[-1]
        except Exception:
            diff_stat = "?"
        slog("iter_worker", slug, "PROPOSE_END",
             elapsed=propose_timer.elapsed, result="edit",
             files=len(changed), diff=diff_stat,
             targeted_dim=weakest_dim)

    # --- 4. Regenerate content for this keyword ---
    if args.dry_run:
        _log("dry-run: skipping content regeneration")
        slog("iter_worker", slug, "REGEN_SKIP", reason="dry_run")
    else:
        import regen
        slog("iter_worker", slug, "REGEN_START")
        with Timer() as regen_timer:
            ok = regen.regenerate_article(slug, worktree)
        if not ok:
            slog("iter_worker", slug, "REGEN_END",
                 elapsed=regen_timer.elapsed, result="failed")
            slog("iter_worker", slug, "WORKER_END", status="regen_failed",
                 elapsed=time.time() - t0)
            return 13
        # Check output
        out_path = worktree / "content" / f"{slug}.md"
        words = len(out_path.read_text().split()) if out_path.exists() else 0
        slog("iter_worker", slug, "REGEN_END",
             elapsed=regen_timer.elapsed, result="ok", words=words)

    # --- 5. Score the (possibly regenerated) article ---
    slog("iter_worker", slug, "POST_SCORE_START")
    with Timer() as post_timer:
        try:
            post = eval_client.score_article(
                content_path, profile_path, verbose=False,
                slug=slug, use_cache=True,
            )
        except eval_client.DaemonUnavailable as e:
            slog("iter_worker", slug, "WORKER_END", status="error",
                 reason="post_score_daemon_unavailable",
                 error=str(e)[:120], elapsed=time.time() - t0)
            return 4
    post_composite = float(post["composite"])
    post_scores = post.get("scores", {})
    delta_composite = post_composite - pre_composite
    # Per-dim deltas
    dim_deltas = {}
    for d in post_scores:
        pre_s = pre_scores.get(d, 0.0)
        post_s = post_scores[d]
        dim_deltas[d] = round(post_s - pre_s, 4)
    slog("iter_worker", slug, "POST_SCORE",
         composite=post_composite, delta=delta_composite,
         cache=post.get("_cache", "unknown"), elapsed=post_timer.elapsed,
         scores={k: round(v, 4) for k, v in post_scores.items()} if post_scores else {},
         deltas=dim_deltas)

    # --- 6. Write proposal ---
    wall = round(time.time() - t0, 2)
    proposal = {
        "uuid": str(uuid.uuid4()),
        "keyword": slug,
        "worktree": str(worktree),
        "baseline": args.baseline,
        "pre_composite": pre_composite,
        "post_composite": post_composite,
        "delta_vs_baseline": post_composite - args.baseline,
        "touched_paths": touched_paths,
        "note": note,
        "dry_run": args.dry_run,
        "worker_pid": os.getpid(),
        "wall_seconds": wall,
        "_cache_pre": pre.get("_cache"),
        "_cache_post": post.get("_cache"),
    }

    out_path = output_dir / f"{proposal['uuid']}.json"
    tmp = out_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(proposal, f, indent=2)
    os.replace(tmp, out_path)

    status = "keep" if delta_composite > 0 else "reject"
    slog("iter_worker", slug, "WORKER_END",
         status=status, pre=pre_composite, post=post_composite,
         delta=delta_composite, delta_vs_baseline=post_composite - args.baseline,
         elapsed=wall)
    return 0


if __name__ == "__main__":
    sys.exit(main())
