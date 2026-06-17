#!/usr/bin/env python3
"""
translate_scraped.py
====================

Translate the per-domain main-page text produced by `scrape_main_pages.py`
(Finnish / mixed-language website text) into English using the Claude API
**Message Batches** endpoint (https://platform.claude.com/docs/en/build-with-claude/batch-processing).

Why the Batches API
-------------------
This is a one-time bulk job over ~50,000 pages where latency does not matter.
The Batches API processes Messages-API requests asynchronously at **50% of the
standard price**, accepts up to 100,000 requests (or 256 MB) per batch, and
supports every Messages feature. That makes it the right tool here versus
firing 50,000 live, full-price requests.

Cost note (model choice)
-------------------------
Translation is a simple task. The default model is `claude-opus-4-8` (highest
quality, highest price). For a run this large, **`claude-haiku-4-5` or
`claude-sonnet-4-6` are dramatically cheaper and handle translation well** --
pass `--model claude-haiku-4-5` to switch. Batches already halve whichever
model's price.

Input
-----
* scraped/<domain>.txt      : one plain-text file per domain (from the scraper)
* scraped/scraped_index.csv : index with an `ok` column; only ok=True domains
                              with a non-empty text file are translated.

Output
------
* translated/<domain>.txt   : the English translation, one file per domain
* translation_index.csv     : one row per domain processed
      domain, source_path, translated_path, chars_in, chars_out, parts,
      status, batch_id
* translation_batches/<batch_id>.json : per-batch manifest mapping each request's
      custom_id back to its domain / chunk, plus the batch's collection state.
      These drive resume and are how `--collect` finds work to retrieve.

How a large page is handled
---------------------------
A page longer than `--chunk-chars` is split on paragraph boundaries into
several requests (custom_id `<n>` with a recorded part index). On collection the
translated chunks are concatenated back in order into a single output file. A
single paragraph longer than the limit is hard-split.

Resumable (same philosophy as the scraper)
-------------------------------------------
State lives on disk, so the job survives being stopped and re-run:
  * a domain with an `ok` row in translation_index.csv (and a non-empty output
    file) is considered done and skipped;
  * domains already assigned to a submitted-but-not-yet-collected batch are not
    re-submitted;
  * `--submit` enqueues only the remaining work; `--collect` polls outstanding
    batches and writes whatever has finished; the default (no mode flag) does
    submit, then poll-and-collect to completion.
Errored requests get no output file, so they automatically re-enter the todo
set on the next `--submit`.

Usage
-----
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...

    # End-to-end: submit remaining work, then poll until everything is collected
    python translate_scraped.py

    # See what would be submitted (counts + rough token/cost estimate), no API calls
    python translate_scraped.py --dry-run

    # Submit only, come back later
    python translate_scraped.py --submit
    python translate_scraped.py --collect      # poll + retrieve finished batches
    python translate_scraped.py --status       # show outstanding batch statuses

    # Cheaper model, small test run
    python translate_scraped.py --model claude-haiku-4-5 --limit 50
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Optional

# Larger CSV fields than the default 128 KB limit (some pages are big).
csv.field_size_limit(16 * 1024 * 1024)

DEFAULT_MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = (
    "You are a professional translator. Translate the website text in the user "
    "message into English. The source is usually Finnish but may contain Swedish "
    "or other languages; translate all non-English content into natural, fluent "
    "English and leave any text that is already English unchanged. Preserve the "
    "line and paragraph structure of the original. Do not summarise, omit, or add "
    "anything. Output ONLY the translation -- no preamble, notes, or explanation."
)

INDEX_FIELDS = [
    "domain", "source_path", "translated_path",
    "chars_in", "chars_out", "parts", "status", "batch_id",
]


# --------------------------------------------------------------------------- #
# Small helpers (mirrors scrape_main_pages.py conventions)
# --------------------------------------------------------------------------- #


def safe_filename(domain: str) -> str:
    """Filesystem-safe file stem for a domain (same rule as the scraper)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", domain)


def _fmt_duration(seconds: float) -> str:
    """Human-readable H:MM:SS (or M:SS) from a number of seconds."""
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def chunk_text(text: str, chunk_chars: int) -> list[str]:
    """Split text into chunks <= chunk_chars, preferring paragraph boundaries.

    Paragraphs (newline-separated) are packed greedily; a single paragraph that
    exceeds the limit is hard-split. Joining the returned chunks with "\\n"
    reconstructs the original (modulo collapsed runs of blank lines)."""
    if len(text) <= chunk_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for para in text.split("\n"):
        # Hard-split a single oversized paragraph.
        while len(para) > chunk_chars:
            if current:
                chunks.append("\n".join(current))
                current, size = [], 0
            chunks.append(para[:chunk_chars])
            para = para[chunk_chars:]
        add = len(para) + (1 if current else 0)
        if size + add > chunk_chars and current:
            chunks.append("\n".join(current))
            current, size = [], 0
            add = len(para)
        current.append(para)
        size += add
    if current:
        chunks.append("\n".join(current))
    return chunks


