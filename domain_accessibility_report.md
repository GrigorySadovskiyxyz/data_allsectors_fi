# Domain Accessibility Report

_Generated: 2026-06-17_

## Summary

| Metric | Count | Share of company-list domains |
|---|---:|---:|
| Company entries in list (`manifest.csv`) | 118,606 | — |
| **Unique domains in company list** | **67,933** | 100.00% |
| Domains reachable (accessibility precheck) | 52,828 | 77.76% |
| Domains attempted for content scrape | 52,828 | 77.76% |
| **Accessible domains successfully scraped (ok=True)** | **50,076** | **73.71%** |

## Headline ratio

**Accessible (successfully scraped) domains / total company-list domains = 50,076 / 67,933 = 73.71%**

- Reachable-but-not-scraped (timeouts, crashes, empty extracts): 2,752
- Unreachable domains (failed accessibility precheck): 15,105 (22.24%)

## Definitions

- **Company list** — `manifest.csv`, 118,606 company entries resolving to 67,933 unique homepage domains (many companies, e.g. housing corporations, share a site).
- **Reachable** — `accessible=True` in `accessibility.csv` (HTTP-level reachability precheck).
- **Accessible / scraped** — `ok=True` in `scraped/scraped_index.csv`: the page loaded and usable text was extracted. This is the strictest, most meaningful "accessible domain" figure.
- Cross-check: all 50,076 successfully scraped domains belong to the company list (0 orphans).

## Failure breakdown (attempted but ok=False)

| Reason | Count |
|---|---:|
| empty_extract | 1,296 |
| Error:Navigation failed because page cra | 801 |
| nav_timeout | 434 |
| ERR_NAME_NOT_RESOLVED | 97 |
| Error:Page.goto: Page crashed
Call log:
 | 85 |
| ERR_TOO_MANY_REDIRECTS | 13 |
| ERR_CONNECTION_REFUSED | 11 |
| ERR_CONNECTION_CLOSED | 9 |
| ERR_CONNECTION_RESET | 8 |
| ERR_SSL_UNRECOGNIZED_NAM | 6 |
| ERR_HTTP2_PROTOCOL_ERROR | 3 |
| ERR_SSL_VERSION_OR_CIPHE | 3 |
| ERR_SSL_PROTOCOL_ERROR | 2 |
| Error:Page.content: Unable to retrieve c | 2 |
| Error:Page.content: Target crashed | 1 |
| ERR_ABORTED; | 1 |
| TargetClosedError:Page.content: Target page, context or br | 1 |
| ERR_EMPTY_RESPONSE | 1 |
| Error:Page.goto: Download is starting
Ca | 1 |
