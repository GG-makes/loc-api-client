# Migration Notes: Chronicling America API Migration

## Overview

This document records the migration of this project from the retired Chronicling America legacy API to the current Library of Congress `loc.gov` API.

The goal of this migration is to restore discovery and ingestion workflows while improving maintainability, test coverage, and resilience against future external API changes.

## Background

In August 2025, the Library of Congress migrated Chronicling America search functionality to the broader `loc.gov` platform.

The previous API endpoints used by this project were retired. Existing workflows relying on the legacy Chronicling America search API no longer function correctly.

The previous implementation relied on endpoints such as ```https://www.loc.gov/chroniclingamerica/search/pages/results/```
with parameters including:
```
format=json
page
rows
date1
date2
dateFilterType
```


These endpoints now return HTTP 404 responses.

The Chronicling America collection remains available, but access has moved to a different API model with different query semantics and response structures.

---

# Migration Goals

The migration aims to:

- restore content discovery functionality
- replace retired API dependencies
- preserve existing ingestion behaviour where possible
- isolate API-specific logic from application workflows
- improve test coverage around external integrations
- make future API changes easier to identify and manage

---

# Current Findings

## Legacy API Dependency

The existing implementation assumes the behaviour of the retired Chronicling America search endpoint.

The current discovery workflow includes assumptions around:

- endpoint structure
- query parameters
- date-range filtering
- response parsing
- pagination behaviour

These assumptions need to be reviewed against the current `loc.gov` API.

## API Behaviour Investigation

Investigation complete and current as of June 18, 2026. Historical responses and parameters confirmed via legacy code and the Chronicling America website accessed via the Wayback Machine. All current parameters and response structures confirmed via live API calls and LOC Jupyter notebooks, as well as official current Chronicling America guidance.

### Base URL and Endpoint

| | Legacy | New |
|---|---|---|
| Page search | `chroniclingamerica.loc.gov/search/pages/results/` | `www.loc.gov/collections/chronicling-america/` |
| Newspaper list | `chroniclingamerica.loc.gov/newspapers.json` | `www.loc.gov/collections/chronicling-america/titles/` |
| Batch list | `chroniclingamerica.loc.gov/batches.json` | `www.loc.gov/collections/chronicling-america/datasets/batch-summary/` |

### Implementation Status (Base URL and Endpoint)

Of the three endpoint pairs, **Page search** and **Batch list** are modeled on both builders and delegated to by LocApiClient (search/paginate_search and get_all_batches). **Newspaper list** is only half-modeled: newspaper_list_url exists on both builders, but there's no build_newspaper_list() and LocApiClient.get_newspapers still constructs its legacy newspapers.json request inline. Closing that gap — adding build_newspaper_list() to both builders and routing get_newspapers through it — is the remaining work before all three list/search methods delegate uniformly.

### Parameter Mapping

**Direct substitutions (1:1)**

| Legacy | New | Notes |
|---|---|---|
| `andtext=` | `qs=` | keyword search |
| `rows=` | `c=` | results per page |
| `page=` | `response['pagination']['next']` | pagination |
| `format=json` | `fo=json` | response format |

**Changed parameters**

| Legacy | New | Notes |
|---|---|---|
| `date1=YYYY` | `dates=` (start of range) | combined into single `dates=YYYY-MM-DD/YYYY-MM-DD` param |
| `date2=YYYY` | `dates=` (end of range)   | combined into single `dates=YYYY-MM-DD/YYYY-MM-DD` param |
| `state=` | `fa=location_state:` | moved to filter attribute pattern |
| `lccn=` | `fa=number_lccn:` | moved to filter attribute pattern |

**New parameters with no legacy equivalent**

| Parameter | Purpose |
|---|---|
| `ops=` | search type: `PHRASE`, `AND`, `OR`, `~5`, `~10` |
| `dl=` | display level: `all`, `title`, `issue`, `page` |
| `front_pages_only=true` | filter to front pages |
| `location_city=` | city-level filter |
| `location_county=` | county-level filter |
| `partof_title=` | filter by newspaper title name |
| `fa=batch:` | filter by batch name |
| `subject_ethnicity=` | filter by ethnicity subject heading |
| `sp` | sequence page filter for the physical newspaper page |

