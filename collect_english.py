#!/usr/bin/env python3
"""
collect_english.py
==================

Build a separate collection of the **English-only** scraped websites and count
them. Each scraped page's language is detected locally (langdetect, no API
cost); pages detected as English are copied into `english_only/`, and every
other language (Finnish, Swedish, etc.) is discarded from this collection.

"Discard" = excluded from the English collection. The original files in
`scraped/` are left untouched -- nothing is deleted.

Language detection
------------------
* langdetect, with a fixed seed for reproducible results.
* Detection runs on the first --sample-chars characters (fast + reliable on
  large pages).
* Pages shorter than --min-chars are too short to detect reliably and are
  recorded as `short` (excluded, not English).
* Detection failures are recorded as `unknown` (excluded).

Output
------
* english_only/<domain>.txt   : copy of each English page
* english_index.csv           : one row per scraped file
      domain, lang, chars, english, source_path, english_path
* a summary (English count + language breakdown) printed to stderr

Usage
-----
    pip install langdetect
    python collect_english.py                      # scans scraped/
    python collect_english.py --dir scraped --out-dir english_only
    python collect_english.py --min-chars 30
"""

from __future__ import annotations

import argparse
import csv
import os
import shutil
import sys
from collections import Counter

from langdetect import detect, DetectorFactory
from langdetect.lang_detect_exception import LangDetectException

DetectorFactory.seed = 0  # deterministic results

csv.field_size_limit(16 * 1024 * 1024)

FIELDS = ["domain", "lang", "chars", "english", "source_path", "english_path"]

# Bot-verification / JS-challenge interstitials the scraper sometimes captured
# instead of real page content. These are in English but are not websites'
# actual content, so they are excluded from the English collection by default.
JUNK_MARKERS = (
    "your request is being verified",
    "just a moment",
    "enable javascript and cookies to continue",
    "checking your browser",
    "verifying you are human",
    "review the security of your connection",
    "performance & security by cloudflare",
    "ddos protection by",
    "attention required",
)


def is_junk(text: str) -> bool:
    """True if the page looks like a verification/challenge interstitial."""
    head = text[:2000].lower()
    return any(m in head for m in JUNK_MARKERS)


def detect_lang(text: str, sample_chars: int, min_chars: int) -> str:
    """Return an ISO-639-1 code, or 'short' / 'unknown'."""
    stripped = text.strip()
    if len(stripped) < min_chars:
        return "short"
    try:
        return detect(stripped[:sample_chars])
    except LangDetectException:
        return "unknown"


def run(directory: str, out_dir: str, index_csv: str,
        sample_chars: int, min_chars: int, drop_junk: bool) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    names = sorted(n for n in os.listdir(directory)
                   if n.endswith(".txt") and n != "scraped_index.csv")

    lang_counter: Counter[str] = Counter()
    english = 0
    junk = 0
    total = len(names)

    fh = open(index_csv, "w", encoding="utf-8", newline="")
    writer = csv.DictWriter(fh, fieldnames=FIELDS)
    writer.writeheader()

    for i, name in enumerate(names, 1):
        src = os.path.join(directory, name)
        try:
            with open(src, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        domain = name[:-4]
        if drop_junk and is_junk(text):
            lang = "blocked"
            junk += 1
        else:
            lang = detect_lang(text, sample_chars, min_chars)
        lang_counter[lang] += 1
        is_en = lang == "en"
        eng_path = ""
        if is_en:
            eng_path = os.path.join(out_dir, name)
            shutil.copyfile(src, eng_path)
            english += 1
        writer.writerow({
            "domain": domain, "lang": lang, "chars": len(text),
            "english": is_en, "source_path": src, "english_path": eng_path,
        })
        if i % 2000 == 0:
            sys.stderr.write(f"\r  scanned {i:,}/{total:,}  english so far: {english:,}")
            sys.stderr.flush()
    sys.stderr.write("\r" + " " * 60 + "\r")
    fh.close()

    return {"total": total, "english": english, "junk": junk,
            "languages": lang_counter, "out_dir": out_dir, "index": index_csv}


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Collect English-only scraped sites and count them.")
    p.add_argument("--dir", default="scraped", help="directory of scraped .txt")
    p.add_argument("--out-dir", default="english_only",
                   help="destination for English page copies")
    p.add_argument("--index", default="english_index.csv",
                   help="per-file language index CSV")
    p.add_argument("--sample-chars", type=int, default=5000,
                   help="detect language on the first N characters")
    p.add_argument("--min-chars", type=int, default=25,
                   help="pages shorter than this are 'short' (excluded)")
    p.add_argument("--keep-junk", action="store_true",
                   help="keep bot-verification/challenge interstitial pages "
                        "(by default these are detected and excluded)")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not os.path.isdir(args.dir):
        print(f"not a directory: {args.dir}", file=sys.stderr)
        return 2
    s = run(args.dir, args.out_dir, args.index, args.sample_chars,
            args.min_chars, drop_junk=not args.keep_junk)

    total = s["total"]
    eng = s["english"]
    pct = (eng / total * 100) if total else 0.0
    print(f"=== English-only collection from {args.dir}/ ===")
    print(f"total scraped files : {total:,}")
    print(f"ENGLISH websites    : {eng:,}  ({pct:.1f}% of scraped)")
    if not args.keep_junk:
        print(f"excluded as junk    : {s['junk']:,}  "
              f"(bot-verification / challenge interstitials)")
    print(f"copied to           : {s['out_dir']}/")
    print(f"index               : {s['index']}")
    print("\ntop detected languages:")
    for lang, n in s["languages"].most_common(12):
        print(f"  {lang:<8} {n:>7,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
