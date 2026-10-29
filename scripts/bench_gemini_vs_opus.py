#!/usr/bin/env python3
"""
bench_gemini_vs_opus.py — Gemini 3 Flash vs Opus (regen) +
Gemini 3 Pro vs Opus (propose) one-shot benchmark.

This script does NOT modify the production loop. It:
  1. backs up content/*.md,
  2. regenerates each of the 4 keyword articles via Gemini 3 Flash,
     scores each via eval_client.score_article, records timing + tokens,
  3. proposes a rule edit via Gemini 3 Pro for each keyword (one call
     per keyword), diffs the proposed SKILL.md against HEAD, records
     timing + tokens. Does NOT apply the edit.
  4. restores content/*.md to their pre-bench state,
  5. pulls Opus reference scores + timings from
     docs/benchmarks/sonnet-vs-opus.json (the prior bench's raw output;
     Opus data is not regenerated),
  6. renders docs/benchmarks/gemini-vs-opus.md.

Model IDs (verified via direct API probe 2026-04-16):
  GEMINI_FLASH_MODEL = "gemini-3-flash-preview"
  GEMINI_PRO_MODEL   = "gemini-3.1-pro-preview"

`gemini-3-pro-preview` and `gemini-3.1-pro-preview` are distinct live
model IDs; the bench targets the 3.1 model.

Run:
    python scripts/bench_gemini_vs_opus.py \\
        --raw-out /tmp/bench-gemini.json \\
        --report-out docs/benchmarks/gemini-vs-opus.md
"""
from __future__ import annotations

import argparse
import difflib
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import eval_client  # noqa: E402
from _regen_prompt import KEYWORD_PROMPTS, build_regen_prompt_for_gemini  # noqa: E402
from _propose_prompt import build_propose_prompt_for_gemini  # noqa: E402
from slog import slog  # noqa: E402
import gemini_backend  # noqa: E402

PROJECT_ROOT = _HERE.parent
OPUS_REFERENCE_JSON = PROJECT_ROOT / "docs" / "benchmarks" / "sonnet-vs-opus.json"
SKILL_PATH = PROJECT_ROOT / "rules" / "content-generation" / "SKILL.md"

# Model IDs + timeouts live in gemini_backend so the production loop and the
# bench can't drift. Bench-specific tweaks (e.g. a longer ceiling for the
# preflight ping) stay local.
GEMINI_FLASH_MODEL = gemini_backend.FLASH_MODEL
GEMINI_PRO_MODEL = gemini_backend.PRO_MODEL

# Bench uses the same per-call ceilings as production to keep comparisons
# fair. These mirror gemini_backend.REGEN_TIMEOUT_S / PROPOSE_TIMEOUT_S.
FLASH_CALL_TIMEOUT_S = gemini_backend.REGEN_TIMEOUT_S
PRO_CALL_TIMEOUT_S = gemini_backend.PROPOSE_TIMEOUT_S


def run_ping() -> int:
    """One short call to Flash and one to Pro; print JSON per call. Used as
    a pre-flight — if either 404s or 403s, stop before the real bench.

    Pro can take 5-60s even for trivial prompts because thinking_level
    defaults to dynamic and the SDK silently allocates thinking tokens
    regardless of prompt complexity (observed: a 4.2s ping followed by a
    >30s ping for the identical prompt). 90s is generous for the pre-flight.
    """
    results = []
    timeouts = {GEMINI_FLASH_MODEL: 60, GEMINI_PRO_MODEL: 90}
    for model in [GEMINI_FLASH_MODEL, GEMINI_PRO_MODEL]:
        r = gemini_backend.call(model, "Respond with the single word OK.",
                                timeout_s=timeouts[model])
        r["model"] = model
        results.append(r)
        print(json.dumps(r))
    ok = all(r["ok"] and "OK" in r["text"].upper()[:20] for r in results)
    return 0 if ok else 1


