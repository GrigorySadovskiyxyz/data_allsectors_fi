#!/usr/bin/env python3
"""
scrape_main_pages.py
====================

Scrape the main-page text of every website that `check_accessible.py` marked as
reachable, using a real headless browser (Playwright + Chromium) for fetching
and trafilatura (https://github.com/adbar/trafilatura) for extraction.

Why a browser instead of a plain HTTP GET
------------------------------------------
`trafilatura.fetch_url` issues a single HTTP GET: it follows HTTP 3xx redirects
but does NOT run JavaScript, does NOT wait for the page to finish loading, and
does NOT follow client-side redirects (meta-refresh / JS `location` changes).
Many sites render their text with JS or bounce through a JS/landing redirect, so
that fetch returns an empty or wrong page.

This version loads each URL in headless Chromium and waits for the page to be
*fully* settled before extracting:
  1. navigate (server + meta + JS redirects are followed automatically),
  2. wait for the `load` event,
  3. wait for `networkidle` (no network traffic for 500ms) so late XHR/JS
     redirects and lazily-rendered content have completed,
  4. re-check whether a client-side redirect changed the URL and, if so, settle
     again.
Only once a site is fully scraped does the script proceed to the next one.

Input
-----
accessibility.csv  -- the output of `check_accessible.py`. Only rows whose
`accessible` column is True are scraped (that boolean is what "status == True"
refers to; the `status` column itself holds the HTTP code).

Output
-------
* scraped/<domain>.txt   : one plain-text file per domain (extracted main page)
* scraped_index.csv      : one row per domain attempted
      domain, url, final_url, ok, chars, reason, text_path

Resumable
---------
Every result is appended to `scraped_index.csv` and each text file is written as
it completes, so the run never starts from the beginning: on re-run, any domain
already present in the index (or with a non-empty text file) is skipped. A long
run can be stopped (Ctrl-C / killed) and resumed without re-scraping.

Requires: trafilatura>=2.0, playwright  (+ `python -m playwright install chromium`)
    pip install trafilatura playwright
    python -m playwright install chromium
    # on Linux you may also need:  sudo python -m playwright install-deps chromium

Usage
-----
    python scrape_main_pages.py
    python scrape_main_pages.py --accessibility accessibility.csv --out scraped
    python scrape_main_pages.py --limit 500 --nav-timeout 30 --idle-timeout 15
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from dataclasses import dataclass, asdict
from typing import Optional

import trafilatura
from trafilatura.settings import use_config
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# Let trafilatura keep even very short extractions; the browser did the fetching.
_CONFIG = use_config()
_CONFIG.set("DEFAULT", "MIN_EXTRACTED_SIZE", "0")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Resource types we don't need for text extraction -- blocking them makes each
# page load dramatically faster without affecting JS-rendered content.
_BLOCKED_RESOURCES = {"image", "media", "font"}

INDEX_FIELDS = ["domain", "url", "final_url", "ok", "chars", "reason", "text_path"]


@dataclass
class ScrapeResult:
    domain: str
    url: str
    final_url: str
    ok: bool
    chars: int
    reason: str
    text_path: str


# --------------------------------------------------------------------------- #
# Input / resume bookkeeping
# --------------------------------------------------------------------------- #


def load_accessible(path: str) -> dict[str, str]:
    """domain -> url to scrape, for rows where accessible is True.

    Prefers `final_url` (post-redirect) and falls back to `homepage`.
    First URL seen per domain wins.
    """
    todo: dict[str, str] = {}
    with open(path, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("accessible") or "").strip().lower() != "true":
                continue
            domain = (row.get("domain") or "").strip()
            url = (row.get("final_url") or row.get("homepage") or "").strip()
            if not domain or not url:
                continue
            todo.setdefault(domain, url)
    return todo


def load_done(index_csv: str) -> set[str]:
    """Domains already recorded in the index file (any outcome)."""
    done: set[str] = set()
    if not os.path.exists(index_csv):
        return done
    with open(index_csv, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("domain"):
                done.add(row["domain"])
    return done


def safe_filename(domain: str) -> str:
    """Filesystem-safe file stem for a domain."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", domain)


# --------------------------------------------------------------------------- #
# The scrape -- one fully-loaded page at a time
# --------------------------------------------------------------------------- #


def _settle(page, idle_ms: int) -> None:
    """Wait for the page to finish loading and for all redirects to complete.

    `load` waits for the load event; `networkidle` waits until there has been no
    network activity for 500ms, which lets late XHR calls and JS-driven
    redirects finish. Each wait is best-effort: long-polling sites that never go
    idle still yield whatever has rendered so far.
    """
    for state in ("load", "networkidle"):
        try:
            page.wait_for_load_state(state, timeout=idle_ms)
        except PWTimeout:
            pass


