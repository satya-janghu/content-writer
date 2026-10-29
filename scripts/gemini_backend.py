"""
gemini_backend.py — shared Gemini SDK wrapper used by propose.py + regen.py.

The production autoresearch loop now runs against Gemini models (decision #43
in docs/decisions.md):

  - regen  → gemini-3-flash-preview    (fast; infrastructure speed > quality)
  - propose→ gemini-3.1-pro-preview    (reasoning model; does the ideation)

This module owns:
  - one lazy `genai.Client` per process (both regen.py and propose.py import
    from here, same process under iter_worker.py — one client is reused),
  - `call()` — one generate_content with a ThreadPoolExecutor hard-timeout
    wrapper, because google-genai's http_options.timeout is silently coerced
    to None internally (python-genai #911, #1893). Same pattern evaluate.py's
    dormant judge path uses.
  - `rate_gate_pro()` — a file-based cross-process spacing gate for the Pro
    model (25 RPM = 2.4s floor). Flash is 1000 RPM so needs no gate; the
    actual call rate under 4 async workers is far below either ceiling.
  - Model IDs + timeouts as module constants so they're one edit away from
    future model swaps (e.g. back to Opus when loop 1 converges).

Not a public module for anything but the loop. The bench script
(scripts/bench_gemini_vs_opus.py) uses these same helpers so bench + prod
can't drift.
"""
from __future__ import annotations

import concurrent.futures
import fcntl
import os
import time
from pathlib import Path

from dotenv import load_dotenv

# Load once at import so subprocess children (iter_worker.py) that don't
# otherwise call load_dotenv() still see GEMINI_API_KEY.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


# --- Model IDs (verified via direct API probe 2026-04-16) ---

# Content regeneration. Flash is "fast + inferior" by design — the loop's
# current phase uses it to iterate rule changes cheaply. Once the stagnation
# gate trips on all 4 keywords, switch this to a stronger generator (e.g.
# gemini-3-pro-preview or Opus 4.6) and
# re-baseline.
FLASH_MODEL = "gemini-3-flash-preview"

# Rule proposal. Pro 3.1 is the reasoning model — the propose prompt needs
# ideation (picking which dim to act on + how), not just execution.
PRO_MODEL = "gemini-3.1-pro-preview"


# --- Timeouts (generous per-call ceilings for a multi-day unattended run) ---
#
# These are PER-CALL wall-clock ceilings enforced by the ThreadPoolExecutor
# wrapper. They don't bound total run time — the loop is expected to run
# for days until per-keyword stagnation converges. They only catch stuck
# calls so one hang can't freeze a coordinator indefinitely.
#
# Observed max on the 2026-04-16 Gemini bench:
#   Flash regen:  242s (4 samples)
#   Pro 3.1 propose: 211s (2 samples on 3.1, partial rerun)
# Setting both ceilings to 1200s = 5× observed max. Rationale: model
# behaviour at inference-time is not stationary — prompt pathologies,
# thinking-budget spikes, or transient Google-side latency can produce
# occasional calls in the 10-15 minute range without being "hung". At
# 1200s we lose at most one iteration's wall-clock to a real hang before
# the wrapper times out; 500s was catching legitimate-but-slow calls and
# dropping iterations the loop would otherwise have kept.
REGEN_TIMEOUT_S = 1200
PROPOSE_TIMEOUT_S = 1200


# --- Rate limits (per the Gemini API tier in use) ---

# Pro: 25 RPM → 2.4s between calls.
# Flash: 1000 RPM → no practical ceiling for this workload.
PRO_MIN_SPACING_S = 3.0  # 3.0 not 2.4 to keep safety headroom.
PRO_RATE_GATE_PATH = Path("/tmp/autoresearch-gemini-pro-last-call.lock")


_CLIENT = None


def get_client():
    """Lazy-construct one google-genai Client for this process. Reused by
    propose and regen (they run in the same iter_worker process)."""
    global _CLIENT
    if _CLIENT is None:
        from google import genai
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY not set. Check .env at project root."
            )
        _CLIENT = genai.Client(api_key=key)
    return _CLIENT


def rate_gate_pro() -> None:
    """Enforce PRO_MIN_SPACING_S between Pro calls across processes.

    4 async coordinators × 1 propose per iteration = effectively well under
    25 RPM, but they could in principle bunch up. fcntl.flock + a shared
    timestamp file prevents 2 coordinators from firing a Pro call within
    PRO_MIN_SPACING_S of each other.
    """
    PRO_RATE_GATE_PATH.touch(exist_ok=True)
    with open(PRO_RATE_GATE_PATH, "r+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            raw = fh.read().strip()
            last = float(raw) if raw else 0.0
            gap = time.time() - last
            if gap < PRO_MIN_SPACING_S:
                time.sleep(PRO_MIN_SPACING_S - gap)
            fh.seek(0)
            fh.truncate()
            fh.write(str(time.time()))
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def call(model: str, prompt: str, timeout_s: int,
         thinking_level: str | None = None) -> dict:
    """Make one generate_content call with a ThreadPoolExecutor hard timeout.

    `thinking_level` is Gemini-3 specific: None = SDK default,
    "MINIMAL" | "LOW" | "MEDIUM" | "HIGH".

    Returns:
        {
          "ok": bool,
          "elapsed_s": float,
          "text": str,             # "" on failure
          "prompt_token_count": int | None,
          "candidates_token_count": int | None,
          "thoughts_token_count": int | None,
          "error": str | None,
        }

    Why ThreadPoolExecutor: google-genai's http_options.timeout is silently
    overridden to None inside the SDK (python-genai #911, #1893; pydantic-ai
    #4031), so requests can hang indefinitely at the TCP layer. The only
    reliable enforcement is running the SDK call in a worker thread and
    calling future.result(timeout=...). On timeout we shut the executor
    down with wait=False; the underlying thread lingers until the OS drops
    the TCP connection but doesn't block the main flow.
    """
    client = get_client()
    config: dict = {"temperature": 0.0, "response_mime_type": "text/plain"}
    if thinking_level is not None:
        from google.genai import types as genai_types
        config["thinking_config"] = genai_types.ThinkingConfig(
            thinking_level=getattr(genai_types.ThinkingLevel, thinking_level)
        )

    t0 = time.time()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(
            client.models.generate_content,
            model=model,
            contents=prompt,
            config=config,
        )
        try:
            resp = future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            return {
                "ok": False, "elapsed_s": time.time() - t0, "text": "",
                "prompt_token_count": None, "candidates_token_count": None,
                "thoughts_token_count": None,
                "error": f"timeout after {timeout_s}s",
            }
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False, "elapsed_s": time.time() - t0, "text": "",
                "prompt_token_count": None, "candidates_token_count": None,
                "thoughts_token_count": None,
                "error": f"{type(e).__name__}: {e}",
            }
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    usage = getattr(resp, "usage_metadata", None)
    return {
        "ok": True,
        "elapsed_s": time.time() - t0,
        "text": resp.text or "",
        "prompt_token_count": getattr(usage, "prompt_token_count", None),
        "candidates_token_count": getattr(usage, "candidates_token_count", None),
        "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
        "error": None,
    }


def strip_markdown_fence(text: str) -> str:
    """If the model wrapped its response in a ```markdown ... ``` fence,
    peel the outermost fence. Preserves interior code blocks."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    if lines[0].lstrip("`").strip() in ("", "markdown", "md"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)
