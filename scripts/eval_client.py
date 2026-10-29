"""
Client for the eval_server daemon.

Thin socket wrapper. The daemon owns every ML model; clients just ask it to
score articles and get composite + dimension breakdowns back. Designed so
`scripts/evaluate.py` can transparently use the daemon when it's up and fall
back to in-process loading when it isn't.

Protocol: newline-delimited JSON over a Unix socket.
  Request:  {"op": "<name>", "args": {...}}\n
  Response: {"ok": true, "result": {...}}\n
            or {"ok": false, "error": "<msg>", "traceback": "..."}\n

Default socket path: /tmp/content-writer-eval.sock
Override with env var EVAL_SERVER_SOCKET.
"""
from __future__ import annotations

import json
import os
import socket

DEFAULT_SOCKET_PATH = "/tmp/content-writer-eval.sock"
SOCKET_PATH = os.environ.get("EVAL_SERVER_SOCKET", DEFAULT_SOCKET_PATH)
CONNECT_TIMEOUT_S = 1.0
REQUEST_TIMEOUT_S = 600.0  # Scoring a single article is ~15-30s on GPU; 10min is plenty.


class DaemonUnavailable(RuntimeError):
    """Raised when the daemon is not reachable or refuses the request."""


def _send_recv(request: dict, socket_path: str = SOCKET_PATH) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT_S)
    try:
        s.connect(socket_path)
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as e:
        raise DaemonUnavailable(f"cannot connect to {socket_path}: {e}") from e
    s.settimeout(REQUEST_TIMEOUT_S)
    try:
        s.sendall((json.dumps(request) + "\n").encode("utf-8"))
        buf = bytearray()
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
            # server closes after sending one response, so read-until-EOF is fine
        if not buf:
            raise DaemonUnavailable("empty response")
        # response is a single JSON line; if multiple, take the first
        first = buf.split(b"\n", 1)[0]
        return json.loads(first.decode("utf-8"))
    finally:
        s.close()


def is_daemon_running(socket_path: str = SOCKET_PATH) -> bool:
    """Quick reachability check. Sends a health op."""
    try:
        resp = _send_recv({"op": "health", "args": {}}, socket_path)
        return bool(resp.get("ok"))
    except DaemonUnavailable:
        return False


def score_article(content_path: str, profile_path: str, verbose: bool = False,
                  slug: str | None = None, use_cache: bool = True,
                  socket_path: str = SOCKET_PATH) -> dict:
    """Ask the daemon to score one article.

    Returns: {"composite": float, "scores": {dim: float}, "details": {dim: {...}},
              "_cache": "hit" | "miss" (only when caching is active)}
    Raises DaemonUnavailable if the daemon refuses or is down.

    `slug` is required to enable caching. With slug=None or use_cache=False,
    the daemon always scores fresh and does not store the result.
    """
    resp = _send_recv(
        {
            "op": "score_article",
            "args": {
                "content_path": content_path,
                "profile_path": profile_path,
                "verbose": verbose,
                "slug": slug,
                "use_cache": use_cache,
            },
        },
        socket_path,
    )
    if not resp.get("ok"):
        raise DaemonUnavailable(
            f"daemon refused: {resp.get('error', 'unknown error')}"
        )
    return resp["result"]


def score_geo_structural(content_path: str, profile_path: str,
                         socket_path: str = SOCKET_PATH) -> dict:
    """Score dim 12 (GEO structural readiness) only. Returns {"score", "details"}.

    Useful for standalone rule-tuning without the full 13-dim daemon pass.
    The daemon does not cache this op; dim 12 is deterministic and fast.
    """
    resp = _send_recv(
        {
            "op": "score_geo_structural",
            "args": {"content_path": content_path, "profile_path": profile_path},
        },
        socket_path,
    )
    if not resp.get("ok"):
        raise DaemonUnavailable(resp.get("error", "score_geo_structural refused"))
    return resp["result"]


def flush_cache(slug: str | None = None, socket_path: str = SOCKET_PATH) -> int:
    """Flush cache for one slug, or the whole article_scores dict if slug is None.
    Returns the count of entries removed."""
    resp = _send_recv(
        {"op": "flush_cache", "args": {"slug": slug}},
        socket_path,
    )
    if not resp.get("ok"):
        raise DaemonUnavailable(resp.get("error", "flush_cache refused"))
    return int(resp["result"].get("removed", 0))


def shutdown(socket_path: str = SOCKET_PATH) -> bool:
    try:
        resp = _send_recv({"op": "shutdown", "args": {}}, socket_path)
        return bool(resp.get("ok"))
    except DaemonUnavailable:
        return False