def scrape_one(page, domain: str, url: str, out_dir: str,
               nav_ms: int, idle_ms: int) -> ScrapeResult:
    text_path = os.path.join(out_dir, f"{safe_filename(domain)}.txt")

    # Belt-and-braces resume: if a non-empty text file already exists, keep it.
    if os.path.exists(text_path) and os.path.getsize(text_path) > 0:
        return ScrapeResult(domain, url, url, True,
                            os.path.getsize(text_path), "cached", text_path)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=nav_ms)
        _settle(page, idle_ms)

        # A client-side (JS/meta) redirect may navigate again after first settle;
        # if the URL changed, wait once more so we extract the destination page.
        seen = page.url
        _settle(page, idle_ms)
        if page.url != seen:
            _settle(page, idle_ms)

        final_url = page.url
        html = page.content()
    except PWTimeout:
        return ScrapeResult(domain, url, "", False, 0, "nav_timeout", "")
    except Exception as e:  # never let one site abort the run
        return ScrapeResult(domain, url, "", False, 0,
                            f"{type(e).__name__}:{str(e)[:40]}", "")

    text = trafilatura.extract(html, url=final_url, config=_CONFIG,
                               favor_recall=True) or ""
    if not text.strip():
        return ScrapeResult(domain, url, final_url, False, 0, "empty_extract", "")

    with open(text_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return ScrapeResult(domain, url, final_url, True, len(text), "ok", text_path)


# --------------------------------------------------------------------------- #
# Resumable runner
# --------------------------------------------------------------------------- #


def run(args) -> dict:
    os.makedirs(args.out, exist_ok=True)
    index_csv = args.index or os.path.join(args.out, "scraped_index.csv")

    todo_all = load_accessible(args.accessibility)
    done = load_done(index_csv)
    todo = {d: u for d, u in todo_all.items() if d not in done}
    if args.limit:
        todo = dict(list(todo.items())[:args.limit])

    new_header = not os.path.exists(index_csv)
    fh = open(index_csv, "a", encoding="utf-8", newline="")
    writer = csv.DictWriter(fh, fieldnames=INDEX_FIELDS)
    if new_header:
        writer.writeheader()
        fh.flush()

    nav_ms = args.nav_timeout * 1000
    idle_ms = args.idle_timeout * 1000
    scraped = ok_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent=USER_AGENT,
                                      ignore_https_errors=True)
        # Skip images/media/fonts -- we only need the DOM text.
        context.route("**/*", lambda route: (
            route.abort() if route.request.resource_type in _BLOCKED_RESOURCES
            else route.continue_()))
        try:
            for domain, url in todo.items():
                page = context.new_page()
                try:
                    res = scrape_one(page, domain, url, args.out, nav_ms, idle_ms)
                except Exception as e:
                    res = ScrapeResult(domain, url, "", False, 0,
                                       f"{type(e).__name__}:{str(e)[:40]}", "")
                finally:
                    page.close()  # fully done with this site before the next one

                writer.writerow(asdict(res))
                fh.flush()  # stay resumable even if killed mid-run
                scraped += 1
                ok_count += int(res.ok)
                if scraped % 25 == 0:
                    print(f"  scraped {scraped:,}/{len(todo):,}  "
                          f"(ok so far: {ok_count:,})", file=sys.stderr)
        finally:
            context.close()
            browser.close()
            fh.close()

    return {
        "accessible_domains": len(todo_all),
        "already_done": len(done),
        "scraped_this_run": scraped,
        "ok_this_run": ok_count,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Scrape fully-loaded main pages of accessible sites "
                    "(headless Chromium + trafilatura).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--accessibility", default="accessibility.csv",
                   help="accessibility.csv from check_accessible.py")
    p.add_argument("--out", default="scraped",
                   help="output directory for per-domain .txt files")
    p.add_argument("--index", default="",
                   help="index CSV (default: <out>/scraped_index.csv); enables resume")
    p.add_argument("--nav-timeout", type=int, default=30,
                   help="max seconds to wait for initial navigation (default 30)")
    p.add_argument("--idle-timeout", type=int, default=15,
                   help="max seconds to wait for load/networkidle settle (default 15)")
    p.add_argument("--limit", type=int, default=0,
                   help="scrape at most this many new domains (0 = all)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not os.path.exists(args.accessibility):
        print(f"accessibility file not found: {args.accessibility}  "
              f"(run `python check_accessible.py` first)", file=sys.stderr)
        return 2
    s = run(args)
    print("\n=== scrape summary ===")
    print(f"accessible domains (status True): {s['accessible_domains']:,}")
    print(f"already scraped (skipped)       : {s['already_done']:,}")
    print(f"scraped this run                : {s['scraped_this_run']:,}")
    print(f"  of which succeeded            : {s['ok_this_run']:,}")
    print(f"\ntext files in {args.out}/  | index: "
          f"{args.index or os.path.join(args.out, 'scraped_index.csv')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
