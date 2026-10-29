# Autoresearch Runbook

Operational guide for starting, continuing, and debugging an autoresearch session. It is the single source of truth for how to run, resume, and reason about the content-framework autoresearch loop.

## What this project does

Optimise a universal content-generation skill (`rules/content-generation/SKILL.md`) so that, given any one-sentence topic prompt, the generated article scores well against a competitive SERP benchmark across four test keywords.

- **The skill is the product.** Content files are disposable test output.
- **Success** = skill improvements that raise the composite score on **every** keyword independently (per-keyword gate, never cross-keyword averaging).

## Baselines and keywords

Baselines live in `autoresearch-state.json.baselines` and are populated by a one-time `parallel_loop.py --establish-baselines` run. The production driver refuses to start without a baselines block. See `docs/ARCHITECTURE.md` for the scoring model and `docs/decisions.md` for the history of invalidation events.

The four test keywords are defined in `data/keywords.json`: `seo-for-roofing`, `seo-for-saas`, `aeo-vs-seo`, `search-intent`.

## Architecture in one picture

```
                    +--------------------+
                    | eval_server.py     |    long-lived daemon
                    |  - BART, BGE, …    |    owns CUDA context
                    |  - score_cache     |    serves UNIX socket
                    +----------^---------+
                               |
        +----------------------+----------------------+
        |                      |                      |
  iter_worker.py         evaluate.py (CLI)     parallel_loop.py
  spawned per-iter       one-shot scoring       async supervisor
  by each of 4                                  (1 coord thread/kw
  coordinator threads                            + 1 merge thread)
        |                                           |
        v                                           v
  proposal JSON   ---------> MergeQueue (FIFO)  ->  apply + commit
  (one per worker run)                             (one keeper
                                                    at a time)
```

Each coordinator loops `{refresh worktree → spawn iter_worker → wait → read proposal → submit if post_composite > baseline → update streak}` independently. The merge thread drains keepers serially; workers never touch master. There are no batches: nothing waits on anything beyond one worker's own propose + regen + score.

## The immutable boundary

The mutable and immutable layers are described in `docs/CONTRIBUTING.md`. In short: during a live loop run only `rules/content-generation/SKILL.md` is mutable; `scripts/*.py`, `data/**`, `banned-words.md` and `content/*.md` are frozen (content changes only through regeneration). See docs/CONTRIBUTING.md.

## First-run bootstrap

```bash
git clone <repo> && cd content-writer
bash scripts/install_deps.sh              # creates .venv, installs requirements.txt
cp .env.example .env                      # then set GEMINI_API_KEY
source .venv/bin/activate
python scripts/scrape_competitors.py      # fetches the competitor articles into data/competitors/
python scripts/analyze_competitors.py     # builds data/profiles/ (about 3 min on GPU; calls Gemini for GEO simulation queries)
python scripts/doctor.py                  # full check (includes live evaluate smoke; ~90 s)
```

`scripts/doctor.py` exits non-zero on any hard failure. Resolve anything red before proceeding.

## Operator sequence

### 1. Doctor

```bash
python scripts/doctor.py
```

Must exit 0. Common failures:
- `.env` missing `GEMINI_API_KEY`: the key is mandatory because propose and regen both call Gemini.
- Stale `eval_server.py` holding the GPU: `pkill -f eval_server.py; rm -f /tmp/content-writer-eval.sock`.
- Missing profiles: `python scripts/analyze_competitors.py` (about 3 min).

### 2. Ping the Gemini backend

```bash
python scripts/bench_gemini_vs_opus.py --ping
```

Expect two JSON lines, both `"ok": true`, for `gemini-3-flash-preview` and `gemini-3.1-pro-preview`, each answering in about 10 s or less. If either returns 404 or 403, stop and fix the model access before continuing.

### 3. Inspect or establish baselines

```bash
# Inspect the current baselines block.
python -c "
import sys; sys.path.insert(0, 'scripts'); import json, score_cache
state = score_cache.read_state()
print(json.dumps({'baselines': state.get('baselines'), 'meta': state.get('baselines_meta')}, indent=2))
"
```

If `baselines` is non-empty and `baselines_meta.weights_epoch` matches the current `v3.7-no-judge` epoch, continue to step 4. Otherwise re-establish:

```bash
# Make sure no stale daemon is holding old model state:
pkill -f eval_server.py 2>/dev/null; rm -f /tmp/content-writer-eval.sock
nohup python scripts/eval_server.py > /tmp/autoresearch-eval-server.log 2>&1 &
for _ in $(seq 1 90); do [[ -S /tmp/content-writer-eval.sock ]] && break; sleep 1; done

python scripts/parallel_loop.py --iterations 1 --establish-baselines
```

The four composites printed on stderr become the live baselines; every keep for the rest of the run must clear them.

### 4. Launch the autonomous run

```bash
nohup bash scripts/run_until_done.sh --iterations 10000 --max-stagnation 10 --max-restarts 500 > /tmp/autoresearch-run.stdout 2>&1 &
echo "run launched; pid=$!"
```

These flags match the `run_until_done.sh` defaults. The run survives multi-day operation and hundreds of transient crash-and-restart cycles before giving up.

### 5. Monitor (every 30 minutes, or on demand)

```bash
# Outer wrapper status
if pgrep -f run_until_done.sh > /dev/null; then
    echo "still running"
else
    echo "outer wrapper exited"
    tail -5 /tmp/autoresearch-run.log
fi

# Progress
wc -l autoresearch-results.tsv
tail -10 autoresearch-results.tsv

# Git-history view of accepted rule changes
git log --oneline | head -20

# Current loop state (completed_iters, stagnation_streaks)
python -c "
import sys; sys.path.insert(0,'scripts'); import json, score_cache
loop = score_cache.read_state().get('loop', {})
print(json.dumps(loop, indent=2))
"

# Recent restarts / stall events
tail -20 /tmp/autoresearch-run.log
```

Things to note at each check-in: keep commits since the last check, per-keyword streaks (they advance per coordinator, so one keyword can be at streak 8 while another is at 1), any restart events, and an ETA to convergence (the stop condition is every keyword reaching `max_stagnation=10`).

### 6. On completion: exit codes

- **Exit 0** (converged): list accepted rule changes with `git log --oneline` and read the final `autoresearch-results.tsv` tail for the keep count per keyword. Compare final baselines with prior baselines dimension by dimension, and note any dimension where headroom is above 0.02 as a candidate for phase 2 (stronger generator).
- **Exit 2** (fatal config): stop and fix the underlying issue before restarting.
- **Exit 5** (daemon could not start): likely a GPU or driver problem.
- **Any other exit**: the wrapper hit `max-restarts`. Inspect `/tmp/autoresearch-run.log` for the failure pattern. Common Gemini-specific causes: `429 RESOURCE_EXHAUSTED` (quota; usually self-resolves within the minute), `503 UNAVAILABLE` (API hiccup), repeated timeouts (the model spending too long thinking on a pathological prompt).

## The loop driver

```bash
# Autonomous outer wrapper: restarts daemon + parallel_loop on any non-fatal exit,
# logs to /tmp/autoresearch-run.log.
bash scripts/run_until_done.sh --iterations 10000 --max-stagnation 10 --max-restarts 500

# Inner driver (what run_until_done.sh calls). Usable directly for debugging:
python scripts/parallel_loop.py --resume --iterations N --max-stagnation 10

# One-time baseline establishment (required after any scoring-semantic change:
# judge swap, weight change, cache-epoch bump). Writes to state.json.baselines.
python scripts/parallel_loop.py --iterations 1 --establish-baselines

# Infrastructure dry-run (no real content regen, no commits).
python scripts/parallel_loop.py --iterations 5 --dry-run
```

The driver:

1. Calls `scripts/profile_freshness.ensure_profiles_fresh()`, which skips the 3-minute `analyze_competitors.py` rebuild if nothing relevant changed.
2. Ensures `eval_server.py` is running (spawns it if not, waits for the socket, about 30 s warm-up cold).
3. Starts one `worker_coordinator` thread per keyword (4 threads) and one `MergeQueue` drain thread:
   a. Coordinator: refresh its worktree under `.autoresearch/worktrees/<slug>/` → spawn `iter_worker.py` → wait (cooperative on `stop_event`) → read proposal → submit to merge queue if `post_composite > baseline` → update its per-keyword stagnation streak → repeat.
   b. Merge thread: pop keeper from queue → `apply_proposal_to_master` → `bg_commit.commit_in_background` (or revert on dry-run) → repeat. No regen-all and no cross-keyword gate; the worker's local gate is sufficient (decisions #42).