def _bench_regen_one(slug: str) -> dict:
    """Run one Flash regen for `slug`, write to content/{slug}.md, score it.
    Returns a dict suitable for dumping to /tmp/bench-gemini.json."""
    out_path = PROJECT_ROOT / "content" / f"{slug}.md"
    slog("bench-gemini", slug, "REGEN_START",
         model=GEMINI_FLASH_MODEL,
         utc=datetime.now(timezone.utc).isoformat())
    prompt = build_regen_prompt_for_gemini(slug, PROJECT_ROOT)
    call = gemini_backend.call(GEMINI_FLASH_MODEL, prompt,
                               timeout_s=FLASH_CALL_TIMEOUT_S)
    slog("bench-gemini", slug, "REGEN_END",
         ok=call["ok"], elapsed=call["elapsed_s"], error=call["error"],
         utc=datetime.now(timezone.utc).isoformat())

    entry: dict = {
        "slug": slug,
        "model": GEMINI_FLASH_MODEL,
        "regen_s": round(call["elapsed_s"], 3),
        "prompt_token_count": call["prompt_token_count"],
        "candidates_token_count": call["candidates_token_count"],
        "thoughts_token_count": call["thoughts_token_count"],
        "error": call["error"],
    }

    if not call["ok"] or not call["text"].strip():
        entry["ok"] = False
        entry["score_error"] = "regen did not return text"
        return entry

    text = gemini_backend.strip_markdown_fence(call["text"])
    # Enforce the completion sentinel. Mirrors regen._validate_output so bench
    # scoring rejects the same way the production validator would.
    if "<!-- phase-4: pass -->" not in text.rstrip().splitlines()[-3:]:
        text = text.rstrip() + "\n\n<!-- phase-4: pass -->\n"

    out_path.write_text(text, encoding="utf-8")

    try:
        score_resp = eval_client.score_article(
            str(out_path),
            str(PROJECT_ROOT / "data" / "profiles" / f"{slug}-profile.json"),
            verbose=False, slug=slug, use_cache=False,
        )
    except Exception as e:  # noqa: BLE001
        entry["ok"] = False
        entry["score_error"] = f"{type(e).__name__}: {e}"
        return entry

    entry["ok"] = True
    entry["composite"] = score_resp["composite"]
    entry["scores"] = score_resp["scores"]
    return entry