def max_tokens_for(chars: int) -> int:
    """Generous output-token ceiling for a chunk of `chars` source characters.

    English output is roughly source-length; ~4 chars/token plus headroom for
    Finnish->English expansion. Clamped to a sane band."""
    return min(16384, max(1024, int(chars * 0.5)))


# --------------------------------------------------------------------------- #
# Work discovery & resume bookkeeping
# --------------------------------------------------------------------------- #


def load_ok_domains(index_csv: str) -> dict[str, str]:
    """domain -> source text_path, for ok=True rows with a non-empty file."""
    todo: dict[str, str] = {}
    with open(index_csv, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("ok") or "").strip().lower() != "true":
                continue
            domain = (row.get("domain") or "").strip()
            path = (row.get("text_path") or "").strip()
            if not domain or not path:
                continue
            if os.path.exists(path) and os.path.getsize(path) > 0:
                todo.setdefault(domain, path)
    return todo


def load_done(index_csv: str, out_dir: str) -> set[str]:
    """Domains already translated successfully (index row ok + output present)."""
    done: set[str] = set()
    if not os.path.exists(index_csv):
        return done
    with open(index_csv, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("status") or "") != "ok":
                continue
            tp = row.get("translated_path") or ""
            if tp and os.path.exists(tp) and os.path.getsize(tp) > 0:
                done.add(row["domain"])
    return done


@dataclass
class BatchManifest:
    """On-disk record of one submitted batch and how to collect it."""
    batch_id: str
    created: str
    status: str  # "submitted" | "collected"
    requests: list[dict]  # {custom_id, domain, part, n_parts, source_path}
    path: str

    @classmethod
    def load(cls, path: str) -> "BatchManifest":
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return cls(d["batch_id"], d["created"], d["status"], d["requests"], path)

    def save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({
                "batch_id": self.batch_id,
                "created": self.created,
                "status": self.status,
                "requests": self.requests,
            }, fh, ensure_ascii=False, indent=2)


def load_manifests(batch_dir: str) -> list[BatchManifest]:
    if not os.path.isdir(batch_dir):
        return []
    out = []
    for name in sorted(os.listdir(batch_dir)):
        if name.endswith(".json"):
            out.append(BatchManifest.load(os.path.join(batch_dir, name)))
    return out


def domains_in_flight(manifests: Iterable[BatchManifest]) -> set[str]:
    """Domains assigned to a submitted-but-not-collected batch."""
    pending: set[str] = set()
    for m in manifests:
        if m.status != "collected":
            for r in m.requests:
                pending.add(r["domain"])
    return pending


# --------------------------------------------------------------------------- #
# Index (output) writer
# --------------------------------------------------------------------------- #