### Estimate / Count Mechanism

| | Legacy | New (loc.gov) |
|---|---|---|
| Request | `rows=1&page=1` | `c=1&at=search,results,pagination` |
| Count field | `response['totalItems']` | `response['pagination']['total']` |
| Accuracy | Best-effort estimate | Exact filtered result count |
| Granularity exercised | Bare year ranges only (the only shape `estimate_download_size` was ever called with) | Any combination `LocGovQueryBuilder` supports (date/state/lccn/batch) |
| lccn filtering | Accepted as a parameter by `estimate_download_size` but never actually added to the request — a pre-existing no-op, not introduced by this migration. Should only have affected the `download_newspaper --estimate-only` cli command - no other. | Fully supported via `fa=number_lccn:` |

Source: `at=`/`pagination.total` mechanism confirmed via
[investigate_new_response_format.py](investigate_new_response_format.py).

### PageInfo Field Changes

The new API does not include image or full OCR URLs in search results. These were available directly in the legacy API's search response; the new API requires a separate item detail request to retrieve them. This reflects standard metadata API design — search results are for discovery, not bulk delivery of download-ready assets.

| Field | Legacy (search result) | New API (search result) | Resolution |
|---|---|---|---|
| `item_id` | `id` (string) | `id` (string) | unchanged |
| `lccn` | `lccn` (string) | `number_lccn[0]` (list) | extract first element |
| `title` | `title` (string) | `partof_title[0]` (list) | extract first element |
| `date` | `YYYYMMDD` → conversion required | already `YYYY-MM-DD` | conversion removed |
| `edition` | `edition` (int) | `number_edition[0]` (list) | extract, parse int |
| `sequence` | `sequence` (int) | not a reliable field | parsed from `?sp=N` in `id` URL |
| `page_url` | `id` (string) | `id` (string) | unchanged |
| `pdf_url` | `pdf` (direct, search result) | not in search results | item detail required |
| `jp2_url` | `image` (direct, search result) | not in search results | item detail required |
| `ocr_text` | full OCR text (`ocr_eng`) | ~1000 char snippet (`description`) | full text requires separate fetch (see below) |
| `word_count` | `word_count` (int) | not available | stored as `None` |

### Full OCR Text

The `description` field in search results is a truncated OCR snippet of approximately 1000 characters. It is sufficient for discovery — confirming that a page has content — but not for full-text indexing or research use.

Full OCR text requires two additional requests per page beyond discovery:

1. **Item detail** — fetch `{page_url}?fo=json` → read `resource.fulltext_file`, which is a URL pointing to the page's plain-text OCR file
2. **OCR fetch** — fetch that URL → full OCR text as a plain text response

This means the per-page download workflow now makes three API calls where the legacy workflow made one (search result included everything). The rate limiter must account for this when bulk downloading with OCR.

For bulk-scale OCR ingestion, the batch archive files (`.tar.bz2` linked from the batch list endpoint) contain ALTO XML OCR files for every page and are substantially more efficient than per-page API calls. This was true under the legacy API as well and remains the recommended approach for large-scale text corpus work.

### Response Structure

**Page search response**

Results are nested — not at the top level:

```
response['pages'][1]['children'][0]['results']
```

Key fields per result (all confirmed):

| Field | Type | Notes |
|---|---|---|
| `id` | string | `http://www.loc.gov/resource/sn.../YYYY-MM-DD/ed-1/?sp=N` |
| `date` | string | already `YYYY-MM-DD` — no conversion needed |
| `number_lccn` | list | e.g. `["sn85042345"]` |
| `number_edition` | list | e.g. `["1"]` |
| `partof_title` | list | newspaper title string |
| `location_state` | list | lowercase, e.g. `["oklahoma"]` |
| `location_city` | list | lowercase |
| `language` | list | lowercase |
| `batch` | list | batch name without `batch_` prefix |
| `resources` | list | `[{"url": "...", "files": N}]` — no PDF/JP2/OCR here |
| `description` | list | OCR text snippet (~1000 chars, not full page) |
| `mime_type` | list | available formats |
| `number_page` | list | zero-padded string — not reliable for sequence number |