4. The supervisor joins all 4 coordinator threads. When every coordinator has stagnated for at least `--max-stagnation` iterations or hit `--iterations`, it shuts the merge queue down and returns rc=0.

State is checkpointed to `autoresearch-state.json.loop` after every commit (the merge thread's `on_commit` callback). `--resume` reloads per-keyword streaks; no in-flight replay is needed because there are no batches. Stale worktrees are pruned by `_refresh_worktree` on every coordinator iteration regardless of resume state.

Per-iteration wall-clock is bounded by the propose timeout (1200 s) and regen timeout (1200 s), enforced in `gemini_backend` (`PROPOSE_TIMEOUT_S`, `REGEN_TIMEOUT_S`). If a coordinator's worktree refresh fails 5 consecutive times, that coordinator stops (logged as `COORDINATOR_REFRESH_GIVE_UP`). Under KeyboardInterrupt or supervisor teardown, `stop_event` is set and any in-flight iter_worker gets SIGKILL'd via its process group.

## The iteration gate (critical correctness rule)

Every "keep" is gated by the **per-keyword local baseline check** inside `iter_worker.py`: after the worker regenerates its own keyword's article under its own candidate rule edit, it writes a proposal JSON only if `post_composite > baseline` for that keyword. The merge queue trusts that gate and commits every arriving keeper without re-regenerating the other three keywords (decision #42: the cross-keyword gate was removed after evidence that it rejected genuine improvements over scoring-noise regressions, at a cost of 1290 s per keep).

If a rule edit for one keyword degrades another keyword over many iterations, the degraded keyword's next pre-score catches it (`post_composite` is measured against the now-drifted master HEAD) and its proposer edits in the corrective direction. The loop self-corrects through iteration, not upfront validation.

Keywords are independent axes. No averaging, no ranking, no "overall" score.

## Rollback commands

```bash
# Revert all rule edits + content to HEAD:
git checkout HEAD -- rules/content-generation/SKILL.md content/

# Clear all worktrees + proposals (if a crash left them around):
git worktree prune
rm -rf .autoresearch/worktrees .autoresearch/proposals

# Flush the daemon's score cache:
python -c "from eval_client import flush_cache; print(flush_cache())"
```

## Resume after interruption

- **Process killed or machine reboot**: run `python scripts/parallel_loop.py --resume --iterations <target>`. If the daemon is not up it will be spawned; each coordinator's next `_refresh_worktree` call prunes and recreates its worktree from the current master HEAD, so no residual mid-iteration state matters. `--resume` only reloads per-keyword stagnation streaks from `state.json.loop.stagnation_streaks`.
- **Daemon crashed but driver alive**: the client falls back to in-process scoring automatically (about 60 s slower per article). Restart the daemon with `python scripts/eval_server.py &` when convenient.
- **State.json corrupt**: delete it and re-run `python scripts/parallel_loop.py --iterations 1 --establish-baselines`.

## Observability

- `autoresearch-results.tsv`: one row per proposal (keep, dry-keep, reject). TSV so it is easy to `awk` or `cut`.
- git log: one commit per kept rule change. `git log --oneline | wc -l` tells you how many improvements landed.
- `scripts/watchdog.py --once`: probe daemon, GPU VRAM, disk, state.json. Run it out-of-band to debug.
- `/tmp/autoresearch-watchdog.log`: append-only JSON-line heartbeat when `watchdog.py` runs continuously alongside the loop.
- `/tmp/parallel_loop-worker-<slug>.log`: per-keyword worker log, appended to by every iter_worker invocation (grows across iterations; truncate out-of-band if disk is tight). Contains structured `slog` lines for every timing-critical boundary (WORKER_START, PRE_SCORE, PROPOSE_START/END, REGEN_START/END, POST_SCORE, WORKER_END). Grep for `iter_worker:<slug>` to isolate one worker's timeline.
- Supervisor-side events in the `parallel_loop:<slug>` stream: `COORDINATOR_START`, `ITER_START`, `ITER_END`, `PROPOSAL_SUBMITTED`, `COORDINATOR_STAGNATED`, `COORDINATOR_REFRESH_GIVE_UP`, `ITER_KILLED_BY_STOP_EVENT`, `COORDINATOR_END`. And `parallel_loop:merge`: `MERGE_QUEUE_START`, `MERGE_PROCESS`, `MERGE_APPLIED`, `MERGE_QUEUE_END`, `DRAIN_EXIT`, `SUBMIT_AFTER_CLOSE`.
- `/tmp/parallel_loop-eval_server.log`: daemon stderr. Contains structured `slog` lines for each scoring op: `SCORE_DIM` per dimension with elapsed seconds, `SCORE_COMPLETE` with composite + total elapsed, `OP_OK` / `OP_FAIL` per request. Grep for `evaluate:<slug>` to see per-dim timing on a specific keyword.
- `/tmp/autoresearch-run.log`: structured JSONL log from `run_until_done.sh` (daemon lifecycle, restart events).
- **Grepping the live log.** All structured log lines have the shape `[UTC-timestamp] [component:context] EVENT key=value ...`. Common queries: `grep "PROPOSE_END" /tmp/parallel_loop-worker-*.log` (which proposals produced edits?), `grep "GATE_RESULT" /tmp/parallel_loop-worker-*.log` (which keepers passed or failed?), `grep "SCORE_DIM.*elapsed" /tmp/parallel_loop-eval_server.log` (which dim is slowest?).

## Tests + green-light criteria

Before a long production run, verify in order:

1. `pytest tests/ -v` must be all green.
2. `python scripts/doctor.py` must exit 0.
3. 5-iteration dry run (`python scripts/parallel_loop.py --iterations 5 --dry-run`); `tests/test_async_workers.py` runs a 3-iteration version when the repo is a git checkout.
4. Real-mode 2-iteration smoke: `python scripts/parallel_loop.py --iterations 2`.
5. Checkpoint/resume round-trip: SIGINT the supervisor, verify coordinators exit via `COORDINATOR_END reason=killed_by_stop_event`, re-run with `--resume`, and verify streaks continued from `state.json.loop.stagnation_streaks`.

## Gemini quota + RPM

Gemini tier used during development:

| Model | RPM | TPM |
|---|---|---|
| `gemini-3-flash-preview` | 1000 | 2M |
| `gemini-3.1-pro-preview` | 25 | 2M |

The loop uses 4 async coordinators. Worst-case call rate per minute:

- Regen (Flash): 4 coordinators x (1 regen / about 2-4 min per iteration) is roughly 1-2 calls/min against a 1000 RPM ceiling. No pressure.
- Propose (Pro): the same 1-2 calls/min against a 25 RPM ceiling. Comfortable margin. `gemini_backend.rate_gate_pro()` adds a 3 s file-based floor between Pro calls as defensive coordination.

TPM is not a realistic concern: per-call totals top out around 80K (Flash with thinking) and 30K (Pro with HIGH thinking).

## Timeouts: per-call ceilings, not total runtime

**The loop is designed to run for multiple days until convergence.** There is no total-runtime cap anywhere in the stack. `--iterations 10000` is effectively unlimited; convergence is gated by per-keyword stagnation (10 consecutive non-keep iterations per keyword, all 4 keywords must trip).

Per-call ceilings in `scripts/gemini_backend.py`:

- `REGEN_TIMEOUT_S = 1200` (20 min; 5x the observed Flash max of 242 s).
- `PROPOSE_TIMEOUT_S = 1200` (20 min; 5x the observed Pro 3.1 max of 211 s).

A single timeout failure drops that iteration; the coordinator retries on the next one. Cross-iteration recovery is the job of the `max-restarts` outer wrapper (500 restarts; at worst-case 300 s backoff each, about 42 hours of pure backoff before giving up).

No hidden per-iteration ceilings exist elsewhere:
- `parallel_loop.py` coordinators use `WAIT_POLL_S = 2.0` for stop-event cooperativeness only, with no hard kill.
- `iter_worker.py` has no timeout on the worker body; it is bounded by propose + regen + scoring (daemon `REQUEST_TIMEOUT_S = 600 s`, though scoring takes about 15-30 s in practice).
- `run_until_done.sh` has no total-runtime cap, only `MAX_RESTARTS=500`.

## Safety rails

- **Do not hand-edit the skill file** at `rules/content-generation/SKILL.md` while the loop is running. The only legitimate edits come from `propose.py` inside worker worktrees.
- **Do not hand-edit content files** in `content/`. Regeneration comes from `regen.py` via the merge queue.
- **Do not touch `scripts/*.py`** during a run. The harness is frozen in loop mode (see docs/CONTRIBUTING.md).
- **Do not kill the daemon directly** unless you are also stopping the outer wrapper. Killing the daemon mid-iteration forces workers to fall back to in-process scoring (slow) or fail.
- **Do not SIGKILL the supervisor.** SIGTERM or Ctrl-C sets `stop_event`, which lets each coordinator finish or SIGKILL its current worker within about 2 s. A SIGKILL on the supervisor leaves iter_worker subprocesses orphaned under init (process-group separation in `_spawn_worker` makes them reapable from outside, but a clean TERM is better).

Clean shutdown:

```bash
pkill -f run_until_done.sh
pkill -TERM -f parallel_loop.py
sleep 5
pkill -TERM -f eval_server.py
pkill -f iter_worker.py
```

## Expected wall-clock

- Baseline establishment: about 3-5 min (4 keywords x 30-60 s each via daemon-cached scoring; no regen).
- Per-iteration wall-clock under Gemini (from the 2026-04-16 bench, `docs/benchmarks/gemini-vs-opus.md`):
  - Regen (Flash): avg 143 s, p95 242 s.
  - Propose (Pro 3.1): avg 150 s, max 211 s on a partial bench.
  - With pre-score, post-score and merge overhead (about 30 s), expect **about 4-8 min per iteration**.
  - Coordinators run asynchronously, so the loop is not bounded by the slowest keyword.
- Convergence: **hard to predict; plan for days, not hours.** Per-keyword streaks advance independently, so one keyword may converge while another is still finding wins. The hard stop is all 4 keywords reaching 10 consecutive non-keep iterations.

Quota over a multi-day horizon (Pro is the tight one): 25 RPM x 60 min x 24 h x 3 days is about 108,000 possible Pro calls per 3-day window against an expected 2,000-4,000. If your Gemini tier has a daily quota, a multi-day run can hit it. The signal is `slog` entries showing `FAIL reason="quota"` or `429 RESOURCE_EXHAUSTED` on consecutive iterations with no self-recovery within 30 minutes. That indicates a daily rather than per-minute quota: stop the loop, wait for the daily rollover, and resume with `--resume`.

## If something looks wrong

- Same keyword stuck at a high streak (above 8) while others advance: that keyword may have saturated. Worth noting, not actionable mid-run.
- Multiple iterations in a row on a single keyword producing rc=12 ("rule proposer made no usable edit"): if it persists, the propose prompt or the per-dim semantics need another look; stop and diagnose before continuing.
- `git status` showing dirty `content/*.md` outside regen windows: something leaked from a worker worktree. Revert immediately.
- `/tmp/parallel_loop-worker-<slug>.log` showing repeated `WORKTREE_REFRESH_FAIL` events: a git pathology (lock file, disk full, corrupted object store). Investigate before that coordinator hits `COORDINATOR_REFRESH_GIVE_UP` and stops.
- `slog` lines for propose or regen with `backend=gemini-pro` or `backend=gemini-flash` showing repeated `FAIL` or `EMPTY_RESPONSE`: likely a Gemini-side issue (quota, model outage). Check Google's status page before assuming a local failure.
- Watchdog alert file present: the outer wrapper should already be handling it, but confirm `run_until_done.sh` is actually running.

## Decision log pointers

All non-obvious trade-offs live in `docs/decisions.md`. Key entries:
- #26 Fedora port + GPU best-config
- #27 BART `tokenizer.model_max_length` pinning (keeps inputs within the model's token limit)
- #28 autonomous run harness + `state.json.baselines` + `run_until_done.sh`
- #30 Gemini judge model ID correction + ThreadPoolExecutor timeout wrapper
- #33 No neutral-0.5 fallbacks anywhere: scorers raise or succeed
- #34 Proposer target selection: max composite headroom (diagnostic only after #38)
- #35 v3.4 scoring-resolution fixes (P1-P4 pooling and depth)
- #37 v3.5: GEO dims removed from scoring; rules kept in generation
- #38 Proposer realigned with autoresearch design: full-context prompt; the model picks the lever and makes the edit
- #39 v3.6-highest-bar: tightened scoring ceilings (P1-P4 resolution raised); baselines re-established
- #41 Single-pass skill consolidation: 13 rule files into one SKILL.md; proposer targets the single file
- #42 Architectural speedup: remove llm_judge dim, remove cross-keyword merge-queue gate, async per-keyword supervisor, keep Opus (Sonnet bench rejected)
- #43 Generation backend moved to Gemini (Pro proposes, Flash regenerates)
