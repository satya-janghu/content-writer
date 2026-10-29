#!/usr/bin/env bash
# run_until_done.sh — autonomous outer wrapper for the autoresearch production run.
#
# Design: parallel_loop.py is the worker. This script restarts it whenever it
# exits with a retryable code (non-zero that isn't "fatal config error"),
# respawning the daemon between attempts. Stops cleanly on:
#   - exit 0   : parallel_loop converged or hit the iteration ceiling
#   - exit 2   : fatal config problem (missing baselines, keyword mismatch)
#   - exit 130 : SIGINT (user hit Ctrl-C)
#   - MAX_RESTARTS hit
#
# Retryable codes include:
#   - exit 3   : watchdog alert (legacy; async mode rarely produces this)
#   - any other non-fatal exit (segfault, OOM, etc.)
#
# Note: exit 4 (--batch-timeout) is no longer produced. parallel_loop.py now
# runs an async per-keyword supervisor; batches no longer exist. Per-iter
# timeouts are enforced inside propose + regen instead.
#
# Usage:
#   scripts/run_until_done.sh [--iterations N] [--max-stagnation N]
#                             [--max-restarts N]
#
# Defaults:
#   --iterations      10000  (effectively unlimited; convergence gates the stop)
#   --max-stagnation  10     (project convergence rule)
#   --max-restarts    500    (upper bound on restart loops; designed for
#                             multi-day unattended runs — at worst-case
#                             300s backoff per restart that's ~42 hours of
#                             pure backoff, which dominates only if every
#                             attempt fails instantly; real-world cadence
#                             is much lower)
#
# The log at /tmp/autoresearch-run.log is append-only JSON lines for
# post-hoc analysis. Every restart prints its reason there.

set -u
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

# --- defaults / arg parsing ---
ITERATIONS=10000
MAX_STAGNATION=10
MAX_RESTARTS=500

while [[ $# -gt 0 ]]; do
    case "$1" in
        --iterations)      ITERATIONS="$2"; shift 2;;
        --max-stagnation)  MAX_STAGNATION="$2"; shift 2;;
        --max-restarts)    MAX_RESTARTS="$2"; shift 2;;
        *) echo "unknown arg: $1" >&2; exit 2;;
    esac
done

RUN_LOG="/tmp/autoresearch-run.log"
DAEMON_LOG="/tmp/autoresearch-eval-server.log"
SOCKET="/tmp/content-writer-eval.sock"

log_event() {
    # JSON line: {ts, event, ...}
    local ts; ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '{"ts":"%s","event":"%s","msg":%s}\n' "$ts" "$1" "$(jq -Rs . <<< "$2" 2>/dev/null || printf '"%s"' "$2")" >> "$RUN_LOG"
}

ensure_daemon() {
    # Start eval_server if no socket. Wait up to 90s for warmup.
    if [[ -S "$SOCKET" ]] && kill -0 "$(pgrep -f eval_server.py | head -1)" 2>/dev/null; then
        log_event "daemon-already-up" "socket=$SOCKET"
        return 0
    fi
    # Clean up any stale socket / dead daemon
    pkill -9 -f eval_server.py 2>/dev/null || true
    rm -f "$SOCKET"
    log_event "daemon-starting" "spawning eval_server.py"
    # shellcheck disable=SC2086
    nohup "$ROOT/.venv/bin/python" "$ROOT/scripts/eval_server.py" \
        >> "$DAEMON_LOG" 2>&1 &
    for _ in $(seq 1 90); do
        sleep 1
        if [[ -S "$SOCKET" ]]; then
            log_event "daemon-ready" "socket=$SOCKET"
            return 0
        fi
    done
    log_event "daemon-warmup-timeout" "socket never appeared after 90s"
    return 1
}

kill_daemon() {
    pkill -TERM -f eval_server.py 2>/dev/null || true
    sleep 2
    pkill -KILL -f eval_server.py 2>/dev/null || true
    rm -f "$SOCKET"
}

source "$ROOT/.venv/bin/activate"

log_event "run-start" "iters=$ITERATIONS stagnation=$MAX_STAGNATION"

attempt=0
last_rc=0
while [[ $attempt -lt $MAX_RESTARTS ]]; do
    attempt=$((attempt + 1))
    log_event "attempt" "$attempt of $MAX_RESTARTS"

    ensure_daemon || { log_event "daemon-fatal" "giving up"; exit 5; }

    python "$ROOT/scripts/parallel_loop.py" \
        --resume \
        --iterations "$ITERATIONS" \
        --max-stagnation "$MAX_STAGNATION"
    last_rc=$?

    log_event "parallel_loop-exit" "rc=$last_rc"

    case "$last_rc" in
        0)
            log_event "converged-or-ceiling" "clean exit"
            exit 0
            ;;
        2)
            log_event "fatal-config-error" "baselines or keyword mismatch; human intervention required"
            exit 2
            ;;
        130)
            log_event "interrupted" "SIGINT received; stopping outer loop"
            exit 130
            ;;
        3|*)
            # 3 = watchdog alert (legacy), anything else = crash
            # Restart the daemon (it may be the stuck component) and retry with --resume.
            log_event "retrying" "rc=$last_rc; killing daemon and restarting after backoff"
            kill_daemon
            # Backoff: 30s, 60s, 120s, capped at 300s.
            backoff=$(( 30 * attempt ))
            if [[ $backoff -gt 300 ]]; then backoff=300; fi
            log_event "backoff" "sleeping ${backoff}s"
            sleep "$backoff"
            ;;
    esac
done

log_event "max-restarts" "attempt=$attempt rc=$last_rc; giving up"
exit "$last_rc"
