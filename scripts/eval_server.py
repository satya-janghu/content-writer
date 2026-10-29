#!/usr/bin/env python3
"""
eval_server.py — long-lived evaluation daemon.

Owns the CUDA context and every ML model used by scoring. Speaks newline-
delimited JSON over a Unix domain socket. Built to eliminate the ~60 s
model-load cost every time `scripts/evaluate.py` starts.

Usage:
    source .venv/bin/activate
    python scripts/eval_server.py                    # run in foreground
    python scripts/eval_server.py --socket /tmp/foo  # override socket path
    python scripts/eval_server.py --background       # double-fork daemonize

    # on a client:
    python scripts/evaluate.py content/ --config data/keywords.json --verbose

The client side lives in `scripts/eval_client.py`. Protocol:

    Request:  {"op": "<name>", "args": {...}}\n
    Response: {"ok": true,  "result": {...}}\n
              or {"ok": false, "error": "<msg>", "traceback": "..."}\n

Ops:
    health        → {"pid": int, "models_loaded": [str]}
    score_article → args: content_path, profile_path, verbose
                    result: {composite, scores: {dim: float}, details: {dim: {}}}
    shutdown      → graceful exit after current request

Lifecycle:
    - Startup: pre-warms every model (so the first client request is hot).
    - Single-threaded: one request at a time. GPU models don't play nice with
      concurrent requests; parallelism lives at the iteration-worker layer.
    - Socket mode 0600 (owner-only).
    - On SIGTERM/SIGINT: removes socket and exits cleanly.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time
import traceback
from pathlib import Path

# Import the scoring module (which owns every model loader and scoring fn).
# We reach into its globals rather than re-declaring them to keep a single
# source of truth for scoring logic.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import evaluate as eval_mod  # noqa: E402
import score_cache  # noqa: E402

DEFAULT_SOCKET_PATH = "/tmp/content-writer-eval.sock"


def warmup() -> list[str]:
    """Pre-load every model so the first real request is hot.
    Returns the list of model names that loaded successfully."""
    from slog import slog as _slog
    loaded = []
    t0 = time.time()
    _slog("eval_server", "warmup", "START")

    models = [
        ("bge-large-en-v1.5", eval_mod.get_embed),
        ("bge-reranker-v2-m3", eval_mod.get_reranker),
        ("gliner_large-v2.1", eval_mod.get_ner),
        ("keybert", eval_mod.get_keybert),
        ("bart-large-cnn", eval_mod.get_summarizer),
    ]
    for name, loader in models:
        mt0 = time.time()
        loader()
        loaded.append(name)
        _slog("eval_server", "warmup", "MODEL_LOADED",
              model=name, elapsed=time.time() - mt0, cumulative=time.time() - t0)

    try:
        import torch
        if torch.cuda.is_available():
            free_gb = torch.cuda.mem_get_info()[0] / (1024**3)
            total_gb = torch.cuda.mem_get_info()[1] / (1024**3)
            _slog("eval_server", "warmup", "VRAM",
                  free_gb=free_gb, total_gb=total_gb)
    except Exception:
        pass

    _slog("eval_server", "warmup", "COMPLETE",
          models_loaded=len(loaded), elapsed=time.time() - t0)
    return loaded


# ---- Op handlers ----

def op_health(_args: dict, state: dict) -> dict:
    return {"pid": os.getpid(), "models_loaded": state["models_loaded"]}


def op_score_article(args: dict, _state: dict) -> dict:
    content_path = args["content_path"]
    profile_path = args["profile_path"]
    verbose = bool(args.get("verbose", False))
    use_cache = bool(args.get("use_cache", True))
    slug = args.get("slug")  # required when use_cache is True

    if not os.path.isabs(content_path):
        content_path = os.path.join(eval_mod.PROJECT_ROOT, content_path)
    if not os.path.isabs(profile_path):
        profile_path = os.path.join(eval_mod.PROJECT_ROOT, profile_path)

    # Try cache first
    key = None
    if use_cache and slug:
        key = score_cache.cache_key(content_path, profile_path)
        hit = score_cache.get_cached(slug, key)
        if hit is not None:
            # Return stored result as-is; annotate cache-hit for telemetry.
            hit = dict(hit)
            hit["_cache"] = "hit"
            return hit

    # Cache miss (or disabled) — do the real work
    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)

    result = eval_mod.evaluate_content(content_path, profile, verbose=verbose)

    if use_cache and slug and key is not None:
        # Persist a copy WITHOUT the _cache annotation (keeps stored shape stable).
        score_cache.put_cached(slug, key, result)
        result = dict(result)
        result["_cache"] = "miss"

    return result


def _load_profile_for_op(args: dict) -> tuple[str, dict]:
    """Shared path-resolve + profile-load used by GEO diagnostic ops."""
    content_path = args["content_path"]
    profile_path = args["profile_path"]
    if not os.path.isabs(content_path):
        content_path = os.path.join(eval_mod.PROJECT_ROOT, content_path)
    if not os.path.isabs(profile_path):
        profile_path = os.path.join(eval_mod.PROJECT_ROOT, profile_path)
    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)
    return content_path, profile


def op_score_geo_structural(args: dict, _state: dict) -> dict:
    """Deterministic GEO structural readiness (dim 12) only. No GPU, no Gemini.

    Exists for standalone rule-tuning / diagnostic use — the main score_article
    path already runs this dim as part of the 13-dim composite. The cache
    is NOT consulted for this op; dim 12 scoring is fast enough that caching
    adds more complexity than value.
    """
    content_path, profile = _load_profile_for_op(args)
    body = eval_mod.read_content_file(content_path)
    score, details = eval_mod.score_geo_structural_readiness(body, profile)
    return {"score": score, "details": details}


def op_flush_cache(args: dict, _state: dict) -> dict:
    """Flush one slug's cache or the entire article_scores dict."""
    slug = args.get("slug")
    if slug:
        removed = 1 if score_cache.flush_slug(slug) else 0
    else:
        removed = score_cache.flush_all()
    return {"removed": removed}


