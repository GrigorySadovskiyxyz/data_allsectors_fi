#!/usr/bin/env python3
"""
count_symbols.py
================

Count the number of symbols (Unicode characters) in every .txt file in a
directory, for corpus analysis. Characters are counted as Unicode code points,
so Finnish letters like 'ä'/'ö' count as one symbol each (not their multi-byte
UTF-8 encoding).

Output
------
* <dir>_symbol_counts.csv : one row per file
      domain, path, chars, bytes, words, lines
* a summary (totals + min/mean/median/max) printed to stderr

Usage
-----
    python count_symbols.py                 # counts scraped/
    python count_symbols.py --dir translated
    python count_symbols.py --dir scraped --out symbol_counts.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import sys

FIELDS = ["domain", "path", "chars", "bytes", "words", "lines"]


def count_dir(directory: str, out_csv: str) -> dict:
    rows = []
    char_counts = []
    total_chars = total_bytes = total_words = total_lines = 0

    names = sorted(n for n in os.listdir(directory)
                   if n.endswith(".txt") and n != "scraped_index.csv")
    for name in names:
        path = os.path.join(directory, name)
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        text = raw.decode("utf-8", errors="replace")
        chars = len(text)               # symbols = Unicode code points
        nbytes = len(raw)               # raw UTF-8 bytes
        words = len(text.split())
        lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)

        domain = name[:-4] if name.endswith(".txt") else name
        rows.append({"domain": domain, "path": path, "chars": chars,
                     "bytes": nbytes, "words": words, "lines": lines})
        char_counts.append(chars)
        total_chars += chars
        total_bytes += nbytes
        total_words += words
        total_lines += lines

    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    n = len(char_counts)
    return {
        "dir": directory, "out": out_csv, "files": n,
        "total_chars": total_chars, "total_bytes": total_bytes,
        "total_words": total_words, "total_lines": total_lines,
        "min": min(char_counts) if n else 0,
        "max": max(char_counts) if n else 0,
        "mean": statistics.mean(char_counts) if n else 0,
        "median": statistics.median(char_counts) if n else 0,
        "empty": sum(1 for c in char_counts if c == 0),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Count symbols (Unicode characters) in every .txt file.")
    p.add_argument("--dir", default="scraped", help="directory of .txt files")
    p.add_argument("--out", default="",
                   help="output CSV (default: <dir>_symbol_counts.csv)")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not os.path.isdir(args.dir):
        print(f"not a directory: {args.dir}", file=sys.stderr)
        return 2
    out_csv = args.out or f"{args.dir.rstrip('/').replace('/', '_')}_symbol_counts.csv"
    s = count_dir(args.dir, out_csv)

    print(f"=== symbol counts for {s['dir']}/ ===", file=sys.stderr)
    print(f"files                : {s['files']:,}", file=sys.stderr)
    print(f"total symbols (chars): {s['total_chars']:,}", file=sys.stderr)
    print(f"total bytes          : {s['total_bytes']:,}", file=sys.stderr)
    print(f"total words          : {s['total_words']:,}", file=sys.stderr)
    print(f"total lines          : {s['total_lines']:,}", file=sys.stderr)
    print(f"symbols per file     : min {s['min']:,}  median {s['median']:,.0f}  "
          f"mean {s['mean']:,.1f}  max {s['max']:,}", file=sys.stderr)
    print(f"empty files          : {s['empty']:,}", file=sys.stderr)
    print(f"\nper-file CSV written : {s['out']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
