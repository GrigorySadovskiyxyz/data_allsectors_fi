#!/usr/bin/env python3
"""
ytj_trafilatura.py
==================

Extract the main page + first-level subpages of the websites belonging to
companies in the Finnish Business Information System (YTJ / PRH).

Pipeline
--------
1. Fetch company records from the PRH open-data API v3
   (https://avoindata.prh.fi/opendata-ytj-api/v3) -- either by an explicit
   list of business IDs or by a search (name / location / company form /
   business line). The v3 API returns a `website` field directly, so there is
   NO need to scrape the tietopalvelu.ytj.fi frontend with Selenium.
2. Keep only companies that actually have a website URL.
3. De-duplicate:
     * companies      -> by business ID
     * websites/crawl -> by registered domain (group companies sharing one
                         site are crawled once, listed once per business ID)
4. For each unique website use trafilatura to:
     * fetch + extract the homepage,
     * discover first-level subpages (same domain, one click from the home
       page; social/external links and asset files are dropped by courlan),
     * fetch + extract every subpage.
5. Write a de-duplicated JSON (full text) and a CSV summary.

Why this differs from the old repo
-----------------------------------
* Old `bis/v1` endpoint is deprecated -> uses `opendata-ytj-api/v3`.
* Website comes straight from the API -> the brittle Selenium scrape
  (keyed off the React class "btn btn-primary false mr-2") is gone.
* One stack instead of three (requests+bs4 / pyppeteer / selenium) ->
  trafilatura for fetch+extract, courlan for URL normalisation & dedup.
* TLS verification stays ON; it is only relaxed on an explicit retry
  (replaces the blanket `verify=False`).

Requires: trafilatura>=2.0  (pulls in courlan + lxml)
    pip install trafilatura
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass, field, asdict
from typing import Iterable, Iterator, Optional
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

import trafilatura
from trafilatura.settings import use_config
from courlan import extract_links, normalize_url, extract_domain, check_url

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

API_BASE = "https://avoindata.prh.fi/opendata-ytj-api/v3"
USER_AGENT = (
    "ytj-trafilatura/1.0 (research crawler; +https://github.com/your-org/ytj-data)"
)

DEFAULTS = dict(
    max_companies=200,   # cap on companies pulled from a search
    max_subpages=25,     # cap on first-level subpages crawled per site
    max_depth=0,         # 0 = any same-domain link on the homepage;
                         # 1 = only paths with a single segment (/about), etc.
    delay=2.0,           # seconds between requests to the SAME website
    api_delay=0.3,       # seconds between PRH API calls (API allows ~300/min)
    timeout=20,          # per-request download timeout (seconds)
    languages=None,      # e.g. ["fi", "en"] to keep only those; None = keep all
)


def build_trafilatura_config(delay: float, timeout: int):
    cfg = use_config()
    cfg.set("DEFAULT", "USER_AGENTS", USER_AGENT)
    cfg.set("DEFAULT", "DOWNLOAD_TIMEOUT", str(int(timeout)))
    cfg.set("DEFAULT", "SLEEP_TIME", str(delay))
    return cfg


# --------------------------------------------------------------------------- #
# PRH / YTJ open-data API v3 client
# --------------------------------------------------------------------------- #


@dataclass
class Company:
    business_id: str
    name: str
    website: Optional[str]
    company_form: Optional[str] = None
    status: Optional[str] = None

    @property
    def has_website(self) -> bool:
        return bool(self.website and self.website.strip())


def _primary_name(company_json: dict) -> str:
    """Pick the current primary company name (type 'toiminimi', version 1)."""
    names = company_json.get("names") or []
    # version 1 == current; type "1" is the primary trade name in v3
    current = [n for n in names if n.get("version") == 1]
    pool = current or names
    for n in pool:
        if n.get("type") in ("1", None) and n.get("name"):
            return n["name"]
    return pool[0]["name"] if pool and pool[0].get("name") else ""


def _website(company_json: dict) -> Optional[str]:
    site = company_json.get("website") or {}
    url = (site.get("url") or "").strip()
    return url or None


def _company_form(company_json: dict) -> Optional[str]:
    forms = company_json.get("companyForms") or []
    current = [f for f in forms if f.get("version") == 1] or forms
    if not current:
        return None
    descs = current[0].get("descriptions") or []
    en = [d.get("description") for d in descs if d.get("languageCode") == "3"]
    return (en[0] if en else current[0].get("type"))


def parse_company(company_json: dict) -> Company:
    return Company(
        business_id=(company_json.get("businessId") or {}).get("value", ""),
        name=_primary_name(company_json),
        website=_website(company_json),
        company_form=_company_form(company_json),
        status=company_json.get("tradeRegisterStatus"),
    )


class YTJClient:
    """Thin client over the PRH open-data API v3 with 429 backoff."""

    def __init__(self, api_delay: float = DEFAULTS["api_delay"], timeout: int = 20):
        self.api_delay = api_delay
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        from urllib.parse import urlencode
        query = urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{API_BASE}{path}?{query}" if query else f"{API_BASE}{path}"
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        backoff = 2.0
        for attempt in range(5):
            try:
                with urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except HTTPError as e:
                if e.code == 429:  # rate limited -> exponential backoff
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                if e.code == 404:
                    return {"totalResults": 0, "companies": []}
                raise
            except URLError as e:
                if attempt == 4:
                    raise
                time.sleep(backoff)
                backoff *= 2
        return {"totalResults": 0, "companies": []}

    def get_by_business_id(self, business_id: str) -> Optional[Company]:
        data = self._get("/companies", {"businessId": business_id})
        time.sleep(self.api_delay)
        companies = data.get("companies") or []
        return parse_company(companies[0]) if companies else None

    def search(
        self,
        name: Optional[str] = None,
        location: Optional[str] = None,
        company_form: Optional[str] = None,
        main_business_line: Optional[str] = None,
        post_code: Optional[str] = None,
        max_companies: int = DEFAULTS["max_companies"],
    ) -> Iterator[Company]:
        """Paginate /companies (100 per page) until max_companies reached."""
        page = 1
        seen = 0
        while seen < max_companies:
            data = self._get(
                "/companies",
                {
                    "name": name,
                    "location": location,
                    "companyForm": company_form,
                    "mainBusinessLine": main_business_line,
                    "postCode": post_code,
                    "page": page,
                },
            )
            time.sleep(self.api_delay)
            companies = data.get("companies") or []
            if not companies:
                break
            for c in companies:
                yield parse_company(c)
                seen += 1
                if seen >= max_companies:
                    return
            page += 1


# --------------------------------------------------------------------------- #
# Crawling / extraction with trafilatura + courlan
# --------------------------------------------------------------------------- #


@dataclass
class Page:
    url: str
    title: Optional[str] = None
    language: Optional[str] = None
    text: Optional[str] = None
    error: Optional[str] = None


@dataclass
class SiteResult:
    homepage: str
    domain: Optional[str]
    main_page: Optional[Page] = None
    subpages: list[Page] = field(default_factory=list)
    error: Optional[str] = None


def _depth(url: str) -> int:
    path = urlsplit(url).path.strip("/")
    return 0 if not path else len([p for p in path.split("/") if p])


def discover_first_level(
    homepage_html: str,
    homepage_url: str,
    max_subpages: int,
    max_depth: int = 0,
) -> list[str]:
    """Same-domain links found on the homepage = first-level subpages.

    courlan.extract_links(external_bool=False) returns a de-duplicated set of
    internal links with assets (.pdf/.jpg/...) and obvious junk already removed.
    """
    links = extract_links(
        pagecontent=homepage_html,
        url=homepage_url,
        external_bool=False,   # internal links only -> drops facebook/linkedin/etc.
        with_nav=True,
        strict=True,
    )
    home_norm = normalize_url(homepage_url).rstrip("/")
    out: dict[str, str] = {}  # canonical key -> stored url, ordered dedup
    for link in links:
        norm = normalize_url(link)
        if not norm:
            continue
        canon = norm.rstrip("/")
        if not canon or canon == home_norm:
            continue
        if max_depth and _depth(norm) > max_depth:
            continue
        out.setdefault(canon, norm)
    return list(out.values())[:max_subpages]


def extract_page(url: str, cfg, languages: Optional[list[str]] = None) -> Page:
    html = trafilatura.fetch_url(url, config=cfg)
    if html is None:  # one retry without strict TLS (http/expired-cert sites)
        html = trafilatura.fetch_url(url, config=cfg, no_ssl=True)
    if html is None:
        return Page(url=url, error="download_failed")

    record = trafilatura.bare_extraction(
        html,
        url=url,
        config=cfg,
        with_metadata=True, as_dict=True,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    if not record:
        return Page(url=url, error="extraction_failed")

    lang = record.get("language")
    if languages and lang and lang not in languages:
        return Page(url=url, language=lang, error="language_filtered")

    return Page(
        url=url,
        title=record.get("title"),
        language=lang,
        text=record.get("text"),
    )


def crawl_site(
    homepage: str,
    cfg,
    max_subpages: int,
    max_depth: int,
    delay: float,
    languages: Optional[list[str]],
) -> SiteResult:
    domain = extract_domain(homepage)
    result = SiteResult(homepage=homepage, domain=domain)

    home_html = trafilatura.fetch_url(homepage, config=cfg)
    if home_html is None:
        home_html = trafilatura.fetch_url(homepage, config=cfg, no_ssl=True)
    if home_html is None:
        result.error = "homepage_unreachable"
        return result

    # main page text from the HTML we already have (no second download)
    main_rec = trafilatura.bare_extraction(
        home_html, url=homepage, config=cfg, with_metadata=True, as_dict=True,
        include_comments=False, include_tables=True, favor_precision=True,
    )
    if main_rec:
        result.main_page = Page(
            url=homepage,
            title=main_rec.get("title"),
            language=main_rec.get("language"),
            text=main_rec.get("text"),
        )
    else:
        result.main_page = Page(url=homepage, error="extraction_failed")

    sub_urls = discover_first_level(home_html, homepage, max_subpages, max_depth)
    for u in sub_urls:
        time.sleep(delay)  # polite, per-site
        result.subpages.append(extract_page(u, cfg, languages))
    return result


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def site_key(homepage: str) -> str:
    """Stable de-dup key for a website: registered domain when resolvable,
    otherwise the host with a leading 'www.' stripped (so www/non-www and
    http/https variants of the same site collapse to one crawl)."""
    dom = extract_domain(homepage)
    if dom:
        return dom
    host = (urlsplit(homepage).netloc or homepage).lower()
    return host[4:] if host.startswith("www.") else host


def normalise_homepage(raw: str) -> Optional[str]:
    """Turn a raw YTJ website value into a clean, fetchable homepage URL."""
    raw = raw.strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("/")
    checked = check_url(raw)  # (url, host) or None
    return checked[0] if checked else normalize_url(raw)


def gather_companies(client: YTJClient, args) -> list[Company]:
    companies: list[Company] = []
    if args.business_ids:
        for bid in args.business_ids:
            c = client.get_by_business_id(bid.strip())
            if c:
                companies.append(c)
    if args.ids_file:
        with open(args.ids_file, encoding="utf-8") as fh:
            for line in fh:
                bid = line.strip()
                if bid:
                    c = client.get_by_business_id(bid)
                    if c:
                        companies.append(c)
    if any([args.search_name, args.location, args.company_form, args.business_line, args.post_code]):
        companies.extend(
            client.search(
                name=args.search_name,
                location=args.location,
                company_form=args.company_form,
                main_business_line=args.business_line,
                post_code=args.post_code,
                max_companies=args.max_companies,
            )
        )
    return companies


def dedupe_companies(companies: Iterable[Company]) -> list[Company]:
    seen: set[str] = set()
    out: list[Company] = []
    for c in companies:
        if c.business_id and c.business_id not in seen:
            seen.add(c.business_id)
            out.append(c)
    return out


def run(args) -> dict:
    cfg = build_trafilatura_config(args.delay, args.timeout)
    client = YTJClient(api_delay=args.api_delay, timeout=args.timeout)

    companies = dedupe_companies(gather_companies(client, args))
    with_site = [c for c in companies if c.has_website]

    # de-duplicate the actual crawl by registered domain (group companies)
    site_cache: dict[str, SiteResult] = {}
    records = []
    for c in with_site:
        homepage = normalise_homepage(c.website)
        if not homepage:
            continue
        domain = site_key(homepage)
        if domain not in site_cache:
            site_cache[domain] = crawl_site(
                homepage, cfg, args.max_subpages, args.max_depth, args.delay, args.languages
            )
        site = site_cache[domain]
        records.append(
            {
                "businessId": c.business_id,
                "name": c.name,
                "companyForm": c.company_form,
                "status": c.status,
                "homepage": site.homepage,
                "domain": site.domain,
                "shared_site": sum(1 for r in records if r.get("domain") == site.domain) > 0,
                "site": {
                    "error": site.error,
                    "main_page": asdict(site.main_page) if site.main_page else None,
                    "subpages": [asdict(p) for p in site.subpages],
                    "subpage_count": len(site.subpages),
                },
            }
        )

    return {
        "summary": {
            "companies_fetched": len(companies),
            "with_website": len(with_site),
            "unique_sites_crawled": len(site_cache),
            "companies_in_output": len(records),
        },
        "companies": records,
    }


def write_outputs(result: dict, out_json: str, out_csv: str) -> None:
    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)

    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["businessId", "name", "companyForm", "homepage", "domain",
                    "subpage_count", "languages", "site_error"])
        for r in result["companies"]:
            site = r["site"]
            langs = set()
            if site["main_page"] and site["main_page"].get("language"):
                langs.add(site["main_page"]["language"])
            for p in site["subpages"]:
                if p.get("language"):
                    langs.add(p["language"])
            w.writerow([
                r["businessId"], r["name"], r["companyForm"], r["homepage"],
                r["domain"], site["subpage_count"], "|".join(sorted(langs)),
                site["error"] or "",
            ])


# --------------------------------------------------------------------------- #
# Bulk mode: ALL companies on the Finnish Trade Register
# --------------------------------------------------------------------------- #

ALL_COMPANIES_URL = f"{API_BASE}/all_companies"


def download_all_companies(dest: str, timeout: int = 1200) -> str:
    """Stream the full register (a ZIP, ~hundreds of MB) to `dest`."""
    req = Request(ALL_COMPANIES_URL, headers={"User-Agent": USER_AGENT})
    tmp = dest + ".part"
    with urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
    os.replace(tmp, dest)
    return dest


def _open_bulk_stream(path: str):
    """Return (binary_stream, closer) for the JSON inside a .zip or a raw .json."""
    if path.lower().endswith(".zip") or zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        names = [n for n in zf.namelist() if n.lower().endswith(".json")] or zf.namelist()
        return zf.open(names[0]), zf
    return open(path, "rb"), None


def _peek_first_char(path: str) -> bytes:
    stream, holder = _open_bulk_stream(path)
    try:
        b = stream.read(1)
        while b and b.isspace():
            b = stream.read(1)
        return b
    finally:
        stream.close()
        if holder:
            holder.close()


def iter_bulk_companies(path: str):
    """Stream company dicts from the bulk file, whatever the top-level shape:
    a bare JSON array `[ {...} ]` or an object `{"companies": [ {...} ]}`."""
    import ijson  # lazy: only bulk mode needs it

    prefix = "item" if _peek_first_char(path) == b"[" else "companies.item"
    stream, holder = _open_bulk_stream(path)
    try:
        yield from ijson.items(stream, prefix)
    finally:
        stream.close()
        if holder:
            holder.close()


def build_manifest(path: str, out_json: str, out_csv: str) -> dict:
    """Scan the whole register, keep companies with a website, de-duplicate by
    business ID, and record which registered domain each maps to.

    This *is* the 'list of every company without duplicates' for all of Finland.
    """
    seen_sites: dict[str, str] = {}   # domain -> first homepage seen
    seen_ids: set[str] = set()
    rows: list[dict] = []
    n_total = 0

    for cj in iter_bulk_companies(path):
        n_total += 1
        c = parse_company(cj)
        if not c.business_id or c.business_id in seen_ids or not c.has_website:
            continue
        homepage = normalise_homepage(c.website)
        if not homepage:
            continue
        seen_ids.add(c.business_id)
        key = site_key(homepage)
        shared = key in seen_sites
        seen_sites.setdefault(key, homepage)
        rows.append({
            "businessId": c.business_id, "name": c.name,
            "companyForm": c.company_form, "status": c.status,
            "homepage": homepage, "domain": key, "shared_site": shared,
        })
        if n_total % 50000 == 0:
            print(f"  scanned {n_total:,}  kept {len(rows):,} (with website)...", file=sys.stderr)

    with open(out_json, "w", encoding="utf-8") as fh:
        json.dump({"total_scanned": n_total, "companies": rows}, fh, ensure_ascii=False, indent=2)
    with open(out_csv, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["businessId", "name", "companyForm", "status", "homepage", "domain", "shared_site"])
        for r in rows:
            w.writerow([r["businessId"], r["name"], r["companyForm"], r["status"],
                        r["homepage"], r["domain"], int(r["shared_site"])])

    return {"total_scanned": n_total, "with_website": len(rows),
            "unique_sites": len(seen_sites), "companies": len(rows)}


# --------------------------------------------------------------------------- #
# Crawl mode: crawl the sites listed in a manifest (resumable, batched)
# --------------------------------------------------------------------------- #


def _safe_filename(domain: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", domain)[:120] or "site"


def crawl_from_manifest(args) -> dict:
    cfg = build_trafilatura_config(args.delay, args.timeout)
    os.makedirs(args.out_dir, exist_ok=True)

    with open(args.manifest, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    # one crawl per unique domain; keep the first company seen for that domain
    site_for: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        d = r.get("domain") or site_key(r["homepage"])
        if d not in site_for:
            site_for[d] = r
            order.append(d)

    batch = order[args.offset:] if not args.limit else order[args.offset: args.offset + args.limit]
    crawled = skipped = failed = 0

    for i, d in enumerate(batch, 1):
        r = site_for[d]
        out_path = os.path.join(args.out_dir, _safe_filename(d) + ".json")
        if os.path.exists(out_path) and not args.overwrite:
            skipped += 1
            continue
        try:
            site = crawl_site(r["homepage"], cfg, args.max_subpages,
                              args.max_depth, args.delay, args.languages)
            payload = {
                "domain": d,
                "homepage": site.homepage,
                "companies": [c for c in rows if (c.get("domain") or site_key(c["homepage"])) == d],
                "error": site.error,
                "main_page": asdict(site.main_page) if site.main_page else None,
                "subpages": [asdict(p) for p in site.subpages],
                "subpage_count": len(site.subpages),
            }
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            crawled += 1
        except Exception as e:  # never let one site kill a multi-day run
            failed += 1
            with open(out_path + ".error", "w", encoding="utf-8") as fh:
                fh.write(f"{type(e).__name__}: {e}\n")
        if i % 100 == 0:
            print(f"  {i}/{len(batch)} done (crawled {crawled}, skipped {skipped}, failed {failed})",
                  file=sys.stderr)

    return {"unique_sites_total": len(order), "in_this_batch": len(batch),
            "crawled": crawled, "skipped_existing": skipped, "failed": failed}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _add_crawl_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--max-subpages", type=int, default=DEFAULTS["max_subpages"])
    p.add_argument("--max-depth", type=int, default=DEFAULTS["max_depth"],
                   help="0 = any homepage link; 1 = single-segment paths only")
    p.add_argument("--delay", type=float, default=DEFAULTS["delay"],
                   help="seconds between requests to one site")
    p.add_argument("--timeout", type=int, default=DEFAULTS["timeout"])
    p.add_argument("--languages", nargs="*", default=DEFAULTS["languages"],
                   help="keep only these languages, e.g. fi en (default: keep all)")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Extract main + first-level subpages of Finnish companies' "
                    "websites (PRH/YTJ open data v3 + trafilatura).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    # bulk: build the deduplicated list of ALL companies with a website
    b = sub.add_parser("bulk", help="ALL companies: build the deduplicated website manifest")
    b.add_argument("--download", action="store_true",
                   help="download the full register first (needs internet to avoindata.prh.fi)")
    b.add_argument("--input", default="all_companies.zip",
                   help="path to the downloaded register (.zip or .json)")
    b.add_argument("--out-json", default="manifest.json")
    b.add_argument("--out-csv", default="manifest.csv")

    # crawl: crawl the sites in a manifest (resumable, batched)
    c = sub.add_parser("crawl", help="crawl the websites listed in a manifest (resumable)")
    c.add_argument("--manifest", default="manifest.csv")
    c.add_argument("--out-dir", default="crawl_out", help="one JSON per site is written here")
    c.add_argument("--offset", type=int, default=0, help="skip this many unique sites")
    c.add_argument("--limit", type=int, default=0, help="crawl at most this many sites (0 = all)")
    c.add_argument("--overwrite", action="store_true", help="re-crawl sites already done")
    _add_crawl_opts(c)

    # search: targeted query (kept from the original tool)
    s = sub.add_parser("search", help="targeted: business IDs or a filtered search, then crawl")
    s.add_argument("--business-ids", nargs="*", help="explicit business IDs, e.g. 2811294-4")
    s.add_argument("--ids-file", help="text file with one business ID per line")
    s.add_argument("--search-name")
    s.add_argument("--location")
    s.add_argument("--company-form", help="e.g. OY, OYJ")
    s.add_argument("--business-line", help="TOL 2008 code, e.g. 28990")
    s.add_argument("--post-code")
    s.add_argument("--max-companies", type=int, default=DEFAULTS["max_companies"])
    s.add_argument("--api-delay", type=float, default=DEFAULTS["api_delay"])
    s.add_argument("--out-json", default="ytj_company_pages.json")
    s.add_argument("--out-csv", default="ytj_company_pages.csv")
    _add_crawl_opts(s)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.command == "bulk":
        if args.download:
            print(f"downloading {ALL_COMPANIES_URL} -> {args.input} ...", file=sys.stderr)
            download_all_companies(args.input)
        if not os.path.exists(args.input):
            print(f"bulk file not found: {args.input}\n"
                  f"Run with --download, or fetch it from {ALL_COMPANIES_URL}", file=sys.stderr)
            return 2
        s = build_manifest(args.input, args.out_json, args.out_csv)
        print(f"scanned={s['total_scanned']:,}  with_website={s['with_website']:,}  "
              f"unique_sites={s['unique_sites']:,}")
        print(f"wrote {args.out_csv} and {args.out_json}  (this is the deduplicated company list)")
        return 0

    if args.command == "crawl":
        if not os.path.exists(args.manifest):
            print(f"manifest not found: {args.manifest}  (run the `bulk` command first)", file=sys.stderr)
            return 2
        s = crawl_from_manifest(args)
        print(f"unique_sites={s['unique_sites_total']:,}  batch={s['in_this_batch']:,}  "
              f"crawled={s['crawled']:,}  skipped_existing={s['skipped_existing']:,}  failed={s['failed']:,}")
        print(f"per-site JSON written to {args.out_dir}/")
        return 0

    if args.command == "search":
        if not any([args.business_ids, args.ids_file, args.search_name, args.location,
                    args.company_form, args.business_line, args.post_code]):
            print("Provide a source, e.g. --business-ids 2811294-4 or --business-line 28990.",
                  file=sys.stderr)
            return 2
        result = run(args)
        write_outputs(result, args.out_json, args.out_csv)
        s = result["summary"]
        print(f"fetched={s['companies_fetched']}  with_website={s['with_website']}  "
              f"unique_sites={s['unique_sites_crawled']}  rows={s['companies_in_output']}")
        if s["with_website"] == 0 and s["companies_fetched"] > 0:
            print("note: companies were found but none had a website registered with PRH. "
                  "For all of Finland use the `bulk` command instead.", file=sys.stderr)
        print(f"wrote {args.out_json} and {args.out_csv}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())