"""Unit tests for regen._validate_output.

The generator either returns a complete article ending with the completion
sentinel or an empty response (the hard-refusal abort signal). _validate_output
must reject: missing file, empty file, missing frontmatter, a too-short draft,
and a draft without the sentinel in its trailing lines (a truncated response).
"""
from __future__ import annotations

from pathlib import Path

import pytest

import sys
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "scripts"))

import regen  # noqa: E402


SENTINEL = "<!-- phase-4: pass -->"
MIN_WORDS = regen.MIN_BODY_WORDS


def _body(words: int) -> str:
    return " ".join(["word"] * words)


def _valid_file(tmp_path, words: int = MIN_WORDS + 50, sentinel: bool = True) -> Path:
    p = tmp_path / "content.md"
    body = f"""---
title: Test
---

# Heading

{_body(words)}
"""
    if sentinel:
        body += f"\n{SENTINEL}\n"
    p.write_text(body, encoding="utf-8")
    return p


def test_valid_file_passes(tmp_path):
    ok, reason = regen._validate_output(_valid_file(tmp_path))
    assert ok, reason


def test_missing_file_fails(tmp_path):
    ok, reason = regen._validate_output(tmp_path / "nope.md")
    assert not ok
    assert "missing" in reason


def test_empty_file_is_abort_signal(tmp_path):
    p = tmp_path / "content.md"
    p.write_text("", encoding="utf-8")
    ok, reason = regen._validate_output(p)
    assert not ok
    assert "empty" in reason.lower()


def test_whitespace_only_is_abort_signal(tmp_path):
    p = tmp_path / "content.md"
    p.write_text("  \n\n  \n", encoding="utf-8")
    ok, reason = regen._validate_output(p)
    assert not ok
    assert "empty" in reason.lower()


def test_missing_frontmatter_fails(tmp_path):
    p = tmp_path / "content.md"
    p.write_text(f"# Title\n\n{_body(MIN_WORDS + 50)}\n\n{SENTINEL}\n",
                 encoding="utf-8")
    ok, reason = regen._validate_output(p)
    assert not ok
    assert "frontmatter" in reason


def test_too_short_fails(tmp_path):
    p = _valid_file(tmp_path, words=100)
    ok, reason = regen._validate_output(p)
    assert not ok
    assert "too short" in reason


def test_missing_sentinel_fails(tmp_path):
    # Phase 2 wrote; Phase 3 or 4 crashed. Looks valid-ish but lacks sentinel.
    p = _valid_file(tmp_path, sentinel=False)
    ok, reason = regen._validate_output(p)
    assert not ok
    assert "sentinel" in reason.lower()


def test_sentinel_as_last_line_with_trailing_newline(tmp_path):
    p = tmp_path / "content.md"
    body = f"""---
title: Test
---

{_body(MIN_WORDS + 50)}

{SENTINEL}
"""
    p.write_text(body, encoding="utf-8")
    ok, _ = regen._validate_output(p)
    assert ok


def test_sentinel_with_phase4_notes_above(tmp_path):
    # Phase 4 pass with some soft-violation notes above the sentinel.
    p = tmp_path / "content.md"
    body = f"""---
title: Test
---

{_body(MIN_WORDS + 50)}

<!-- phase-4-notes: saw 1 weak paragraph at line 12 -->
{SENTINEL}
"""
    p.write_text(body, encoding="utf-8")
    ok, _ = regen._validate_output(p)
    assert ok


def test_stray_sentinel_far_from_tail_does_not_pass(tmp_path):
    # A sentinel accidentally embedded in the body (e.g., in a code block)
    # should NOT satisfy the check — only the tail is authoritative.
    p = tmp_path / "content.md"
    body = f"""---
title: Test
---

{SENTINEL}

{_body(MIN_WORDS + 50)}

End of article without the real Phase-4 sentinel.
"""
    p.write_text(body, encoding="utf-8")
    ok, reason = regen._validate_output(p)
    assert not ok
    assert "sentinel" in reason.lower()