**Pagination** (confirmed):

```
response['pagination']['total']     # filtered result count
response['pagination']['current']   # current page number
response['pagination']['next']      # next page URL, or null
response['pagination']['perpage']   # results per page
```

**Item detail response** (`resource/sn.../YYYY-MM-DD/ed-1/?sp=N&fo=json`)

```
response['item']['date']              # YYYY-MM-DD
response['item']['newspaper_title']   # list
response['item']['number_lccn']       # list
response['item']['location_state']    # list
response['item']['location_city']     # list
response['item']['batch']             # list
response['resource']['pdf']           # tile.loc.gov PDF URL
response['resource']['image']         # tile.loc.gov JP2/IIIF URL
response['resource']['fulltext_file'] # OCR text service URL (full text)
response['pagination']['current']     # page sequence number
```

**Issue detail response** (`resource/sn.../YYYY-MM-DD/ed-1/?fo=json`)

```
response['item']                      # same fields as item detail
response['resources'][0]['files']     # list of lists — one inner list per page
```

Each inner list contains file dicts keyed by `mimetype`:

| Mimetype | Field | Content |
|---|---|---|
| `image/jp2` | `url` | JP2 image file |
| `application/pdf` | `url` | PDF file |
| `text/xml` | `url` | ALTO XML OCR file |
| `image/jpeg` | `url` | thumbnail (appears twice at different sizes) |
| `application/json` | `title` | "Image N of [newspaper title]..." |
| `text/plain` | `fulltext_service` | OCR text service URL |

**Newspaper list response** (`collections/chronicling-america/titles/?fo=json`)

Results nested at:
```
response['pages'][2]['children'][0]['results']
```

Key fields — note mixed dict/list structure:

| Field | Type | Notes |
|---|---|---|
| `number_lccn` | plain list | e.g. `["sn85026945"]` |
| `title` | string | full title string |
| `location_state` | dict | `{"label": "South Carolina", "value": "south carolina"}` |
| `location_city` | plain list | lowercase strings |
| `language` | dict | `{"label": "English", "value": "english"}` |
| `partof_title` | dict | `{"label": "...", "value": "...", "url": "..."}` |
| `number_first_issue` | dict | `{"label": "1847-03-03", "url": "..."}` — start date |
| `number_last_issue` | dict | `{"label": "1869-09-29", "url": "..."}` — end date |
| `number_issue_count` | dict | `{"label": "254", "value": "254"}` |

NewspaperInfo carries state and city as dedicated fields: the loc.gov titles response supplies them structurally (location_state.label, location_city), a fidelity gain over the legacy free-text place_of_publication strings, which can only be split into city/state heuristically.

**Batch list response** (`collections/chronicling-america/datasets/batch-summary/`)

Not actually a bare static JSON file — `fo=json` is **required**. Without it,
the request returns HTTP 403 with a Cloudflare bot-challenge page
("Just a moment..."), not JSON. Confirmed live, June 28 2026:

```python
requests.get(url, headers={"User-Agent": "..."})              # -> 403, Cloudflare challenge
requests.get(url, headers={"User-Agent": "..."}, params={"fo": "json"})  # -> 200, real JSON
```
The batch list itself is nested under the datasets key. 2959 entries as 
of June 28, 2026.
```
Confirmed per-entry fields:
{
    "batch": "okhi_durant_ver01",
    "archive_name": "okhi_durant_ver01.tar.bz2",
    "archive_created": "...",
    "batch_file": "...",
    "identifier": "...",
    "ingested": "2014-11-21T20:47:33-05:00",
    "issue_count": 211,
    "page_count": 5241,
    "lccns": ["sn83030214"],
    "metadata_key": "...",
    "sha256": "...",
    "size": "...",
    "url": "https://chroniclingamerica.loc.gov/data/ocr/....tar.bz2",
    "verified": "..."
}
```

archive_created, batch_file, identifier, metadata_key, sha256,
size, verified were not previously documented. Exact value types/formats
not yet individually confirmed — recorded here as field names found, pending
closer inspection if needed.

