#!/usr/bin/env python3
"""
watchdog.py — liveness + resource probe for long autoresearch runs.

Runs alongside scripts/parallel_loop.py. Every `interval` seconds it
probes a small set of health signals. On N consecutive failures of the
same check it writes `.autoresearch/watchdog-alert.json` and exits so
the parallel loop can see the alert (it polls between batches).

Usage:
    python scripts/watchdog.py                         # defaults (60s, 3 strikes)
    python scripts/watchdog.py --interval 30 --strikes 5
    python scripts/watchdog.py --once                  # single probe + exit

Probes:
  - eval_server daemon reachable (ping the health op)
  - GPU memory ≥ 500 MiB free (nvidia-smi)
  - Disk free ≥ 1 GiB under the project root
  - autoresearch-state.json parses as JSON

Alert file schema:
    {
      "tripped_check": "daemon" | "gpu" | "disk" | "state",
      "detail": "<human-readable>",
      "strikes": N,
      "written_at": <epoch>
    }

Log: /tmp/autoresearch-watchdog.log (append-only JSON lines).
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
PROJECT_ROOT = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

ALERT_PATH = PROJECT_ROOT / ".autoresearch" / "watchdog-alert.json"
LOG_PATH = Path("/tmp/autoresearch-watchdog.log")
LOG_ROTATE_BYTES = 5 * 1024 * 1024  # rotate at 5 MiB; keep one .1 archive

DEFAULT_SOCKET = "/tmp/content-writer-eval.sock"


def _rotate_log_if_big() -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_ROTATE_BYTES:
            rotated = LOG_PATH.with_suffix(LOG_PATH.suffix + ".1")
            if rotated.exists():
                rotated.unlink()
            LOG_PATH.rename(rotated)
    except OSError:
        # Best-effort; if rotation fails we just keep appending.
        pass


def _log_line(obj: dict) -> None:
    obj["ts"] = int(time.time())
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log_if_big()
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


# ---- Probes. Each returns (ok: bool, detail: str) ----

def probe_daemon() -> tuple[bool, str]:
    try:
        import eval_client
        ok = eval_client.is_daemon_running()
        return ok, "reachable" if ok else f"no response on {eval_client.SOCKET_PATH}"
    except Exception as e:
        return False, f"probe failed: {e}"


def probe_gpu(min_free_mib: int = 500) -> tuple[bool, str]:
    smi = shutil.which("nvidia-smi")
    if not smi:
        return False, "nvidia-smi not on PATH"
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip().splitlines()
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"smi error: {e}"
    if not out:
        return False, "no GPU rows from smi"
    free = int(out[0].split(",")[0].strip())
    ok = free >= min_free_mib
    return ok, f"{free} MiB free (min {min_free_mib})"


def probe_disk(min_free_gib: float = 1.0) -> tuple[bool, str]:
    total, used, free = shutil.disk_usage(PROJECT_ROOT)
    free_gib = free / (1024 ** 3)
    ok = free_gib >= min_free_gib
    return ok, f"{free_gib:.1f} GiB free (min {min_free_gib})"


def probe_state_json() -> tuple[bool, str]:
    p = PROJECT_ROOT / "autoresearch-state.json"
    resume_hint = PROJECT_ROOT / ".autoresearch" / "resume-hint.md"
    if not p.exists():
        # A missing state.json mid-run is a real failure: the loop writes it
        # after every batch. Distinguish by the presence of a resume hint
        # (it exists only while a loop is active).
        if resume_hint.exists():
            return False, "state.json missing while an active loop was in progress"
        return True, "state.json missing (new run — OK)"
    try:
        json.loads(p.read_text(encoding="utf-8"))
        return True, "parses OK"
    except json.JSONDecodeError as e:
        return False, f"parse error: {e}"


PROBES = {
    "daemon": probe_daemon,
    "gpu":    probe_gpu,
    "disk":   probe_disk,
    "state":  probe_state_json,
}


def run_probes_once() -> dict[str, tuple[bool, str]]:
    results = {}
    for name, fn in PROBES.items():
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = (False, f"probe exception: {e}")
    return results


def write_alert(tripped: str, detail: str, strikes: int) -> None:
    ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ALERT_PATH.write_text(json.dumps({
        "tripped_check": tripped,
        "detail": detail,
        "strikes": strikes,
        "written_at": int(time.time()),
    }, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=60.0,
                        help="seconds between probe rounds (default: 60)")
    parser.add_argument("--strikes", type=int, default=3,
                        help="consecutive failures on one check before alerting (default: 3)")
    parser.add_argument("--once", action="store_true",
                        help="single pass; print result; exit")
    args = parser.parse_args()

    if args.once:
        results = run_probes_once()
        for name, (ok, detail) in results.items():
            print(f"  [{'✓' if ok else '✗'}] {name:10s} {detail}")
        return 0 if all(ok for ok, _ in results.values()) else 1

    fail_streak = {name: 0 for name in PROBES}
    while True:
        results = run_probes_once()
        for name, (ok, detail) in results.items():
            if ok:
                fail_streak[name] = 0
            else:
                fail_streak[name] += 1
                _log_line({"name": name, "ok": False, "detail": detail,
                           "streak": fail_streak[name]})
                if fail_streak[name] >= args.strikes:
                    write_alert(name, detail, fail_streak[name])
                    _log_line({"name": name, "alert": True, "detail": detail})
                    print(f"watchdog: ALERT — {name}: {detail}", file=sys.stderr)
                    return 1
        # periodic green heartbeat
        if all(ok for ok, _ in results.values()):
            _log_line({"heartbeat": True})
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
