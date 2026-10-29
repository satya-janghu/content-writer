#!/usr/bin/env python3
"""
parallel_loop.py — async-per-keyword driver for the autoresearch loop.

Flow (post-decisions-#42):

One supervisor process runs in the foreground. It spawns ONE long-running
worker coordinator thread per keyword (4 threads). Each coordinator:

    while not (stagnated or ceiling_hit):
        ensure my worktree is at master HEAD (prune if out of date)
        spawn iter_worker.py as a subprocess in my worktree
        wait for it to exit (no coordination with other workers)
        read its proposal JSON (if any)
        if proposal.post_composite > my_baseline:
            push proposal onto merge_queue (serialized by lock)
            stagnation_streak = 0
        else:
            stagnation_streak += 1
        cleanup worktree
        record state

A single merge thread drains the merge_queue serially: apply proposal
diff to master, bg_commit, release. The merge thread is the only writer
to master's rules/; workers never touch master.

When a commit lands on master, in-flight workers are stale (they forked
off an older HEAD) — that's fine. Their proposal is still valid (local
gate proved it improved their keyword); they just might propose on top
of a rule that's about to be overwritten. The merge queue is last-writer-
wins on SKILL.md content, which matches the pre-async behaviour because
the pre-async merge queue also serialized keepers FIFO.

Exit: when EVERY keyword's stagnation_streak >= max_stagnation, the
supervisor tears down all workers and returns rc=0. Hitting the
--iterations ceiling is also an exit. --batch-timeout is removed
(batches no longer exist); propose + regen timeouts enforce per-iter
wall-clock ceilings.

Usage:
    python scripts/parallel_loop.py --iterations 200 --max-stagnation 10
    # Runs until every keyword has stagnated 10 consecutive iterations
    # or until any keyword reaches 200 total iterations, whichever first.

    python scripts/parallel_loop.py --iterations 5 --dry-run
    # Infra smoke with stub rule tweaks. No model API calls.

    python scripts/parallel_loop.py --iterations 1 --establish-baselines
    # One-shot: score current content/ and write autoresearch-state.baselines.
"""
from __future__ import annotations

import argparse
import json
import os
import queue as _queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import eval_client  # noqa: E402
import profile_freshness  # noqa: E402
import bg_commit  # noqa: E402

PROJECT_ROOT = _HERE.parent
WORKTREE_BASE = PROJECT_ROOT / ".autoresearch" / "worktrees"
PROPOSALS_DIR = PROJECT_ROOT / ".autoresearch" / "proposals"
RESULTS_TSV = PROJECT_ROOT / "autoresearch-results.tsv"


def _log(msg: str) -> None:
    print(f"[parallel_loop] {msg}", file=sys.stderr, flush=True)


def load_keywords() -> list[dict]:
    with open(PROJECT_ROOT / "data" / "keywords.json", "r", encoding="utf-8") as f:
        return json.load(f)["keywords"]


def load_baselines() -> dict[str, float]:
    """Per-keyword baselines from autoresearch-state.json.baselines.

    These are established by a one-time `--establish-baselines` run after
    any scoring-semantic change (judge-model swap, weight change, etc.).
    The loop refuses to run without them so the per-keyword gate can't
    silently pass with stale values.
    """
    import score_cache
    baselines = score_cache.read_state().get("baselines", {}) or {}
    return {k: float(v) for k, v in baselines.items()}


def save_baselines(baselines: dict[str, float]) -> None:
    """Write the baselines block to autoresearch-state.json under the flock."""
    import score_cache
    with score_cache.mutate_state() as state:
        state["baselines"] = {k: float(v) for k, v in baselines.items()}


def establish_baselines(keywords: list[dict]) -> dict[str, float]:
    """Score every keyword's current content/*.md via the daemon and record
    the composite as that keyword's baseline. Flushes the score cache first
    so every score is freshly computed under the currently-loaded models
    (no accidental reuse of stale judge scores)."""
    try:
        eval_client.flush_cache()
    except eval_client.DaemonUnavailable:
        pass  # no daemon yet; that's fine — score_article will fall through

    baselines: dict[str, float] = {}
    for kw in keywords:
        slug = kw["slug"]
        content = str(PROJECT_ROOT / kw["content_file"])
        profile = str(PROJECT_ROOT / "data" / "profiles" / f"{slug}-profile.json")
        _log(f"establishing baseline: {slug}")
        result = eval_client.score_article(content, profile, verbose=False,
                                           slug=slug, use_cache=False)
        score = float(result["composite"])
        baselines[slug] = score
        _log(f"  {slug} -> {score:.4f}")
    save_baselines(baselines)
    _log(f"baselines saved to autoresearch-state.json: {baselines}")
    return baselines