def op_shutdown(_args: dict, state: dict) -> dict:
    state["shutdown"] = True
    return {"goodbye": True}


OPS = {
    "health": op_health,
    "score_article": op_score_article,
    "score_geo_structural": op_score_geo_structural,
    "flush_cache": op_flush_cache,
    "shutdown": op_shutdown,
}


# ---- Server loop ----

def _read_request(conn: socket.socket) -> dict | None:
    buf = bytearray()
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in buf:
            break
    if not buf:
        return None
    first = buf.split(b"\n", 1)[0]
    return json.loads(first.decode("utf-8"))


def _write_response(conn: socket.socket, payload: dict) -> None:
    conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def serve(socket_path: str) -> int:
    # Clean up a stale socket if the previous server crashed without removing it.
    if os.path.exists(socket_path):
        try:
            # Quick probe: is another server listening?
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.2)
            probe.connect(socket_path)
            probe.close()
            print(
                f"eval_server: another daemon appears to be listening on {socket_path}; refusing to start",
                file=sys.stderr,
            )
            return 2
        except (ConnectionRefusedError, FileNotFoundError, socket.timeout):
            # Stale socket file with no server — remove it.
            try:
                os.unlink(socket_path)
            except OSError:
                pass

    state = {"shutdown": False, "models_loaded": []}
    state["models_loaded"] = warmup()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old_umask = os.umask(0o177)  # create the socket owner-only (no world-connectable window)
    try:
        srv.bind(socket_path)
    finally:
        os.umask(old_umask)
    os.chmod(socket_path, 0o600)
    srv.listen(4)
    print(f"eval_server: listening on {socket_path} (pid={os.getpid()})", file=sys.stderr, flush=True)

    def _cleanup(*_):
        state["shutdown"] = True
        try:
            srv.close()
        finally:
            if os.path.exists(socket_path):
                try:
                    os.unlink(socket_path)
                except OSError:
                    pass

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    try:
        while not state["shutdown"]:
            try:
                conn, _ = srv.accept()
            except OSError:
                break  # socket closed during shutdown
            try:
                req = _read_request(conn)
                if req is None:
                    _write_response(conn, {"ok": False, "error": "empty request"})
                    conn.close()
                    continue

                op_name = req.get("op")
                args = req.get("args", {}) or {}
                handler = OPS.get(op_name)

                if handler is None:
                    _write_response(conn, {"ok": False, "error": f"unknown op: {op_name!r}"})
                    conn.close()
                    continue

                from slog import slog as _slog
                slug_hint = args.get("slug", args.get("content_path", "?"))
                if isinstance(slug_hint, str) and "/" in slug_hint:
                    slug_hint = os.path.splitext(os.path.basename(slug_hint))[0]
                cache_hint = "n/a"
                t0 = time.time()
                try:
                    result = handler(args, state)
                    elapsed_s = time.time() - t0
                    _write_response(conn, {"ok": True, "result": result})
                    if isinstance(result, dict):
                        cache_hint = result.get("_cache", "n/a")
                    _slog("eval_server", slug_hint, "OP_OK",
                          op=op_name, elapsed=elapsed_s, cache=cache_hint)
                except Exception as e:
                    elapsed_s = time.time() - t0
                    tb = traceback.format_exc()
                    _write_response(conn, {"ok": False, "error": str(e), "traceback": tb})
                    _slog("eval_server", slug_hint, "OP_FAIL",
                          op=op_name, elapsed=elapsed_s,
                          error=str(e)[:120])
            finally:
                conn.close()
    finally:
        _cleanup()

    return 0


def _daemonize() -> None:
    """Double-fork + redirect stdio so the server outlives the launching shell."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    with open("/dev/null", "rb", 0) as dn:
        os.dup2(dn.fileno(), sys.stdin.fileno())
    # Keep stderr/stdout pointing at the launcher's file descriptors if the
    # user redirected them; otherwise daemonize throws them away.


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluation daemon")
    parser.add_argument("--socket", default=os.environ.get("EVAL_SERVER_SOCKET", DEFAULT_SOCKET_PATH),
                        help="Unix socket path (default: %(default)s)")
    parser.add_argument("--background", action="store_true",
                        help="Daemonize (double-fork, detach from terminal)")
    args = parser.parse_args()

    if args.background:
        _daemonize()

    return serve(args.socket)


if __name__ == "__main__":
    sys.exit(main())
