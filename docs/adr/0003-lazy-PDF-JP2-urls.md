# ADR 0003: Fetch loc.gov PDF/JP2/OCR URLs lazily at download time

Status: Accepted (proposed 2026-07-06)
Date: 2026-07-09

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

Two further behaviours, confirmed by live probe (2026-07-09), shape the design:
- **`resource.fulltext_file` returns JSON, not plain text.** It is the
  word-coordinates service (`format=alto_xml&full_text=1`); the OCR text is
  nested at `full_text` under a dynamic segment key. "Fetch the OCR" is therefore
  a fetch-then-parse step, and the parse is API-specific.
- **The full JP2 is not in the response.** `resource.image` is a ~6% IIIF preview
  JPEG; the full ~4 MB JP2 is obtained by swapping the confirmed `resource.pdf`
  extension (`.pdf` → `.jp2`). This is a safe construction *from an
  already-fetched authoritative URL* — distinct from, and not contradicting, the
  rejected idea of constructing URLs from the search `id` (see Alternatives).

Because a page moves through three states — catalogued (URL unknown), enriched
(URL spotted), downloaded (URL called) — and re-discovery can revisit a page, the
design must record which state a page is in, not merely its URLs.

## Decision
Store `pdf_url` / `jp2_url` / `ocr_url` as NULL when a loc.gov page is catalogued
from search. Model the three states explicitly on `pages`: `enriched` (item-detail
fetch done, URLs "spotted") and `ocr_fetched` (OCR text retrieved and written,
"called"). Defer the item-detail fetch to download time: in
`DownloadProcessor._download_page`, when the URLs are NULL, fetch item detail,
enrich the record (URLs + `enriched=1`), then download.

Route the API-specific work through two `ResponseProcessor` twins:
- `enrich_page` — legacy: no-op (search already carried the URLs); loc.gov:
  fetches item detail, takes `resource.pdf`, constructs the JP2 by swapping
  `.pdf`→`.jp2`, and takes `resource.fulltext_file` as `ocr_url`.
- `parse_fulltext_response` — extracts OCR text from the fulltext body (loc.gov:
  nested `full_text`; legacy: the body is already text).

Keep `ocr_url` (URL, "spotted") distinct from `ocr_text` (fetched text, "called").
For loc.gov the fetched text is written to disk, not stored in the DB, so
`ocr_fetched` — not text presence — is the "called" signal.

## Consequences
+ Search/cataloguing stays cheap — one request per page of results.
+ The expensive per-page detail call happens only for pages actually downloaded.
+ Keeps the legacy path unchanged (its search results already carry the URLs).
+ "Spotted vs called" is explicit (`enriched`, `ocr_fetched`), so re-runs skip
  work already done rather than re-paying API cost. This also replaces the fragile
  "NULL `pdf_url` means not-enriched" heuristic flagged when this ADR was proposed.
- The `pages` schema needs `ocr_url` + `enriched` + `ocr_fetched` columns, and
  `PageInfo` an `ocr_url` field.
- `DownloadProcessor` gains a dependency on a `ResponseProcessor` (to call
  `enrich_page` / `parse_fulltext_response`), which it does not currently hold.
- Page re-store must be non-destructive (`INSERT OR IGNORE`, not
  `INSERT OR REPLACE`), or re-discovery wipes the spotted URLs.
- The JP2 mapping is a construction, valid for the NDNP storage layout confirmed
  live; if LOC changes that layout the swap could break (low risk — it mirrors how
  legacy already constructed `{base}.jp2`).

## Alternatives considered
- **Eager enrichment at catalogue time** — rejected: wasteful, since most
  catalogued pages are never downloaded, and it front-loads rate-limit pressure.
- **Bulk batch archives (`.tar.bz2` ALTO XML) instead of per-page API** — rejected
  for the target use case: archives are efficient only when you want a large
  fraction of a corpus, because you download whole batches and filter locally. The
  intended queries are *selective* (keyword full-text, e.g. recipe terms) and
  loc.gov indexes the full OCR server-side, so per-page enrichment fetches only
  matching pages. Batch archives remain the better tool at bulk-fraction scale, or
  if per-page request volume grows large enough to hit rate-limit/CAPTCHA walls —
  the escalation path, not the default.
- **Construct image URLs from the search-result `id`** — rejected:
  `TestLocGovImageUrlsLive` shows a URL built from the search `id` does not
  reliably match the authoritative `resource.pdf` (different host/path). This
  differs from swapping the extension on the *already-fetched* `resource.pdf`
  (Context/Decision), which is safe because it starts from the authoritative URL.