def append_index_rows(index_csv: str, rows: list[dict]) -> None:
    new = not os.path.exists(index_csv)
    with open(index_csv, "a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=INDEX_FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)
        fh.flush()


# --------------------------------------------------------------------------- #
# Request building
# --------------------------------------------------------------------------- #


def build_requests(todo: dict[str, str], model: str, chunk_chars: int):
    """Yield (manifest_entry, batch_request_params) for every chunk to translate.

    Returns a list of (entry_dict, params_dict). params_dict is the raw Messages
    request body; it is wrapped into the SDK's typed Request at submit time so
    this function stays import-free for --dry-run."""
    out = []
    counter = 0
    for domain, src_path in todo.items():
        try:
            with open(src_path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            out.append(({"domain": domain, "source_path": src_path,
                         "error": f"read:{e}"}, None))
            continue
        if not text.strip():
            out.append(({"domain": domain, "source_path": src_path,
                         "error": "empty_source"}, None))
            continue

        chunks = chunk_text(text, chunk_chars)
        n_parts = len(chunks)
        for part, chunk in enumerate(chunks):
            custom_id = f"r{counter}"
            counter += 1
            entry = {
                "custom_id": custom_id,
                "domain": domain,
                "part": part,
                "n_parts": n_parts,
                "source_path": src_path,
            }
            params = {
                "model": model,
                "max_tokens": max_tokens_for(len(chunk)),
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": chunk}],
            }
            out.append((entry, params))
    return out


# --------------------------------------------------------------------------- #
# Submit
# --------------------------------------------------------------------------- #


def submit(client, work, batch_dir: str, max_per_batch: int,
           max_bytes_per_batch: int) -> list[str]:
    """Group built requests into batches and submit them. Returns batch ids."""
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
        manifest = BatchManifest(
            batch_id=batch.id,
            created=batch.created_at.isoformat() if hasattr(batch.created_at, "isoformat") else str(batch.created_at),
            status="submitted",
            requests=[e for e, _ in group],
            path=os.path.join(batch_dir, f"{batch.id}.json"),
        )
        manifest.save()
        batch_ids.append(batch.id)
        print(f"  submitted batch {batch.id}  ({len(group):,} requests)",
              file=sys.stderr)
        group, group_bytes = [], 0

    for e, p in real:
        approx = len(p["messages"][0]["content"]) + len(SYSTEM_PROMPT) + 256
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


def _result_text(message) -> str:
    """Concatenate the text blocks of a succeeded batch message."""
    return "".join(b.text for b in message.content if b.type == "text")


def collect_batch(client, manifest: BatchManifest, out_dir: str,
                  index_csv: str) -> dict:
    """Retrieve one ended batch, write per-domain outputs, append index rows.

    Returns counts. Leaves the manifest as 'submitted' if not yet ended so a
    later --collect retries."""
    batch = client.messages.batches.retrieve(manifest.batch_id)
    if batch.processing_status != "ended":
        return {"status": batch.processing_status, "written": 0}

    by_id = {r["custom_id"]: r for r in manifest.requests}
    # domain -> {part: text}, and domain -> n_parts / source_path / errored
    acc: dict[str, dict] = {}
    for r in manifest.requests:
        acc.setdefault(r["domain"], {
            "parts": {}, "n_parts": r["n_parts"],
            "source_path": r["source_path"], "errored": False})

    for result in client.messages.batches.results(manifest.batch_id):
        entry = by_id.get(result.custom_id)
        if entry is None:
            continue
        dom = acc[entry["domain"]]
        if result.result.type == "succeeded":
            dom["parts"][entry["part"]] = _result_text(result.result.message)
        else:
            dom["errored"] = True  # invalid_request / errored / expired / canceled

    rows = []
    written = 0
    for domain, dom in acc.items():
        got_all = (not dom["errored"]
                   and len(dom["parts"]) == dom["n_parts"])
        if not got_all:
            # Don't write a partial file; no output -> domain re-enters todo.
            rows.append({
                "domain": domain, "source_path": dom["source_path"],
                "translated_path": "", "chars_in": "", "chars_out": "",
                "parts": dom["n_parts"], "status": "error",
                "batch_id": manifest.batch_id,
            })
            continue
        text = "\n".join(dom["parts"][i] for i in range(dom["n_parts"]))
        tp = os.path.join(out_dir, f"{safe_filename(domain)}.txt")
        with open(tp, "w", encoding="utf-8") as fh:
            fh.write(text)
        written += 1
        try:
            chars_in = os.path.getsize(dom["source_path"])
        except OSError:
            chars_in = ""
        rows.append({
            "domain": domain, "source_path": dom["source_path"],
            "translated_path": tp, "chars_in": chars_in,
            "chars_out": len(text), "parts": dom["n_parts"],
            "status": "ok", "batch_id": manifest.batch_id,
        })

    append_index_rows(index_csv, rows)
    manifest.status = "collected"
    manifest.save()
    return {"status": "ended", "written": written,
            "errors": sum(1 for r in rows if r["status"] == "error")}


def collect_all(client, batch_dir: str, out_dir: str, index_csv: str,
                poll_seconds: int, wait: bool) -> None:
    """Collect every outstanding batch, optionally polling until all are done."""
    started = time.monotonic()
    while True:
        manifests = [m for m in load_manifests(batch_dir) if m.status != "collected"]
        if not manifests:
            print("all batches collected.", file=sys.stderr)
            return
        ended = 0
        for m in manifests:
            res = collect_batch(client, m, out_dir, index_csv)
            if res["status"] == "ended":
                ended += 1
                print(f"  collected {m.batch_id}: wrote {res['written']:,} "
                      f"domains, {res.get('errors', 0):,} errors", file=sys.stderr)
        remaining = sum(1 for m in load_manifests(batch_dir)
                        if m.status != "collected")
        print(f"[{_fmt_duration(time.monotonic()-started)}] "
              f"outstanding batches: {remaining:,}", file=sys.stderr)
        if remaining == 0:
            print("all batches collected.", file=sys.stderr)
            return
        if not wait:
            print(f"{remaining:,} batch(es) still processing -- re-run "
                  f"--collect later.", file=sys.stderr)
            return
        time.sleep(poll_seconds)


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# Dry run (no API)
# --------------------------------------------------------------------------- #


def dry_run(work, model: str) -> None:
    real = [(e, p) for e, p in work if p is not None]
    skipped = [(e, p) for e, p in work if p is None]
    total_chars = sum(len(p["messages"][0]["content"]) for _, p in real)
    domains = {e["domain"] for e, _ in real}
    # Rough: ~4 chars/token in; English out ~= in tokens; batches halve price.
    in_tok = total_chars / 4
    out_tok = in_tok  # similar length translation
    price = {  # ($/Mtok input, output) -- standard; batches are 50% of this
        "claude-opus-4-8": (5.0, 25.0),
        "claude-sonnet-4-6": (3.0, 15.0),
        "claude-haiku-4-5": (1.0, 5.0),
    }.get(model, (5.0, 25.0))
    full = (in_tok / 1e6) * price[0] + (out_tok / 1e6) * price[1]
    print(f"model               : {model}")
    print(f"domains to translate: {len(domains):,}")
    print(f"requests (chunks)   : {len(real):,}")
    print(f"skipped (empty/err) : {len(skipped):,}")
    print(f"source characters   : {total_chars:,}")
    print(f"~input tokens       : {in_tok:,.0f}")
    print(f"~est. cost (batched): ${full*0.5:,.2f}  "
          f"(full-price would be ${full:,.2f})")
    print("\n(estimate only -- no API calls made; pass without --dry-run to submit)")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Translate scraped main-page text to English via the "
                    "Claude Batches API (resumable).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--index", default="scraped/scraped_index.csv",
                   help="scraper index CSV (default: scraped/scraped_index.csv)")
    p.add_argument("--out", default="translated",
                   help="output directory for translated .txt files")
    p.add_argument("--translation-index", default="translation_index.csv",
                   help="CSV recording every domain processed (resume state)")
    p.add_argument("--batch-dir", default="translation_batches",
                   help="directory of per-batch manifests (resume state)")
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"Claude model id (default {DEFAULT_MODEL}; "
                        f"claude-haiku-4-5 / claude-sonnet-4-6 are far cheaper)")
    p.add_argument("--chunk-chars", type=int, default=12000,
                   help="split source pages longer than this many characters")
    p.add_argument("--max-per-batch", type=int, default=20000,
                   help="max requests per submitted batch")
    p.add_argument("--max-mb-per-batch", type=int, default=180,
                   help="approx max request bytes per batch (< API's 256 MB)")
    p.add_argument("--poll-seconds", type=int, default=60,
                   help="seconds between batch status polls when waiting")
    p.add_argument("--limit", type=int, default=0,
                   help="translate at most this many new domains (0 = all)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="report what would be submitted; no API calls")
    mode.add_argument("--submit", action="store_true",
                      help="submit remaining work as batches, then exit")
    mode.add_argument("--collect", action="store_true",
                      help="poll outstanding batches and write finished results")
    mode.add_argument("--status", action="store_true",
                      help="print outstanding batch statuses")
    return p


def make_client():
    try:
        import anthropic
    except ImportError:
        print("the `anthropic` package is required: pip install anthropic",
              file=sys.stderr)
        raise SystemExit(2)
    if not (os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("set ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN) first.",
              file=sys.stderr)
        raise SystemExit(2)
    return anthropic.Anthropic()


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if not os.path.exists(args.index):
        print(f"scraper index not found: {args.index}  "
              f"(run scrape_main_pages.py first)", file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)

    # --collect / --status don't need the todo set.
    if args.collect:
        client = make_client()
        collect_all(client, args.batch_dir, args.out, args.translation_index,
                    args.poll_seconds, wait=False)
        return 0
    if args.status:
        client = make_client()
        show_status(client, args.batch_dir)
        return 0

    # Discover remaining work for --dry-run / --submit / default.
    ok = load_ok_domains(args.index)
    done = load_done(args.translation_index, args.out)
    in_flight = domains_in_flight(load_manifests(args.batch_dir))
    todo = {d: pth for d, pth in ok.items()
            if d not in done and d not in in_flight}
    if args.limit:
        todo = dict(list(todo.items())[:args.limit])

    print(f"{len(ok):,} ok domains | {len(done):,} already translated | "
          f"{len(in_flight):,} in-flight | {len(todo):,} to submit",
          file=sys.stderr)

    work = build_requests(todo, args.model, args.chunk_chars)

    if args.dry_run:
        dry_run(work, args.model)
        return 0

    client = make_client()
    max_bytes = args.max_mb_per_batch * 1024 * 1024

    if todo:
        print("submitting batches...", file=sys.stderr)
        submit(client, work, args.batch_dir, args.max_per_batch, max_bytes)
    else:
        print("nothing new to submit.", file=sys.stderr)

    if args.submit:
        print("submitted. Run with --collect (or no flag) to retrieve results.",
              file=sys.stderr)
        return 0

    # Default mode: poll to completion and collect everything.
    print("polling for completion (Ctrl-C to stop; re-run --collect later)...",
          file=sys.stderr)
    collect_all(client, args.batch_dir, args.out, args.translation_index,
                args.poll_seconds, wait=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