# ---- Daemon management ----

def ensure_daemon_up(timeout_s: int = 90) -> subprocess.Popen | None:
    """Start the eval_server daemon if not reachable; wait for readiness."""
    if eval_client.is_daemon_running():
        _log("daemon already running")
        return None
    _log("starting eval_server daemon...")
    proc = subprocess.Popen(
        [sys.executable, str(_HERE / "eval_server.py")],
        cwd=str(PROJECT_ROOT),
        stdout=open("/tmp/parallel_loop-eval_server.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if eval_client.is_daemon_running():
            _log(f"daemon ready (pid {proc.pid})")
            return proc
        if proc.poll() is not None:
            raise RuntimeError(f"daemon exited prematurely with rc={proc.returncode} "
                               f"(see /tmp/parallel_loop-eval_server.log)")
        time.sleep(1.0)
    raise TimeoutError(f"daemon failed to become ready within {timeout_s}s")


# ---- Worktrees ----



# ---- Merge queue (serial) ----

def apply_proposal_to_master(proposal: dict) -> list[str]:
    """Copy the proposal's touched files from its worktree onto master.

    Returns the list of paths relative to PROJECT_ROOT that were modified.
    """
    wt = Path(proposal["worktree"])
    modified: list[str] = []
    for rel in proposal["touched_paths"]:
        src = wt / rel
        dst = PROJECT_ROOT / rel
        if not src.exists():
            _log(f"apply: source missing {src}; skipping")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        modified.append(rel)
    return modified


def revert_paths(paths: list[str]) -> None:
    if not paths:
        return
    subprocess.run(
        ["git", "checkout", "HEAD", "--"] + paths,
        cwd=str(PROJECT_ROOT), check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def drain_merge_queue(keepers: list[dict], baselines: dict[str, float],
                      dry_run: bool, iter_no: int) -> tuple[int, set[str]]:
    """Apply each keeper in order and commit immediately.

    The cross-keyword gate that used to sit here was removed in
    decisions #42: the regen-all-4 + score-all-4 validation step cost 1290s
    per batch and rejected 2/3 of otherwise-good edits over regressions
    inside the +-0.5% scoring noise band. The invariant that replaces it:
    each worker's local gate (post_composite > baseline for ITS OWN
    keyword) is already proof the rule does what the proposer intended.
    If a rule change hurts another keyword over time, that keyword's
    next iteration's pre-score will catch it and the proposer will edit
    in the other direction. The loop self-corrects through iteration.

    In dry-run mode, applied diffs are reverted (no commit) but the
    keeper's keyword is still returned in `committed` so the stagnation
    counter resets — matches the pre-rewrite contract.
    """
    from slog import slog
    slog("parallel_loop", f"batch_{iter_no}", "MERGE_QUEUE_START",
         keepers=len(keepers))
    queue_t0 = time.time()
    commits = 0
    committed: set[str] = set()
    for ki, k in enumerate(keepers):
        slug = k["keyword"]
        slog("parallel_loop", f"batch_{iter_no}", "MERGE_PROCESS",
             keeper=ki + 1, keyword=slug, delta=k["delta_vs_baseline"],
             uuid=k["uuid"][:8])
        _log(f"merge queue: processing proposal {k['uuid'][:8]} for {slug} "
             f"(delta={k['delta_vs_baseline']:+.4f})")
        keeper_t0 = time.time()

        modified = apply_proposal_to_master(k)
        if not modified:
            _log("  no paths applied; skipping")
            continue

        if dry_run:
            _log("  dry-run: reverting instead of committing")
            revert_paths(modified)
            decision = "dry-keep"
        else:
            msg = f"iter {iter_no} keep ({slug}, delta={k['delta_vs_baseline']:+.4f})"
            _log("  committing")
            bg_commit.commit_in_background(msg, modified)
            commits += 1
            decision = "keep"
        committed.add(slug)
        slog("parallel_loop", f"batch_{iter_no}", "MERGE_APPLIED",
             keyword=slug, decision=decision,
             elapsed=time.time() - keeper_t0)
        bg_commit.append_tsv(str(RESULTS_TSV), [
            int(time.time()), iter_no, slug, decision,
            f"{k['pre_composite']:.4f}", f"{k['post_composite']:.4f}",
            f"{k['delta_vs_baseline']:+.4f}", k["uuid"],
        ])
    slog("parallel_loop", f"batch_{iter_no}", "MERGE_QUEUE_END",
         elapsed=time.time() - queue_t0, commits=commits,
         committed=list(committed))
    return commits, committed


# ---- Checkpoint + resume ----

def _read_loop_state() -> dict:
    """Read the `loop` block from autoresearch-state.json. {} on any failure."""
    import score_cache
    return score_cache.read_state().get("loop", {}) or {}


def _write_loop_state(loop_state: dict) -> None:
    """Persist the `loop` block under an exclusive lock so concurrent writers
    (e.g. a stray worker) can't corrupt it."""
    import score_cache
    with score_cache.mutate_state() as state:
        state["loop"] = loop_state


def _git_head_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(PROJECT_ROOT),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out[:10]
    except (subprocess.SubprocessError, OSError):
        return ""


def _load_streaks(slugs: list[str]) -> dict[str, int]:
    """Per-keyword 'iterations since last keep-commit'. Zero for any slug
    with no stored streak (first run or resume after a fresh baseline)."""
    stored = _read_loop_state().get("stagnation_streaks", {}) or {}
    return {s: int(stored.get(s, 0)) for s in slugs}


# ---- Async supervisor: merge queue ----

class MergeQueue:
    """FIFO queue of keeper proposals, drained serially by a single thread.

    Workers push keepers via `submit(proposal)`. The merge thread pops them
    in arrival order, applies + commits each via the existing drain_merge_queue
    (no cross-keyword gate; just apply + commit), and fires a
    completion callback so the pushing coordinator can reset its streak
    and the supervisor can persist state.

    Sentinel `None` tells the drain thread to exit. Push it once per
    supervisor shutdown.
    """
    def __init__(self, baselines: dict[str, float], dry_run: bool,
                 on_commit=None):
        self._q: _queue.Queue = _queue.Queue()
        self._baselines = baselines
        self._dry_run = dry_run
        self._on_commit = on_commit
        self._thread: threading.Thread | None = None
        self._iter_counter = 0
        self._lock = threading.Lock()
        self._closed = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._drain, name="merge-queue",
                                        daemon=True)
        self._thread.start()

    def submit(self, proposal: dict) -> bool:
        """Return True if enqueued, False if the queue is closed.

        The race being guarded: worker_coordinator finishes `proc.wait()` and
        enters this method just as the supervisor calls `shutdown()`. Without
        the `_closed` flag, `shutdown` would push the sentinel into the queue
        first; the coordinator's `put` then lands after the sentinel and the
        drain thread exits on the sentinel, silently dropping the commit. The
        flag + lock serialize the two paths so a late submit is rejected with
        an audit-logged drop.
        """
        from slog import slog
        with self._lock:
            if self._closed:
                slog("parallel_loop", "merge", "SUBMIT_AFTER_CLOSE",
                     keyword=proposal.get("keyword"),
                     uuid=proposal.get("uuid", "")[:8])
                return False
            self._q.put(proposal)
            return True

    def shutdown(self, timeout_s: float = 30.0) -> None:
        with self._lock:
            self._closed = True
        self._q.put(None)
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)

    def _drain(self) -> None:
        from slog import slog
        while True:
            item = self._q.get()
            if item is None:
                slog("parallel_loop", "merge", "DRAIN_EXIT")
                return
            with self._lock:
                self._iter_counter += 1
                iter_no = self._iter_counter
            commits, committed = drain_merge_queue(
                [item], self._baselines, self._dry_run, iter_no,
            )
            if self._on_commit and committed:
                self._on_commit(item["keyword"], iter_no, commits > 0)


# ---- Async supervisor: coordinator helpers ----

def _refresh_worktree(keyword: str, worktree: Path) -> None:
    """Tear down + recreate the keyword's worktree so it's at master HEAD.

    Called between iterations of the same coordinator. Cheaper than a git
    fetch+reset because the worktree is <200 MB of tracked files and
    worktree add --detach HEAD is a near-instant hard link operation on
    most filesystems."""
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=str(PROJECT_ROOT), check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
        cwd=str(PROJECT_ROOT), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def _spawn_worker(slug: str, worktree: Path, baseline: float,
                  dry_run: bool) -> subprocess.Popen:
    cmd = [
        sys.executable, str(_HERE / "iter_worker.py"),
        "--keyword", slug,
        "--worktree", str(worktree),
        "--baseline", f"{baseline:.6f}",
        "--output", str(PROPOSALS_DIR),
    ]
    if dry_run:
        cmd.append("--dry-run")
    log_file = f"/tmp/parallel_loop-worker-{slug}.log"
    # start_new_session puts iter_worker and any children it spawns into
    # their own process group so stop_event teardown can SIGKILL the whole
    # tree with one killpg.
    return subprocess.Popen(
        cmd, cwd=str(worktree),
        stdout=open(log_file, "a"), stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _read_fresh_proposal(slug: str, start_ts: float) -> dict | None:
    """Return the newest proposal for `slug` written after `start_ts`.
    The worker writes 0 or 1 proposals per invocation; if there are
    more (shouldn't happen), the newest wins."""
    candidates = []
    for p in PROPOSALS_DIR.glob("*.json"):
        if p.stat().st_mtime < start_ts:
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                prop = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if prop.get("keyword") != slug:
            continue
        prop["__path__"] = str(p)
        candidates.append(prop)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p["__path__"])
    return candidates[-1]


def worker_coordinator(slug: str, baseline: float, max_iters: int,
                       max_stagnation: int, merge_queue: MergeQueue,
                       dry_run: bool, stop_event: threading.Event,
                       streak_state: dict[str, int],
                       streak_lock: threading.Lock) -> None:
    """Per-keyword long-running loop. Runs until stop_event, stagnation,
    iteration ceiling, or repeated worktree-refresh failure."""
    import os as _os
    import signal as _signal
    from slog import slog
    MAX_REFRESH_FAILS = 5
    WAIT_POLL_S = 2.0

    worktree = WORKTREE_BASE / slug
    iters_run = 0
    refresh_fail_streak = 0
    slog("parallel_loop", slug, "COORDINATOR_START",
         baseline=baseline, max_iters=max_iters, max_stagnation=max_stagnation,
         dry_run=dry_run)

    def _end(reason: str, streak: int) -> None:
        slog("parallel_loop", slug, "COORDINATOR_END",
             reason=reason, iters_run=iters_run, streak=streak)

    while not stop_event.is_set() and iters_run < max_iters:
        with streak_lock:
            streak = streak_state.get(slug, 0)
        if streak >= max_stagnation:
            _end("stagnated", streak)
            return

        try:
            _refresh_worktree(slug, worktree)
            refresh_fail_streak = 0
        except subprocess.SubprocessError as e:
            refresh_fail_streak += 1
            slog("parallel_loop", slug, "WORKTREE_REFRESH_FAIL",
                 error=str(e)[:160], streak=refresh_fail_streak)
            if refresh_fail_streak >= MAX_REFRESH_FAILS:
                slog("parallel_loop", slug, "COORDINATOR_REFRESH_GIVE_UP",
                     fails=refresh_fail_streak)
                _end("refresh_give_up", streak)
                return
            time.sleep(5)
            continue

        iter_start_ts = time.time()
        slog("parallel_loop", slug, "ITER_START",
             iters_run=iters_run + 1, streak=streak)
        proc = _spawn_worker(slug, worktree, baseline, dry_run)
        # Cooperative wait: poll on WAIT_POLL_S cadence so stop_event can
        # tear us down mid-iteration. propose + regen already enforce their
        # own internal timeouts, so a runaway worker can't live longer than
        # their ceiling. The only in-flight kill path is stop_event-driven.
        rc = None
        while rc is None:
            try:
                rc = proc.wait(timeout=WAIT_POLL_S)
            except subprocess.TimeoutExpired:
                if stop_event.is_set():
                    try:
                        _os.killpg(proc.pid, _signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    try:
                        rc = proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        rc = -9
                    slog("parallel_loop", slug, "ITER_KILLED_BY_STOP_EVENT",
                         rc=rc, elapsed=time.time() - iter_start_ts)
                    _end("killed_by_stop_event", streak)
                    return
        elapsed = time.time() - iter_start_ts
        slog("parallel_loop", slug, "ITER_END",
             rc=rc, elapsed=elapsed)

        proposal = _read_fresh_proposal(slug, iter_start_ts)
        kept = False
        if proposal and proposal["post_composite"] > proposal["baseline"]:
            if merge_queue.submit(proposal):
                kept = True
                slog("parallel_loop", slug, "PROPOSAL_SUBMITTED",
                     uuid=proposal.get("uuid", "")[:8],
                     post_composite=proposal["post_composite"],
                     baseline=proposal["baseline"])

        with streak_lock:
            if kept:
                streak_state[slug] = 0
            else:
                streak_state[slug] = streak_state.get(slug, 0) + 1
        iters_run += 1

    with streak_lock:
        final_streak = streak_state.get(slug, 0)
    _end("stop_event" if stop_event.is_set() else "ceiling", final_streak)


# ---- Entry point ----

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", "-n", type=int, required=True,
                        help="Per-keyword iteration ceiling. Each worker "
                             "coordinator runs up to this many iterations on "
                             "its own keyword before forcibly stopping.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Stub rule tweaks; skip content regen.")
    parser.add_argument("--resume", action="store_true",
                        help="Reload streaks from autoresearch-state.json. "
                             "Async mode: there are no in-flight batches to "
                             "restart; --resume is just streak continuity.")
    parser.add_argument("--max-stagnation", type=int, default=10,
                        help="Per-keyword: stop its coordinator after this "
                             "many consecutive non-keep iterations. When all "
                             "4 coordinators hit this, the supervisor exits 0.")
    parser.add_argument("--no-stagnation-stop", action="store_true",
                        help="Ignore the stagnation gate; each coordinator "
                             "runs exactly --iterations iterations.")
    parser.add_argument("--establish-baselines", action="store_true")
    args = parser.parse_args()

    keywords = load_keywords()
    if args.establish_baselines:
        ensure_daemon_up()
        establish_baselines(keywords)
        return 0

    baselines = load_baselines()
    if not baselines:
        _log("ERROR: no baselines. Run --establish-baselines first.")
        return 2
    if set(baselines) != {kw["slug"] for kw in keywords}:
        _log(f"ERROR: keyword set mismatch. baselines={set(baselines)}, "
             f"cfg={[kw['slug'] for kw in keywords]}")
        return 2

    ensure_daemon_up()
    if not args.dry_run:
        res = profile_freshness.ensure_profiles_fresh()
        if res["action"] == "failed":
            raise RuntimeError(f"profile_freshness failed: {res}")

    WORKTREE_BASE.mkdir(parents=True, exist_ok=True)
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)

    slugs = [kw["slug"] for kw in keywords]
    streaks = _load_streaks(slugs) if args.resume else {s: 0 for s in slugs}
    streak_lock = threading.Lock()

    # Effective per-coordinator stagnation ceiling. --no-stagnation-stop
    # disables the early exit by setting it past any reachable streak.
    effective_max_stagnation = (10**9 if args.no_stagnation_stop
                                 else args.max_stagnation)

    def _on_commit(slug_committed: str, iter_no: int, real_commit: bool) -> None:
        with streak_lock:
            ls = _read_loop_state()
            ls["stagnation_streaks"] = dict(streaks)
            ls["last_commit_sha"] = _git_head_sha()
            ls["completed_iters"] = iter_no
            _write_loop_state(ls)

    merge_queue = MergeQueue(baselines, args.dry_run, on_commit=_on_commit)
    merge_queue.start()

    stop_event = threading.Event()
    threads: list[threading.Thread] = []
    for slug in slugs:
        t = threading.Thread(
            target=worker_coordinator,
            args=(slug, baselines[slug], args.iterations,
                  effective_max_stagnation, merge_queue, args.dry_run,
                  stop_event, streaks, streak_lock),
            name=f"coord-{slug}", daemon=True,
        )
        t.start()
        threads.append(t)

    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        _log("interrupted; signalling workers to stop")
        stop_event.set()
        for t in threads:
            t.join(timeout=30)

    merge_queue.shutdown(timeout_s=60)
    _log(f"=== ALL COORDINATORS DONE; streaks={streaks} ===")
    print(json.dumps({"final_streaks": streaks, "mode": "async"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