Note: url points to legacy OCR bulk download archives. The batch name
(without batch_ prefix) is what the fa=batch: search filter expects.

No pagination support confirmed — c=/sp= params have no effect; the
full datasets list (all 2959 entries) is returned regardless. Treat as a
fetch-everything-at-once endpoint, not a paginated one.

at= does not scope this endpoint the way it does for page search —
at=results/at=data return an empty dict under that key. The at=
response-trimming trick (see Estimate/Count Mechanism above) appears
specific to the search endpoint, not this one.

### Newspaper Issues Listing

Legacy `get_newspaper_issues(lccn)` → `chroniclingamerica.loc.gov/lccn/{lccn}.json`,
returning `{'issues': [...]}`.

**loc.gov has no equivalent dedicated endpoint.** Confirmed live (2026-06-28):
issue listing is the *same* search/collection endpoint already used for page
search, with `dl=issue` instead of `dl=page`, filtered by `fa=number_lccn:`:

```python
requests.get(
    "https://www.loc.gov/collections/chronicling-america/",
    params={"fo": "json", "fa": f"number_lccn:{lccn}", "dl": "issue", "c": 10},
)
```

### Facets

**Filtering** — legacy `facet_` parameters are replaced by the `fa=` filter
attribute prefix (`fa=number_lccn:`, `fa=location_state:`, `fa=batch:`) or
explicit date parameters (`start_date=`, `end_date=`). Filtering capability
is preserved and expanded.

**Facet counts** — the new API does not return aggregate facet counts in the JSON response. Any logic consuming these counts must be removed or redesigned.

### Coverage Dates

| | Legacy (assumed) | New (confirmed) |
|---|---|---|
| Start | 1836-01-01 | 1736-08-03 |
| End | present | 1963-11-30 |

The new API covers nearly a century more history than the legacy assumption. Code that previously rejected pre-1836 dates will need updating.

### Features Not Carried Forward

| Legacy feature | Status |
|---|---|
| OpenSearch AutoSuggest (`/suggest/titles/?q=`) | no equivalent |
| Linked Data / RDF views | not part of `loc.gov` API |
| JSONP support (`callback=` parameter) | CORS only |
| `facet_subject=` subject heading filter | no direct equivalent |
| Facet aggregate counts in response | removed |

### Discovery Query Scope

The `discover_facet_content` wiring supports `date_range`, `state`, and `combined`
facet types, which map to date and state filtering via `ChroniclingAmericaSearchParams`.
This covers the full bulk systematic discovery workflow.

`LocGovQueryBuilder` exposes additional new-API capabilities (`ops=`, `qs=` text
search, `location_city=`, `location_county=`, `fa=batch:`) that are not exercised
by the current discovery workflow. These are available for interactive/CLI use via
`from_cli()` but are intentionally out of scope for bulk facet discovery.


### Open API Questions
1) What was the date format(s) accepted by the legacy api?
Context: The CLI accepts only 4 digit years and YYYY-MM-DD formatted dates except for searchText,
which allows anything. However, it looks like searchText might have been the primary search
function for the LoC. To further complicate things, it looks like dates were
formatted to MM/DD/YEAR before submission.

ANSWER: Checking the Wayback Machine's archived copy of the *Chronicling America API Guidance* leads us to *The OpenSearch Description Document*. When date1 and date2 were included,
they are marked as *chronam* dates. Going to the archived chronam repository shows us that
chronam accepted the dates `01/01/1900 or 01/1900 or 1900`, aka the formats MM/DD/YEAR, MM/YEAR,
or YEAR. These were then solrized into an integer suitable for querying a solr document.
Format selection on the wire was paired with a dateFilterType parameter (range or
yearRange), which told chronam which of the above formats to expect (MM/DD/YEAR or YEAR)
 — the date values and the filter type were never independent of each other.

2) What degree of granularity in date range searches was accepted?

Context: pre-migration exploratory code deals solely in 4 digit date ranges. Within the logic,
dates appear to have been changed to January 1st (start date) or December 31st (end date) when
submitted as part of a date range. Separately, the download_newspaper CLI command already
documented and validated day-level dates (YYYY-MM-DD) for a single newspaper's date range,
though it's unclear whether this was ever translated into chronam's actual MM/DD/YEAR wire format
before submission.

