"""
Structured logging for the autoresearch loop.

Every log line is a greppable, parseable record:
    [2026-04-16T01:23:45Z] [component:context] EVENT key=value key2=value2

Usage:
    from slog import slog
    slog("iter_worker", "seo-for-roofing", "PROPOSE_END",
         elapsed=387.0, exit_code=0, result="edit", file="SKILL.md")

Output goes to stderr (flush=True) so it interleaves with subprocess stderr
in the worker log files under /tmp/parallel_loop-worker-{slug}.log.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone


def slog(component: str, context: str, event: str, **kw) -> None:
    """Emit one structured log line to stderr.

    Args:
        component: module name (iter_worker, parallel_loop, eval_server, etc.)
        context:   qualifier (keyword slug, batch number, model name, etc.)
        event:     UPPER_CASE event tag (WORKER_START, PROPOSE_END, etc.)
        **kw:      arbitrary key=value pairs. Floats rounded to 4 decimal places.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts: list[str] = []
    for k, v in kw.items():
        if isinstance(v, float):
            parts.append(f"{k}={v:.4f}")
        elif isinstance(v, dict):
            # Inline dict as k1=v1,k2=v2 for greppability
            inner = ",".join(f"{ik}={iv:.4f}" if isinstance(iv, float)
                            else f"{ik}={iv}" for ik, iv in v.items())
            parts.append(f"{k}={{{inner}}}")
        elif isinstance(v, (list, tuple)):
            parts.append(f"{k}=[{','.join(str(x) for x in v)}]")
        else:
            parts.append(f"{k}={v}")
    tail = " ".join(parts)
    line = f"[{ts}] [{component}:{context}] {event} {tail}".rstrip()
    print(line, file=sys.stderr, flush=True)


class Timer:
    """Context manager that measures wall-clock seconds.

    Usage:
        with Timer() as t:
            do_work()
        slog("comp", "ctx", "DONE", elapsed=t.elapsed)
    """
    __slots__ = ("_t0", "elapsed")

    def __enter__(self) -> "Timer":
        self._t0 = time.monotonic()
        self.elapsed = 0.0
        return self

    def __exit__(self, *exc) -> None:
        self.elapsed = time.monotonic() - self._t0
