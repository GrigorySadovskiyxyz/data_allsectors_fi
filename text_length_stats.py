#!/usr/bin/env python3
"""
text_length_stats.py
====================

Perform basic text cleaning on the English-only corpus and produce the
summary-statistics table for text length (the analogue of "Table 4").

Basic text cleaning pipeline (applied per file)
-----------------------------------------------
1. Lowercase.
2. Strip HTML tags (in case any survived scraping).
3. Remove URLs and email addresses.
4. Keep only letters (incl. Finnish a-umlaut / o-umlaut) and whitespace;
   drop digits, punctuation and other symbols.
5. Collapse runs of whitespace to a single space and strip the ends.

Per-file variables
------------------
* Char_clean  : number of characters after cleaning      (len of clean text)
* Words_clean : number of words after cleaning           (clean text split)
* Tokens      : numeric token ids from the cl100k_base
                BPE tokenizer (tiktoken) of the clean text

Output
------
* english_only_text_length.csv : one row per file (domain + 3 variables)
* a LaTeX-style + plain summary table printed to stdout (Mean/Std/Min/Max)

Usage
-----
    python text_length_stats.py
    python text_length_stats.py --dir english_only --out english_only_text_length.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys

import pandas as pd
import tiktoken

# --- cleaning regexes -------------------------------------------------------
HTML_TAG = re.compile(r"<[^>]+>")
URL = re.compile(r"https?://\S+|www\.\S+")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def clean_text(text: str) -> str:
    """Apply the basic text-cleaning pipeline and return the cleaned string."""
    text = text.lower()
    text = HTML_TAG.sub(" ", text)
    text = URL.sub(" ", text)
    text = EMAIL.sub(" ", text)
    # keep letters (any language) and whitespace; drop digits/punct/symbols
    text = re.sub(r"[^a-zà-öø-ÿ\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Text-length summary statistics.")
    p.add_argument("--dir", default="english_only", help="corpus directory")
    p.add_argument("--out", default="english_only_text_length.csv",
                   help="per-file CSV output")
    p.add_argument("--encoding", default="cl100k_base",
                   help="tiktoken encoding name for token counts")
    args = p.parse_args(argv)

    if not os.path.isdir(args.dir):
        print(f"not a directory: {args.dir}", file=sys.stderr)
        return 2

    enc = tiktoken.get_encoding(args.encoding)
    names = sorted(n for n in os.listdir(args.dir) if n.endswith(".txt"))

    rows = []
    total = len(names)
    for i, name in enumerate(names, 1):
        path = os.path.join(args.dir, name)
        with open(path, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        clean = clean_text(raw)
        rows.append({
            "domain": name[:-4],
            "Char_clean": len(clean),
            "Words_clean": len(clean.split()),
            "Tokens": len(enc.encode(clean)),
        })
        if i % 1000 == 0:
            sys.stderr.write(f"\r  processed {i:,}/{total:,}")
            sys.stderr.flush()
    sys.stderr.write("\r" + " " * 40 + "\r")

    df = pd.DataFrame(rows)
    # drop the domain column for stats; keep it in the CSV
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["domain", "Char_clean",
                                           "Words_clean", "Tokens"])
        w.writeheader()
        w.writerows(rows)

    variables = {
        "Char_clean": "Number of characters after text cleaning",
        "Words_clean": "Number of words after text cleaning",
        "Tokens": "Numeric representations of characters",
    }
    stats = df[list(variables)].agg(["mean", "std", "min", "max"]).T

    print(f"\n=== Text-length summary statistics for {args.dir}/ "
          f"(n = {len(df):,} files) ===\n")
    header = f"{'Variable':<12} {'Definition':<42} {'Mean':>8} {'Std':>8} {'Min':>6} {'Max':>8}"
    print(header)
    print("-" * len(header))
    for var, definition in variables.items():
        s = stats.loc[var]
        print(f"{var:<12} {definition:<42} {s['mean']:>8.0f} {s['std']:>8.0f} "
              f"{int(s['min']):>6} {int(s['max']):>8}")

    print("\n--- LaTeX (booktabs) ---")
    print(r"\begin{tabular}{llrrrr}")
    print(r"\toprule")
    print(r"Variable & Definition & Mean & Std & Min & Max \\")
    print(r"\midrule")
    for var, definition in variables.items():
        s = stats.loc[var]
        print(f"{var} & {definition} & {s['mean']:.0f} & {s['std']:.0f} & "
              f"{int(s['min'])} & {int(s['max'])} \\\\")
    print(r"\bottomrule")
    print(r"\end{tabular}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
