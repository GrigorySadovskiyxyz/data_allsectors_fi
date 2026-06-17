#!/usr/bin/env python3
"""
classify_innovative.py
======================

Classify each scraped company as **innovative or not** by reasoning over its
website text with Claude (default `claude-haiku-4-5` for cost; pass --model to
change), via the Claude **Message Batches** API.

It reuses the batch plumbing, manifests, and resume bookkeeping from
`translate_scraped.py`; only the request shape (a structured-output
classification instead of a translation) and the output (a CSV of verdicts
instead of per-domain text files) differ.

Input (per domain, first that exists)
--------------------------------------
1. translated/<domain>.txt   (English translation, preferred -- cleaner signal)
2. scraped/<domain>.txt       (original scraped text, fallback)

Only domains marked ok=True in scraped/scraped_index.csv with a non-empty
source file are considered. Very long pages are truncated to --max-input-chars
(classification needs the gist, not the whole 2 MB page).

Output
------
* innovation_index.csv : one row per domain classified
      domain, source_path, innovative, confidence, rationale, model,
      status, batch_id
* innovation_batches/<batch_id>.json : per-batch manifest (resume state)

Each request uses structured outputs (`output_config.format`) so the model
returns a guaranteed-parseable JSON object:
    {"innovative": bool, "confidence": 0..1, "rationale": "<=2 sentences"}
By default no separate thinking is used (cheapest); the `rationale` field still
captures the model's justification. Pass --think-budget N for explicit reasoning
tokens (adaptive thinking on models that support it).

Resumable: a domain with an `ok` row in innovation_index.csv is skipped; domains
in a submitted-but-uncollected batch are not resubmitted; errored requests get
no row and re-enter the todo set on the next --submit.

Usage
-----
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...

    python classify_innovative.py --dry-run            # counts + rough cost
    python classify_innovative.py                      # submit, poll, collect
    python classify_innovative.py --submit             # enqueue only
    python classify_innovative.py --collect            # retrieve finished
    python classify_innovative.py --status             # batch statuses
    python classify_innovative.py --limit 20           # small test run
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from typing import Optional

# Reuse the batch/manifest/resume machinery from the translator.
from translate_scraped import (
    BatchManifest, load_manifests, domains_in_flight, load_ok_domains,
    make_client, _fmt_duration,
)

csv.field_size_limit(16 * 1024 * 1024)

DEFAULT_MODEL = "claude-haiku-4-5"

# Models that take adaptive thinking; everything else uses enabled+budget_tokens.
_ADAPTIVE_THINKING = ("opus-4-5", "opus-4-6", "opus-4-7", "opus-4-8",
                      "sonnet-4-6", "fable-5", "mythos-5")


def thinking_config(model: str, budget: int) -> Optional[dict]:
    """Thinking config for `model`, or None to disable.

    budget<=0 disables thinking (cheapest -- the structured `rationale` field
    still carries a short justification). Adaptive-capable models ignore the
    numeric budget; older models (e.g. Haiku 4.5) take enabled+budget_tokens
    (min 1024)."""
    if budget <= 0:
        return None
    if any(tag in model for tag in _ADAPTIVE_THINKING):
        return {"type": "adaptive"}
    return {"type": "enabled", "budget_tokens": max(1024, budget)}


def max_tokens_for(model: str, budget: int) -> int:
    """Output ceiling: room for the verdict JSON plus any thinking budget."""
    if budget <= 0:
        return 1024
    if any(tag in model for tag in _ADAPTIVE_THINKING):
        return 4096
    return max(1024, budget) + 1024  # budget_tokens must be < max_tokens

# What "innovative" means for this study. Edit this rubric to match the paper's
# definition -- it is the single biggest lever on the labels you get.
SYSTEM_PROMPT = (
    "You are an analyst classifying companies as innovative or not, based only "
    "on the text of their website.\n\n"
    "Treat a company as INNOVATIVE if its website indicates it develops, "
    "engineers, or commercialises novel or technically differentiated products, "
    "technologies, services, or business models -- e.g. R&D, proprietary "
    "technology, software/hardware products, patents, scientific or engineering "
    "work, or a clearly new approach to its market.\n\n"
    "Treat a company as NOT innovative if it offers conventional, locally-"
    "delivered, or commoditised goods and services with no novel technology or "
    "product development -- e.g. a hairdresser, restaurant, plumber, taxi firm, "
    "bookkeeping/accounting office, general retailer, or pure reseller/distributor "
    "of others' products.\n\n"
    "Judge only on the evidence in the text. If the text is too thin to tell, "
    "lean 'not innovative' and lower your confidence. Reason carefully, then "
    "return your verdict."
)

USER_TEMPLATE = (
    "Company domain: {domain}\n\n"
    "Website text:\n\"\"\"\n{text}\n\"\"\"\n\n"
    "Classify this company as innovative or not."
)

# Structured-output schema -> guaranteed-parseable verdict.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "innovative": {"type": "boolean"},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["innovative", "confidence", "rationale"],
    "additionalProperties": False,
}

INDEX_FIELDS = [
    "domain", "source_path", "innovative", "confidence", "rationale",
    "model", "status", "batch_id",
]


# --------------------------------------------------------------------------- #
# Work discovery
# --------------------------------------------------------------------------- #


def source_for(domain: str, scraped_path: str, translated_dir: str) -> str:
    """Prefer the English translation if present, else the scraped original."""
    from translate_scraped import safe_filename
    tp = os.path.join(translated_dir, f"{safe_filename(domain)}.txt")
    if os.path.exists(tp) and os.path.getsize(tp) > 0:
        return tp
    return scraped_path


def load_done(index_csv: str) -> set[str]:
    done: set[str] = set()
    if not os.path.exists(index_csv):
        return done
    with open(index_csv, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("status") or "") == "ok" and row.get("domain"):
                done.add(row["domain"])
    return done


def append_index_rows(index_csv: str, rows: list[dict]) -> None:
    new = not os.path.exists(index_csv)
    with open(index_csv, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=INDEX_FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)
        fh.flush()


def build_requests(todo: dict[str, str], translated_dir: str, model: str,
                   max_input_chars: int, think_budget: int):
    """Yield (manifest_entry, params) per domain. One request per domain."""
    thinking = thinking_config(model, think_budget)
    max_tokens = max_tokens_for(model, think_budget)
    out = []
    counter = 0
    for domain, scraped_path in todo.items():
        src = source_for(domain, scraped_path, translated_dir)
        try:
            with open(src, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            out.append(({"domain": domain, "source_path": src,
                         "error": f"read:{e}"}, None))
            continue
        if not text.strip():
            out.append(({"domain": domain, "source_path": src,
                         "error": "empty_source"}, None))
            continue
        if len(text) > max_input_chars:
            text = text[:max_input_chars]

        custom_id = f"c{counter}"
        counter += 1
        entry = {"custom_id": custom_id, "domain": domain, "source_path": src}
        params = {
            "model": model,
            "max_tokens": max_tokens,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user",
                          "content": USER_TEMPLATE.format(domain=domain, text=text)}],
            "output_config": {"format": {"type": "json_schema",
                                         "schema": VERDICT_SCHEMA}},
        }
        if thinking is not None:
            params["thinking"] = thinking
        out.append((entry, params))
    return out


# --------------------------------------------------------------------------- #
# Submit
# --------------------------------------------------------------------------- #


def submit(client, work, batch_dir: str, max_per_batch: int,
           max_bytes_per_batch: int) -> list[str]:
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    os.makedirs(batch_dir, exist_ok=True)
    real = [(e, p) for e, p in work if p is not None]
    if not real:
        return []

    batch_ids: list[str] = []
    group: list[tuple[dict, dict]] = []
    group_bytes = 0

    def flush_group() -> None:
        nonlocal group, group_bytes
        if not group:
            return
        requests = [
            Request(custom_id=e["custom_id"],
                    params=MessageCreateParamsNonStreaming(**p))
            for e, p in group
        ]
        batch = client.messages.batches.create(requests=requests)
        created = (batch.created_at.isoformat()
                   if hasattr(batch.created_at, "isoformat") else str(batch.created_at))
        m = BatchManifest(batch_id=batch.id, created=created, status="submitted",
                          requests=[e for e, _ in group],
                          path=os.path.join(batch_dir, f"{batch.id}.json"))
        m.save()
        batch_ids.append(batch.id)
        print(f"  submitted batch {batch.id}  ({len(group):,} requests)",
              file=sys.stderr)
        group, group_bytes = [], 0

    for e, p in real:
        approx = len(p["messages"][0]["content"]) + len(SYSTEM_PROMPT) + 512
        if group and (len(group) >= max_per_batch
                      or group_bytes + approx > max_bytes_per_batch):
            flush_group()
        group.append((e, p))
        group_bytes += approx
    flush_group()
    return batch_ids


# --------------------------------------------------------------------------- #
# Collect
# --------------------------------------------------------------------------- #


def _verdict_json(message) -> Optional[dict]:
    """Parse the structured-output JSON from a succeeded message."""
    text = "".join(b.text for b in message.content if b.type == "text")
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def collect_batch(client, manifest: BatchManifest, index_csv: str,
                  model: str) -> dict:
    batch = client.messages.batches.retrieve(manifest.batch_id)
    if batch.processing_status != "ended":
        return {"status": batch.processing_status, "written": 0}

    by_id = {r["custom_id"]: r for r in manifest.requests}
    rows = []
    written = errors = 0
    for result in client.messages.batches.results(manifest.batch_id):
        entry = by_id.get(result.custom_id)
        if entry is None:
            continue
        if result.result.type == "succeeded":
            v = _verdict_json(result.result.message)
            if v is None:
                rows.append({"domain": entry["domain"],
                             "source_path": entry["source_path"],
                             "innovative": "", "confidence": "", "rationale": "",
                             "model": model, "status": "parse_error",
                             "batch_id": manifest.batch_id})
                errors += 1
                continue
            rows.append({
                "domain": entry["domain"], "source_path": entry["source_path"],
                "innovative": bool(v.get("innovative")),
                "confidence": v.get("confidence", ""),
                "rationale": (v.get("rationale", "") or "").replace("\n", " ").strip(),
                "model": model, "status": "ok", "batch_id": manifest.batch_id,
            })
            written += 1
        else:
            rows.append({"domain": entry["domain"],
                         "source_path": entry["source_path"],
                         "innovative": "", "confidence": "", "rationale": "",
                         "model": model, "status": "error",
                         "batch_id": manifest.batch_id})
            errors += 1

    append_index_rows(index_csv, rows)
    manifest.status = "collected"
    manifest.save()
    return {"status": "ended", "written": written, "errors": errors}


def collect_all(client, batch_dir: str, index_csv: str, model: str,
                poll_seconds: int, wait: bool) -> None:
    started = time.monotonic()
    while True:
        manifests = [m for m in load_manifests(batch_dir) if m.status != "collected"]
        if not manifests:
            print("all batches collected.", file=sys.stderr)
            return
        for m in manifests:
            res = collect_batch(client, m, index_csv, model)
            if res["status"] == "ended":
                print(f"  collected {m.batch_id}: {res['written']:,} classified, "
                      f"{res['errors']:,} errors", file=sys.stderr)
        remaining = sum(1 for m in load_manifests(batch_dir)
                        if m.status != "collected")
        print(f"[{_fmt_duration(time.monotonic()-started)}] "
              f"outstanding batches: {remaining:,}", file=sys.stderr)
        if remaining == 0:
            return
        if not wait:
            print(f"{remaining:,} batch(es) still processing -- re-run --collect "
                  f"later.", file=sys.stderr)
            return
        time.sleep(poll_seconds)


def show_status(client, batch_dir: str) -> None:
    manifests = load_manifests(batch_dir)
    if not manifests:
        print("no batches submitted yet.", file=sys.stderr)
        return
    for m in manifests:
        if m.status == "collected":
            print(f"{m.batch_id}  collected  ({len(m.requests):,} reqs)")
            continue
        b = client.messages.batches.retrieve(m.batch_id)
        c = b.request_counts
        print(f"{m.batch_id}  {b.processing_status}  "
              f"({len(m.requests):,} reqs | succeeded={c.succeeded} "
              f"processing={c.processing} errored={c.errored})")


def dry_run(work, model: str, think_budget: int) -> None:
    real = [(e, p) for e, p in work if p is not None]
    skipped = [(e, p) for e, p in work if p is None]
    in_chars = sum(len(p["messages"][0]["content"]) for _, p in real)
    in_tok = in_chars / 4
    # Output = short JSON verdict (~120 tok) + any thinking budget consumed.
    out_tok_each = 120 + (max(1024, think_budget) if think_budget > 0 else 0)
    out_tok = len(real) * out_tok_each
    price = {
        "claude-opus-4-8": (5.0, 25.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }.get(model, (1.0, 5.0))
    full = (in_tok / 1e6) * price[0] + (out_tok / 1e6) * price[1]
    thinking_note = (f"thinking budget {think_budget}" if think_budget > 0
                     else "no thinking (rationale only)")
    print(f"model                : {model}  ({thinking_note})")
    print(f"domains to classify  : {len(real):,}")
    print(f"skipped (empty/err)  : {len(skipped):,}")
    print(f"~input tokens        : {in_tok:,.0f}")
    print(f"~output tokens (est) : {out_tok:,.0f}  (~{out_tok_each}/domain)")
    print(f"~est. cost (batched) : ${full*0.5:,.2f}  (full-price ${full:,.2f})")
    print("\nNOTE: output tokens are a rough guess. Validate on a --limit run "
          "before the full set.")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Classify scraped companies as innovative-or-not with Claude "
                    "(reasoning) via the Batches API (resumable).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--index", default="scraped/scraped_index.csv")
    p.add_argument("--translated-dir", default="translated",
                   help="use English translations from here when available")
    p.add_argument("--innovation-index", default="innovation_index.csv")
    p.add_argument("--batch-dir", default="innovation_batches")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Claude model id (default {DEFAULT_MODEL})")
    p.add_argument("--max-input-chars", type=int, default=16000,
                   help="truncate each page to this many chars before classifying")
    p.add_argument("--think-budget", type=int, default=0,
                   help="thinking tokens per request (0 = off, cheapest; the "
                        "structured rationale field still justifies each verdict). "
                        "Adaptive-thinking models ignore the number and just "
                        "enable adaptive thinking.")
    p.add_argument("--max-per-batch", type=int, default=20000)
    p.add_argument("--max-mb-per-batch", type=int, default=180)
    p.add_argument("--poll-seconds", type=int, default=60)
    p.add_argument("--limit", type=int, default=0,
                   help="classify at most this many new domains (0 = all)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--submit", action="store_true")
    mode.add_argument("--collect", action="store_true")
    mode.add_argument("--status", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not os.path.exists(args.index):
        print(f"scraper index not found: {args.index}", file=sys.stderr)
        return 2

    if args.collect:
        client = make_client()
        collect_all(client, args.batch_dir, args.innovation_index, args.model,
                    args.poll_seconds, wait=False)
        return 0
    if args.status:
        client = make_client()
        show_status(client, args.batch_dir)
        return 0

    ok = load_ok_domains(args.index)
    done = load_done(args.innovation_index)
    in_flight = domains_in_flight(load_manifests(args.batch_dir))
    todo = {d: pth for d, pth in ok.items()
            if d not in done and d not in in_flight}
    if args.limit:
        todo = dict(list(todo.items())[:args.limit])

    print(f"{len(ok):,} ok domains | {len(done):,} already classified | "
          f"{len(in_flight):,} in-flight | {len(todo):,} to submit",
          file=sys.stderr)

    work = build_requests(todo, args.translated_dir, args.model,
                          args.max_input_chars, args.think_budget)

    if args.dry_run:
        dry_run(work, args.model, args.think_budget)
        return 0

    client = make_client()
    max_bytes = args.max_mb_per_batch * 1024 * 1024

    if todo:
        print("submitting batches...", file=sys.stderr)
        submit(client, work, args.batch_dir, args.max_per_batch, max_bytes)
    else:
        print("nothing new to submit.", file=sys.stderr)

    if args.submit:
        print("submitted. Run --collect (or no flag) to retrieve results.",
              file=sys.stderr)
        return 0

    print("polling for completion (Ctrl-C to stop; re-run --collect later)...",
          file=sys.stderr)
    collect_all(client, args.batch_dir, args.innovation_index, args.model,
                args.poll_seconds, wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
