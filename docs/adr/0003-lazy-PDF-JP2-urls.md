# ADR 0003: Fetch loc.gov PDF/JP2 URLs lazily at download time

Status: Proposed
Date: 2026-07-06

## Context
loc.gov search results do NOT contain downloadable PDF/JP2 URLs. This is
confirmed by the live test `TestLocGovImageUrlsLive`: the authoritative image
URLs (`resource.pdf`, `resource.image`) and the OCR text URL
(`resource.fulltext_file`) are only present on a per-page **item-detail** response
(`<page-url>?fo=json`), which requires a second request per page. Cataloguing
often stores far more pages than are ever downloaded, so fetching item detail for
every page at search time would multiply API calls wastefully — a rate-limiting
concern given LOC's limits. (The legacy API returned these URLs directly in
search results, so it needs no such step.)

## Decision
Store `pdf_url` / `jp2_url` as NULL when a loc.gov page is catalogued from search.
Defer the item-detail fetch to download time: in
`DownloadProcessor._download_page`, when `pdf_url` is NULL, make the item-detail
request and enrich the record before downloading. Route this through an
`enrich_from_detail` hook on `ResponseProcessor` (legacy: no-op; loc.gov: reads
`resource.pdf` / `resource.image` / `resource.fulltext_file`). Add a
`fulltext_url` column so OCR text can be fetched separately from the image files.

## Consequences
+ Search/cataloguing stays cheap — one request per page of results.
+ The expensive per-page detail call happens only for pages actually downloaded.
+ Keeps the legacy path unchanged (its search results already carry the URLs).
- The `pages` schema needs a `fulltext_url` column, and `PageInfo` a matching field.
- `DownloadProcessor` gains a dependency on a `ResponseProcessor` (to call
  `enrich_from_detail`), which it does not currently hold.
- A NULL `pdf_url` becomes load-bearing state (means "not yet enriched"), so the
  download path must distinguish "not enriched" from "genuinely no PDF".

## Alternatives considered
- **Eager enrichment at catalogue time** — rejected: wasteful, since most
  catalogued pages are never downloaded, and it front-loads rate-limit pressure.
- **Construct image URLs from the search-result `id`** — rejected:
  `TestLocGovImageUrlsLive` shows the constructed URL does not reliably match the
  authoritative `resource.pdf` (different host/path), so construction is unsafe.