# v3.7-no-judge weights (n/82 fractions). Must match evaluate.WEIGHTS.
WEIGHTS = {
    "topical_coverage":       25 / 82,
    "corpus_match":           11 / 82,
    "keyword_relevance":      10 / 82,
    "readability_structural":  8 / 82,
    "entity_coverage":         7 / 82,
    "keyword_profile":         5 / 82,
    "content_length":          5 / 82,
    "banned_words":            5 / 82,
    "entity_graph":            3 / 82,
    "narrative_coverage":      3 / 82,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "weights must sum to 1.0"

TOLERANCE = 0.10  # 10% per-dim relative-drop tolerance


def _load_opus_reference() -> dict:
    """Read the Opus block out of docs/benchmarks/sonnet-vs-opus.json.
    Returns {slug: {composite, regen_s, scores}} for each of the 4 keywords."""
    if not OPUS_REFERENCE_JSON.exists():
        raise RuntimeError(
            f"Opus reference data not found at {OPUS_REFERENCE_JSON}; "
            f"the report cannot fill the Opus column."
        )
    raw = json.loads(OPUS_REFERENCE_JSON.read_text())
    opus = raw.get("opus") or {}
    return {slug: {
        "composite": e["composite"],
        "regen_s": e["regen_s"],
        "scores": e["scores"],
    } for slug, e in opus.items()}


def render_report(data: dict, out_path: Path) -> None:
    """Render the 10-section comparison report mirroring
    docs/benchmarks/sonnet-vs-opus.md. Body is assembled in sections,
    joined, and written to out_path."""
    import statistics

    opus_ref = _load_opus_reference()
    sections: list[str] = []

    flash_regen = [e for e in data.get("regen", {}).values() if e.get("ok")]
    flash_times = [e["regen_s"] for e in flash_regen]
    flash_comps = [e["composite"] for e in flash_regen]
    opus_times = [v["regen_s"] for v in opus_ref.values()]
    opus_comps = [v["composite"] for v in opus_ref.values()]
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
    speed_ratio = (avg(flash_times) / avg(opus_times)) if avg(opus_times) else float("nan")

    # Section 0: header
    sections.append(
        f"# Gemini 3 Flash + Gemini 3 Pro vs Opus 4.6 — Benchmark Report "
        f"({datetime.now(timezone.utc).date()})\n\n"
        f"**Run window (UTC):** {data['run_utc_start']} → {data['run_utc_end']}\n"
        f"**Driver script:** `scripts/bench_gemini_vs_opus.py`\n"
        f"**Cache epoch:** `v3.7-no-judge` (10-dim composite, `n/82` weights)\n"
        f"**Opus reference:** `docs/benchmarks/sonnet-vs-opus.json`\n"
        f"**Models under test:** Flash=`{data['models']['flash']}`, "
        f"Pro=`{data['models']['pro']}`\n"
        f"**Tolerance:** {int(TOLERANCE*100)}% per-dim relative drop\n"
    )

    # Section 1: executive summary
    sections.append(
        "## 1. Executive summary\n\n"
        "| | **Opus 4.6** | **Gemini 3 Flash** | Ratio / Δ |\n"
        "|---|---:|---:|---:|\n"
        f"| Avg regen time | {avg(opus_times):.1f}s | {avg(flash_times):.1f}s | "
        f"{speed_ratio:.2f}× |\n"
        f"| Avg composite | {avg(opus_comps):.4f} | {avg(flash_comps):.4f} | "
        f"{avg(flash_comps) - avg(opus_comps):+.4f} |\n"
        f"| Max regen time | {max(opus_times) if opus_times else 0:.1f}s | "
        f"{max(flash_times) if flash_times else 0:.1f}s | — |\n"
        f"| Min regen time | {min(opus_times) if opus_times else 0:.1f}s | "
        f"{min(flash_times) if flash_times else 0:.1f}s | — |\n"
        f"| Keywords where Flash ≥ Opus composite | — | "
        f"{sum(1 for s in opus_ref if data.get('regen', {}).get(s, {}).get('ok') and data['regen'][s]['composite'] >= opus_ref[s]['composite'])}/4 | — |\n"
    )

    # Section 2: event timeline
    timeline_rows = ["| # | Keyword | Model | Phase | Elapsed |",
                     "|---|---|---|---|---:|"]
    i = 0
    for slug, e in data.get("regen", {}).items():
        i += 1
        timeline_rows.append(
            f"| {i} | {slug} | Flash | REGEN | {e.get('regen_s', 0):.1f}s |"
        )
    for slug, e in data.get("propose", {}).items():
        i += 1
        timeline_rows.append(
            f"| {i} | {slug} | Pro | PROPOSE | {e.get('propose_s', 0):.1f}s |"
        )
    sections.append("## 2. Event timeline\n\n" + "\n".join(timeline_rows))

    # Section 3: per-keyword headline
    head_rows = ["| keyword | opus_comp | opus_regen_s | flash_comp | flash_regen_s | Δcomp | speed_ratio |",
                 "|---|---:|---:|---:|---:|---:|---:|"]
    for slug, oref in opus_ref.items():
        fe = data.get("regen", {}).get(slug, {})
        if not fe.get("ok"):
            head_rows.append(f"| {slug} | {oref['composite']:.4f} | "
                             f"{oref['regen_s']:.1f} | FAILED | — | — | — |")
            continue
        dc = fe["composite"] - oref["composite"]
        sr = fe["regen_s"] / oref["regen_s"] if oref["regen_s"] else float("nan")
        head_rows.append(
            f"| {slug} | {oref['composite']:.4f} | {oref['regen_s']:.1f} | "
            f"{fe['composite']:.4f} | {fe['regen_s']:.1f} | "
            f"{dc:+.4f} | {sr:.2f}× |"
        )
    sections.append("## 3. Per-keyword headline table\n\n" + "\n".join(head_rows))

    # Section 4: per-keyword per-dim breakdown + collect breaches
    breaches: list[tuple] = []
    for slug, oref in opus_ref.items():
        fe = data.get("regen", {}).get(slug, {})
        if not fe.get("ok"):
            continue
        lines = [f"### 4.{slug}\n",
                 f"- Opus regen: **{oref['regen_s']:.1f}s** → composite **{oref['composite']:.4f}**",
                 f"- Flash regen: **{fe['regen_s']:.1f}s** → composite **{fe['composite']:.4f}**",
                 f"- Composite Δ: **{fe['composite'] - oref['composite']:+.4f}**\n",
                 "| dim | weight | opus | flash | Δraw | Δrel_% | Δcontrib | flag |",
                 "|---|---:|---:|---:|---:|---:|---:|:-:|"]
        total_shift = 0.0
        for dim, w in WEIGHTS.items():
            op = oref["scores"].get(dim, 0.0)
            fp = fe["scores"].get(dim, 0.0)
            draw = fp - op
            drel = ((op - fp) / op * 100) if op > 0 else 0.0
            dcontrib = draw * w
            total_shift += dcontrib
            flag = "="
            if draw > 0: flag = "⬆"
            elif draw < 0: flag = "⬇"
            if drel > TOLERANCE * 100:
                flag = "🚫"
                breaches.append((slug, dim, w, op, fp, drel, dcontrib))
            lines.append(f"| {dim} | {w:.4f} | {op:.4f} | {fp:.4f} | "
                         f"{draw:+.4f} | {drel:+.1f} | {dcontrib:+.5f} | {flag} |")
        lines.append(f"| **total contribution shift** | — | — | — | — | — | "
                     f"**{total_shift:+.5f}** | — |")
        sections.append("\n".join(lines))

    # Section 5: tolerance breaches
    if breaches:
        b_rows = ["| # | keyword | dim | weight | opus | flash | drop_% | composite impact |",
                  "|---|---|---|---:|---:|---:|---:|---:|"]
        for idx, (slug, dim, w, op, fp, drel, dcontrib) in enumerate(breaches, 1):
            b_rows.append(f"| {idx} | {slug} | {dim} | {w:.4f} | {op:.4f} | "
                          f"{fp:.4f} | {drel:.1f}% | {dcontrib:+.5f} |")
        sections.append("## 5. Per-dim regressions exceeding 10% tolerance\n\n"
                        + "\n".join(b_rows))
    else:
        sections.append("## 5. Per-dim regressions exceeding 10% tolerance\n\n"
                        "None — every dim on every keyword stayed within the "
                        f"{int(TOLERANCE*100)}% drop tolerance.")

    # Section 6: timing analysis
    def stats(xs):
        return (min(xs), statistics.median(xs), max(xs)) if xs else (0, 0, 0)
    fmin, fmed, fmax = stats(flash_times)
    omin, omed, omax = stats(opus_times)
    sections.append(
        "## 6. Timing analysis (regen, 4 samples each)\n\n"
        "| model | min | median | max |\n"
        "|---|---:|---:|---:|\n"
        f"| Opus 4.6 | {omin:.1f}s | {omed:.1f}s | {omax:.1f}s |\n"
        f"| Gemini 3 Flash | {fmin:.1f}s | {fmed:.1f}s | {fmax:.1f}s |"
    )

    # Section 7: Pro propose analysis
    prop_rows = ["| keyword | propose_s | edit_status | ±lines | prompt_tok | candidate_tok | thoughts_tok | error |",
                 "|---|---:|---|---:|---:|---:|---:|---|"]
    for slug, pe in data.get("propose", {}).items():
        prop_rows.append(
            f"| {slug} | {pe.get('propose_s', 0):.1f} | "
            f"{pe.get('edit_status', '—')} | "
            f"+{pe.get('diff_added_lines', 0)}/-{pe.get('diff_removed_lines', 0)} | "
            f"{pe.get('prompt_token_count') or '—'} | "
            f"{pe.get('candidates_token_count') or '—'} | "
            f"{pe.get('thoughts_token_count') or '—'} | "
            f"{pe.get('error') or ''} |"
        )
    sections.append("## 7. Pro propose analysis\n\n" + "\n".join(prop_rows))

    # Section 8: token counts
    tok_rows = ["| task | keyword | prompt_tok | candidate_tok | thoughts_tok | total |",
                "|---|---|---:|---:|---:|---:|"]
    for slug, e in data.get("regen", {}).items():
        pt = e.get("prompt_token_count") or 0
        ct = e.get("candidates_token_count") or 0
        tt = e.get("thoughts_token_count") or 0
        tok_rows.append(f"| regen (Flash) | {slug} | {pt} | {ct} | {tt} | {pt + ct + tt} |")
    for slug, e in data.get("propose", {}).items():
        pt = e.get("prompt_token_count") or 0
        ct = e.get("candidates_token_count") or 0
        tt = e.get("thoughts_token_count") or 0
        tok_rows.append(f"| propose (Pro) | {slug} | {pt} | {ct} | {tt} | {pt + ct + tt} |")
    sections.append("## 8. Token counts (input + output + thinking)\n\n"
                    + "\n".join(tok_rows))

    # Section 9: recommendation
    flash_win_count = sum(
        1 for slug in opus_ref
        if data.get("regen", {}).get(slug, {}).get("ok")
        and data["regen"][slug]["composite"] >= opus_ref[slug]["composite"]
    )
    edit_count = sum(1 for e in data.get("propose", {}).values()
                     if e.get("edit_status") == "edit")
    breach_count = len(breaches)
    pro_times = [e["propose_s"] for e in data.get("propose", {}).values() if e.get("ok")]

    regen_rec = (
        "**Switch regen to Gemini 3 Flash.**"
        if (speed_ratio < 0.9 and breach_count == 0
            and avg(flash_comps) >= avg(opus_comps) - 0.005)
        else "**Keep regen on Opus 4.6.**"
    )
    propose_rec = (
        "**Switch propose to Gemini 3 Pro.**"
        if (edit_count >= 3 and pro_times and max(pro_times) < PRO_CALL_TIMEOUT_S)
        else "**Keep propose on Opus 4.6.**"
    )

    sections.append(
        "## 9. Recommendation\n\n"
        f"- Regen: {regen_rec}\n"
        f"  Flash average regen time = {avg(flash_times):.1f}s "
        f"(speed ratio vs Opus {speed_ratio:.2f}×), Flash average composite = "
        f"{avg(flash_comps):.4f} ({avg(flash_comps) - avg(opus_comps):+.4f} "
        f"vs Opus), per-dim tolerance breaches = {breach_count}, keywords "
        f"where Flash ≥ Opus composite = {flash_win_count}/4.\n"
        f"- Propose: {propose_rec}\n"
        f"  Pro produced a real edit on {edit_count}/4 keywords. Pro max "
        f"propose time = {max(pro_times) if pro_times else 0:.1f}s "
        f"(production ceiling is {PRO_CALL_TIMEOUT_S}s).\n\n"
        f"Heuristics: Flash wins regen only if >10% faster on average AND "
        f"zero per-dim tolerance breaches AND composite within 0.005 of Opus. "
        f"Pro wins propose only if it produced real edits on ≥3/4 keywords "
        f"AND worst-case latency fits the {PRO_CALL_TIMEOUT_S}s production ceiling.\n\n"
        f"The report is an input to the engine-swap decision, not the decision itself."
    )

    text = "\n\n".join(sections) + "\n"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    print(f"Wrote report: {out_path}")


def _fetch_score_report(slug: str) -> dict:
    """Shell out to evaluate.py in --suggest-weakest-dim mode for `slug`.
    Returns the parsed JSON dict. Uses the daemon path (no --no-daemon) for
    speed; the output shape is identical either way."""
    content_path = PROJECT_ROOT / "content" / f"{slug}.md"
    profile_path = PROJECT_ROOT / "data" / "profiles" / f"{slug}-profile.json"
    proc = subprocess.run(
        [
            "python", "scripts/evaluate.py", str(content_path),
            "--profile", str(profile_path),
            "--suggest-weakest-dim",
        ],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout.strip())


def _bench_propose_one(slug: str) -> dict:
    """Run one Pro propose for `slug`. Fetches the live score report, builds
    the prompt, calls Pro with HIGH thinking, diffs the result against the
    on-disk SKILL.md. Records: timing, token counts, whether a real edit was
    produced, and the number of changed lines. Does NOT apply the edit."""
    slog("bench-gemini", slug, "PROPOSE_START",
         model=GEMINI_PRO_MODEL,
         utc=datetime.now(timezone.utc).isoformat())
    report = _fetch_score_report(slug)
    prompt = build_propose_prompt_for_gemini(slug, report, PROJECT_ROOT)
    gemini_backend.rate_gate_pro()
    call = gemini_backend.call(GEMINI_PRO_MODEL, prompt,
                               timeout_s=PRO_CALL_TIMEOUT_S,
                               thinking_level="HIGH")
    slog("bench-gemini", slug, "PROPOSE_END",
         ok=call["ok"], elapsed=call["elapsed_s"], error=call["error"],
         utc=datetime.now(timezone.utc).isoformat())

    entry: dict = {
        "slug": slug,
        "model": GEMINI_PRO_MODEL,
        "propose_s": round(call["elapsed_s"], 3),
        "prompt_token_count": call["prompt_token_count"],
        "candidates_token_count": call["candidates_token_count"],
        "thoughts_token_count": call["thoughts_token_count"],
        "error": call["error"],
        "composite_before": report.get("composite"),
        "weakest_dim": report.get("weakest_dim"),
    }

    if not call["ok"] or not call["text"].strip():
        entry["ok"] = False
        entry["edit_status"] = "no_response"
        return entry

    proposed = gemini_backend.strip_markdown_fence(call["text"])
    current = SKILL_PATH.read_text(encoding="utf-8")
    diff = list(difflib.unified_diff(
        current.splitlines(), proposed.splitlines(),
        fromfile="SKILL.md (current)", tofile="SKILL.md (proposed)",
        lineterm="",
    ))
    added = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
    entry["ok"] = True
    entry["edit_status"] = "no_edit" if (added == 0 and removed == 0) else "edit"
    entry["diff_added_lines"] = added
    entry["diff_removed_lines"] = removed
    entry["diff_snippet"] = "\n".join(diff[:40])
    return entry


def _snapshot_content(content_dir: Path, backup_dir: Path) -> None:
    """Copy every content/*.md into backup_dir. Called before any regen."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    for f in content_dir.glob("*.md"):
        shutil.copy2(f, backup_dir / f.name)


def _restore_content(backup_dir: Path, content_dir: Path) -> None:
    """Restore each backup/*.md → content/*.md. Called in a finally: block.
    Leaves no residue: backup_dir is removed after restore."""
    for f in backup_dir.glob("*.md"):
        shutil.copy2(f, content_dir / f.name)
    shutil.rmtree(backup_dir, ignore_errors=True)


def run_full(raw_out: Path, skip_regen: bool, skip_propose: bool) -> dict:
    """Drive the whole bench. Returns a dict written verbatim to raw_out.

    Results shape:
      {
        "run_utc_start": "...",
        "run_utc_end": "...",
        "models": {"flash": "...", "pro": "..."},
        "regen":   {slug: {...per-keyword entry from _bench_regen_one...}},
        "propose": {slug: {...per-keyword entry from _bench_propose_one...}},
      }
    """
    if not eval_client.is_daemon_running():
        raise RuntimeError("eval_server daemon is not running; start it and retry")

    out: dict = {
        "run_utc_start": datetime.now(timezone.utc).isoformat(),
        "models": {"flash": GEMINI_FLASH_MODEL, "pro": GEMINI_PRO_MODEL},
        "regen": {},
        "propose": {},
    }

    content_dir = PROJECT_ROOT / "content"
    backup = PROJECT_ROOT / ".autoresearch" / "bench-gemini-backup"
    _snapshot_content(content_dir, backup)
    try:
        if not skip_regen:
            for slug in KEYWORD_PROMPTS:
                print(f"\n=== regen/{slug} via {GEMINI_FLASH_MODEL} ===", flush=True)
                entry = _bench_regen_one(slug)
                out["regen"][slug] = entry
                print(f"  {'OK' if entry.get('ok') else 'FAIL'}: "
                      f"regen_s={entry.get('regen_s')} "
                      f"composite={entry.get('composite')} "
                      f"err={entry.get('error') or entry.get('score_error')}",
                      flush=True)
        if not skip_propose:
            for slug in KEYWORD_PROMPTS:
                print(f"\n=== propose/{slug} via {GEMINI_PRO_MODEL} ===", flush=True)
                entry = _bench_propose_one(slug)
                out["propose"][slug] = entry
                print(f"  {'OK' if entry.get('ok') else 'FAIL'}: "
                      f"propose_s={entry.get('propose_s')} "
                      f"status={entry.get('edit_status')} "
                      f"±lines={entry.get('diff_added_lines')}/{entry.get('diff_removed_lines')} "
                      f"err={entry.get('error')}", flush=True)
    finally:
        _restore_content(backup, content_dir)

    out["run_utc_end"] = datetime.now(timezone.utc).isoformat()
    raw_out.write_text(json.dumps(out, indent=2))
    print(f"\nWrote raw bench JSON: {raw_out}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-out", default="/tmp/bench-gemini.json")
    parser.add_argument(
        "--report-out",
        default=str(PROJECT_ROOT / "docs" / "benchmarks" / "gemini-vs-opus.md"),
    )
    parser.add_argument("--skip-regen", action="store_true",
                        help="Skip the Flash regen bench; reuse cached regen block in --raw-out if present.")
    parser.add_argument("--skip-propose", action="store_true",
                        help="Skip the Pro propose bench; reuse cached propose block in --raw-out if present.")
    parser.add_argument("--ping", action="store_true",
                        help="One-call smoke to both Gemini models, then exit.")
    parser.add_argument("--regen-only",
                        help="Run Flash regen + score on one slug only, print result JSON, exit.")
    parser.add_argument("--propose-only",
                        help="Run Pro propose on one slug only, print result JSON, exit.")
    args = parser.parse_args()

    if args.ping:
        return run_ping()

    if args.regen_only:
        if args.regen_only not in KEYWORD_PROMPTS:
            parser.error(f"unknown slug {args.regen_only!r}; valid: "
                         f"{sorted(KEYWORD_PROMPTS.keys())}")
        if not eval_client.is_daemon_running():
            parser.error("eval_server daemon is not running; start it and retry")
        backup = PROJECT_ROOT / ".autoresearch" / "bench-gemini-backup"
        _snapshot_content(PROJECT_ROOT / "content", backup)
        try:
            r = _bench_regen_one(args.regen_only)
            print(json.dumps(r, indent=2))
        finally:
            _restore_content(backup, PROJECT_ROOT / "content")
        return 0 if r.get("ok") else 1

    if args.propose_only:
        if args.propose_only not in KEYWORD_PROMPTS:
            parser.error(f"unknown slug {args.propose_only!r}; valid: "
                         f"{sorted(KEYWORD_PROMPTS.keys())}")
        r = _bench_propose_one(args.propose_only)
        print(json.dumps(r, indent=2))
        return 0 if r.get("ok") else 1

    raw_out = Path(args.raw_out)
    # Allow partial runs: if cached JSON exists and --skip-regen or
    # --skip-propose is set, we run only the missing half and merge.
    cached: dict | None = None
    if raw_out.exists() and (args.skip_regen or args.skip_propose):
        cached = json.loads(raw_out.read_text())

    data = run_full(raw_out, skip_regen=args.skip_regen, skip_propose=args.skip_propose)
    if cached is not None:
        if args.skip_regen and cached.get("regen"):
            data["regen"] = cached["regen"]
        if args.skip_propose and cached.get("propose"):
            data["propose"] = cached["propose"]
        raw_out.write_text(json.dumps(data, indent=2))

    render_report(data, Path(args.report_out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