ANSWER: The *chronam* github supports more granular dates being accepted, but not being part
of the design of this module. The migration has chosen as a judgement call to accept day-level
dates as part of the pre- and post- date range construction logic, continuing the day-level
intent already present in download_newspapere.


### Parameter Mapping

- Library of Congress' Chronicling America API Guidance, December 18 2023 - Courtesy of the Wayback Machine: [link](https://web.archive.org/web/20231218003023/https://chroniclingamerica.loc.gov/about/api/)
- Library of Congress' Chronicling America API Guidance, June 17 2026: [link](https://libraryofcongress.github.io/data-exploration/loc.gov%20JSON%20API/Chronicling_America/README.html)
- Library of Congress Jupyter Notebooks: [link](github.com/nwy/Chronicling-America-API)
- Response testing: [link](investigate_new_response_format.py)
- Library of Congress link to OpenSearch XML document, December 20 2023 - Courtesy of the Wayback Machine: [link](https://web.archive.org/web/20231220131158/https://chroniclingamerica.loc.gov/search/pages/opensearch.xml)
- chronam: [link](https://github.com/LibraryOfCongress/chronam/blob/7436a24c2cdf1e38cf2107d420be2721d35b2d32/core/index.py#L726)
- Batch Response Testing: [link](investigate_new_batch_metadata.py)

# Implementation Approach

## Query Construction

### Current State

Query construction logic is coupled to the legacy API behaviour.

This makes API changes harder because discovery logic, query generation, and API-specific details are intertwined.

### Planned Change

Introduce a centralised query construction component responsible for:

- generating API requests
- isolating API-specific parameters
- providing a consistent interface to discovery workflows

Benefits:

- easier testing
- clearer separation of concerns
- simpler future API migrations

---

# Testing Strategy

The migration will expand automated validation around areas affected by the API change.

Testing goals:

- detect incompatible API behaviour changes
- validate query generation
- validate response parsing
- preserve existing ingestion behaviour

Planned coverage:

- query construction tests
- API response parsing tests
- discovery workflow tests
- regression tests for previously supported behaviour

---

# Windows Compatibility

This fork also includes Windows compatibility improvements.

Issues addressed include:

- platform-specific assumptions
- path handling differences
- environment-specific behaviour

The goal is to maintain compatibility across supported development environments while preserving existing Linux functionality.

---

# Planned Migration Steps

## Phase 1: Investigation

- [x] Identify legacy API dependencies
- [x] Confirm retired endpoints
- [x] Document replacement API behaviour
- [x] Identify response format differences

## Phase 2: Refactoring

- [x] Introduce centralised query construction logic
      (LegacyQueryBuilder / LocGovQueryBuilder)
- [x] Separate API-specific response handling concerns from application
      workflows (processor_new ResponseProcessor hierarchy)
- [ ] Remove assumptions tied only to the retired API
    - [x] Newspaper list: build_newspaper_list + fetch_all_newspapers on
          both builders; client get_all_newspapers yields NewspaperInfo
    - [x] Confirmed titles/?fo=json&c=150 live; baked c=150 into build_newspaper_list
    - [x] NewspaperInfo carries structured state/city, persisted to newspapers +
          periodicals tables
    - [x] Batch discovery bulk path FIXED: BatchDiscoveryProcessor.
          process_issue_from_batch now uses processor.parse_issue (was calling
          the removed process_page_from_issue + reading legacy issue['pages'] →
          silent zero on loc.gov). Added LegacyResponseProcessor.parse_issue to
          complete the interface.
    - [x] Per-title issue discovery REMOVED, not migrated (ADR 0006):
          get_newspaper_issues + discover_periodical_issues. loc.gov has no
          per-title issues endpoint; issue enumeration is batch-based. Its
          periodical_issues storage cluster is intentionally retained (inert,
          reusable if the feature is rebuilt on the batch path).
    - [ ] search_pages: retire the legacy shim (rate_limited_client.py:642).
          Live under discover_facet_content → process_captcha_recovery + CLI
          auto_discover_facets / setup_download_workflow / retry_failed_facets /
          test_discovery. Route through search(builder) / paginate_search(builder).
- [ ] Separate API-specific query concerns from application workflows
    - [x] discovery_manager._convert_newspaper_to_periodical and list_newspapers
          consume NewspaperInfo, not raw legacy dicts
    - [ ] discover_facet_content still calls the legacy search_pages shim
          (discovery_manager.py:454) — folds into the search_pages item above

## Phase 3: Validation

- [ ] Add migration regression tests
    - [x] Builder unit tests for build_newspaper_list / fetch_all_newspapers
    - [x] parse_newspapers state/city assertions; storage migration + round-trip
    - [x] Batch discovery test reaching parse_issue on a loc.gov issue shape
          (test_batch_discovery.py) — the coverage gap that let the broken
          call slip past a green suite
    - [ ] Rewrite test_get_page_metadata (currently a skipped Phase 3
          placeholder) once a page item-detail fetch exists
- [ ] Validate discovery workflows — blocked on search_pages migration
- [ ] Validate ingestion workflows
    - [ ] BLOCKED on item-detail enrichment (not yet built):
        - [ ] fulltext_url column + PageInfo field
        - [ ] enrich_from_detail on ResponseProcessor
        - [ ] wire DownloadProcessor._download_page lazy enrichment

## Phase 4: Cleanup

- [ ] Remove dead / duplicated code (graph-verified orphans)
    - [ ] DiscoveryManager._process_issue_from_batch + _handle_captcha_during_
          batch_discovery — dead twin of the live batch_discovery.py method,
          now fully redundant after the batch fix consolidated the logic
    - [ ] api_client.py — dead module (no live src import; only scratch scripts).
          Delete module + its tests; accept the scratch scripts break
    - [ ] discovery_manager._extract_city — unused by _convert after enrichment
          (or keep with TODO — see notes)
- [ ] Remove legacy-specific CLI commands
- [ ] Remove obsolete code paths
    - [ ] get_newspapers: filter state via the `state` column instead of
          place_of_publication LIKE (deferred until catalog repopulated live)
    - [ ] _migrate_database: split per-table try blocks so one table's ALTER
          failure can't abort later migrations
- [ ] Update user documentation
- [ ] Resolve deferred, non-blocking TODOs: download_newspaper --estimate-only,
      search_text (get_count vs paginate), tui_monitor (timeout NameError;
      cross-process rate-limit singleton), merge_databases phantom columns

Decisions recorded as ADRs (docs/adr/): 0001 builder/processor split · 0002
loc.gov sp/cursor pagination · 0003 lazy item-detail enrichment (Proposed) ·
0004 defer pre-existing defects · 0005 keep legacy alive for its test suite ·
0006 remove per-title issue discovery.

# Design Principles

The migration follows these principles:

1. Avoid unnecessary rewrites.
2. Preserve existing behaviour unless change is required.
3. Keep external API dependencies isolated.
4. Prefer incremental, reviewable changes.
5. Add tests alongside migration work.
6. Document assumptions and decisions.

---

# Upstream Collaboration

This work is being developed in a fork with the intention of contributing improvements upstream where appropriate.

Related upstream discussion:

- Pull Request: [Windows fixes](https://github.com/jakalope/loc-api-client/pull/2)
- Pull Request: [Update tests for production changes since initial release
](https://github.com/jakalope/loc-api-client/pull/3)
- Issue: [August 4 2025 Chronicling America API Updates](https://github.com/jakalope/loc-api-client/issues/4)
- Issue: [facet_type mismatch between discovery_manager.py ('hybrid') and facet_processor.py ('combined')](https://github.com/jakalope/loc-api-client/issues/5)

---

# Open Questions

Items requiring further investigation:

- Which existing discovery behaviours can be preserved directly?
- Which legacy query patterns require redesign?
    ANSWER: dateFilterType is no longer supported. 
- Are there new capabilities available through the `loc.gov` API that should be adopted?
- Which legacy CLI commands no longer represent useful workflows?