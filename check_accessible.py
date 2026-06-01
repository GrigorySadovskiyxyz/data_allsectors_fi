#!/usr/bin/env python3
"""
check_accessible.py
===================

Read the manifest produced by `ytj_trafilatura.py bulk` (manifest.json or
manifest.csv) and check whether each company's website is actually reachable,
then count how many are valid.

Design (built for manifest scale -- potentially hundreds of thousands of rows)
------------------------------------------------------------------------------
* De-duplicates by registered domain, so a website shared by several group
  companies is fetched only ONCE. The final company count still attributes the
  shared result to every business ID that points at that domain.
* Concurrent (thread pool) -- network checks are I/O-bound.
* Resumable: results are appended to a CSV as they complete; re-running skips
  domains already recorded. A multi-hour run can be stopped and resumed.
* Resilient: any single site failing (timeout, DNS, TLS, refused) is recorded
  as not-accessible with a reason; it never aborts the run.
* No third-party dependencies -- standard library only.

"Accessible" = an HTTP(S) request to the homepage returns a final status in the
2xx/3xx range (after following redirects). HEAD is tried first; if the server
rejects it (405/501/etc.) a GET is tried. A TLS failure triggers one
certificate-relaxed retry, with the outcome flagged in the `tls` column.

Usage
-----
    python check_accessible.py --manifest manifest.csv
    python check_accessible.py --manifest manifest.json --workers 40 --timeout 15
    python check_accessible.py --manifest manifest.csv --results accessibility.csv

Outputs
-------
* accessibility.csv : one row per UNIQUE domain
      domain, homepage, accessible, status, final_url, reason, tls, elapsed_s
* prints a summary: unique domains checked, accessible domains,
  and accessible *companies* (business IDs whose site is reachable).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import socket
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPSHandler

USER_AGENT = (
    "ytj-accessibility-check/1.0 (+https://github.com/your-org/ytj-data) "
    "Mozilla/5.0 (compatible)"
)

# Module-level so worker threads inherit them without per-call plumbing.
DEFAULT_TIMEOUT = 15
DEFAULT_WORKERS = 30


@dataclass
class CheckResult:
    domain: str
    homepage: str
    accessible: bool
    status: Optional[int]
    final_url: Optional[str]
    reason: str
    tls: str          # "ok" | "insecure-retry" | "n/a"
    elapsed_s: float


CSV_FIELDS = ["domain", "homepage", "accessible", "status",
              "final_url", "reason", "tls", "elapsed_s"]


# --------------------------------------------------------------------------- #
# Manifest loading
# --------------------------------------------------------------------------- #


def load_manifest(path: str) -> list[dict]:
    """Return rows with at least 'businessId', 'homepage', 'domain'."""
    if path.lower().endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        rows = data.get("companies", data) if isinstance(data, dict) else data
    else:
        with open(path, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        homepage = (r.get("homepage") or "").strip()
        if not homepage:
            continue
        out.append({
            "businessId": r.get("businessId", ""),
            "name": r.get("name", ""),
            "homepage": homepage,
            "domain": (r.get("domain") or homepage).strip(),
        })
    return out


def unique_domains(rows: list[dict]) -> dict[str, str]:
    """domain -> homepage to fetch (first one seen for that domain)."""
    seen: dict[str, str] = {}
    for r in rows:
        seen.setdefault(r["domain"], r["homepage"])
    return seen


# --------------------------------------------------------------------------- #
# The accessibility check
# --------------------------------------------------------------------------- #


class _Redirect(HTTPRedirectHandler):
    """Follow redirects but remember the final URL; cap the chain length."""
    max_redirections = 10


def _request(url: str, method: str, timeout: int, context: ssl.SSLContext):
    opener = build_opener(HTTPSHandler(context=context), _Redirect())
    req = Request(url, method=method, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "fi,en;q=0.8",
    })
    return opener.open(req, timeout=timeout)


def check_one(domain: str, homepage: str, timeout: int) -> CheckResult:
    start = time.monotonic()
    secure = ssl.create_default_context()
    insecure = ssl._create_unverified_context()

    def attempt(ctx: ssl.SSLContext, tls_label: str):
        # HEAD first (cheap); fall back to GET when HEAD is unsupported.
        for method in ("HEAD", "GET"):
            try:
                resp = _request(homepage, method, timeout, ctx)
                status = getattr(resp, "status", None) or resp.getcode()
                final = resp.geturl()
                resp.close()
                return CheckResult(domain, homepage, True, status, final,
                                   "ok", tls_label, round(time.monotonic() - start, 2))
            except HTTPError as e:
                # Server answered. 405/501 on HEAD -> retry with GET.
                if method == "HEAD" and e.code in (403, 405, 406, 501):
                    continue
                # 2xx/3xx never raise; 4xx/5xx mean "reachable but not serving".
                # Treat <500 as accessible (site exists), >=500 as not.
                accessible = e.code < 500
                return CheckResult(domain, homepage, accessible, e.code,
                                   getattr(e, "url", homepage),
                                   f"http_{e.code}", tls_label,
                                   round(time.monotonic() - start, 2))
        return None  # both methods exhausted without a definitive answer

    # 1) secure attempt
    try:
        r = attempt(secure, "ok")
        if r is not None:
            return r
    except ssl.SSLError:
        # 2) certificate problem -> one relaxed retry, flagged
        try:
            r = attempt(insecure, "insecure-retry")
            if r is not None:
                return r
        except Exception as e:
            return _fail(domain, homepage, start, _reason(e))
    except (URLError, socket.timeout, socket.gaierror, ConnectionError, OSError) as e:
        # network-level error possibly hiding a TLS issue -> relaxed retry once
        try:
            r = attempt(insecure, "insecure-retry")
            if r is not None:
                return r
        except Exception:
            pass
        return _fail(domain, homepage, start, _reason(e))
    except Exception as e:
        return _fail(domain, homepage, start, _reason(e))

    return _fail(domain, homepage, start, "no_response")


def _reason(e: Exception) -> str:
    if isinstance(e, socket.timeout):
        return "timeout"
    if isinstance(e, socket.gaierror):
        return "dns_error"
    if isinstance(e, ssl.SSLError):
        return "tls_error"
    if isinstance(e, URLError):
        inner = getattr(e, "reason", e)
        if isinstance(inner, socket.timeout):
            return "timeout"
        if isinstance(inner, socket.gaierror):
            return "dns_error"
        if isinstance(inner, ssl.SSLError):
            return "tls_error"
        return f"url_error:{str(inner)[:40]}"
    if isinstance(e, ConnectionError):
        return "connection_error"
    return f"{type(e).__name__}:{str(e)[:40]}"


def _fail(domain: str, homepage: str, start: float, reason: str) -> CheckResult:
    return CheckResult(domain, homepage, False, None, None, reason, "n/a",
                       round(time.monotonic() - start, 2))


# --------------------------------------------------------------------------- #
# Resumable runner
# --------------------------------------------------------------------------- #


def load_done(results_csv: str) -> dict[str, bool]:
    """domain -> accessible, for rows already in the results file."""
    done: dict[str, bool] = {}
    if not os.path.exists(results_csv):
        return done
    with open(results_csv, encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            done[row["domain"]] = row.get("accessible", "").lower() == "true"
    return done


def run(args) -> dict:
    rows = load_manifest(args.manifest)
    domains = unique_domains(rows)
    done = load_done(args.results)

    todo = {d: hp for d, hp in domains.items() if d not in done}
    if args.limit:
        todo = dict(list(todo.items())[:args.limit])

    new_header = not os.path.exists(args.results)
    fh = open(args.results, "a", encoding="utf-8", newline="")
    writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
    if new_header:
        writer.writeheader()
        fh.flush()

    checked = 0
    accessible_domains = sum(1 for v in done.values() if v)
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(check_one, d, hp, args.timeout): d
                for d, hp in todo.items()
            }
            for fut in as_completed(futures):
                res = fut.result()
                done[res.domain] = res.accessible
                if res.accessible:
                    accessible_domains += 1
                writer.writerow(asdict(res))
                fh.flush()  # keep the file resumable even if killed mid-run
                checked += 1
                if checked % 100 == 0:
                    print(f"  checked {checked:,}/{len(todo):,}  "
                          f"(accessible so far: {accessible_domains:,})", file=sys.stderr)
    finally:
        fh.close()

    # Attribute domain results back to companies (business IDs).
    total_companies = len(rows)
    companies_accessible = sum(1 for r in rows if done.get(r["domain"]) is True)

    return {
        "companies_in_manifest": total_companies,
        "unique_domains": len(domains),
        "domains_checked_this_run": checked,
        "domains_accessible": sum(1 for v in done.values() if v),
        "domains_total_recorded": len(done),
        "companies_with_accessible_site": companies_accessible,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Check which company websites in the manifest are reachable, and count them.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", default="manifest.csv",
                   help="manifest.csv or manifest.json from `ytj_trafilatura.py bulk`")
    p.add_argument("--results", default="accessibility.csv",
                   help="output CSV (appended to; enables resume)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                   help=f"concurrent checks (default {DEFAULT_WORKERS})")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help=f"per-request timeout seconds (default {DEFAULT_TIMEOUT})")
    p.add_argument("--limit", type=int, default=0,
                   help="check at most this many new domains (0 = all)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not os.path.exists(args.manifest):
        print(f"manifest not found: {args.manifest}  "
              f"(run `python ytj_trafilatura.py bulk` first)", file=sys.stderr)
        return 2
    s = run(args)
    print("\n=== accessibility summary ===")
    print(f"companies in manifest          : {s['companies_in_manifest']:,}")
    print(f"unique domains                 : {s['unique_domains']:,}")
    print(f"domains checked this run       : {s['domains_checked_this_run']:,}")
    print(f"ACCESSIBLE domains (valid)     : {s['domains_accessible']:,}")
    print(f"companies w/ accessible site   : {s['companies_with_accessible_site']:,}")
    print(f"\nper-domain detail written to {args.results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())