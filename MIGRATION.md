# Migration Notes: Chronicling America API Migration

## Overview

This document records the migration of this project from the retired Chronicling America legacy API to the current Library of Congress `loc.gov` API.

The goal of this migration is to restore discovery and ingestion workflows while improving maintainability, test coverage, and resilience against future external API changes.

## Approach

This migration keeps both API versions live and swaps between them by config,
rather than cutting over in place. The shape of it:

- **Per-API "twin" classes, selected by config.** Each version's divergence
  lives behind a paired hierarchy — `QueryBuilder` (api_params.py),
  `ResponseProcessor` (processor_new.py), `FacetQueryStrategy`
  (facet_strategy.py) — chosen from `API_VERSION` via `config`. Application code
  depends on the interfaces, not the version.
- **Orchestration stays single and API-agnostic.** Discovery/CLI code delegates
  to the config-selected twin; it does not branch on API version. Where a
  divergence is storage-coupled (legacy state LCCN-sampling), it gets its own
  small twin (`FacetQueryStrategy`) rather than an `if version` in the loop.
- **Legacy is kept runnable and tested as a reference,** not deleted (ADR 0005).
  A green suite is only meaningful because the known-good legacy path still runs.
- **Decisions recorded as ADRs** (docs/adr/) — the sp/cursor pagination model,
  lazy enrichment, why legacy stays, what was removed. These carried as much of
  the migration as the class structure did, across many sessions.
- **Live probing for ground truth.** Unknown API behaviour (pagination, filters,
  endpoint shapes) was confirmed with throwaway live requests before being coded,
  not assumed.

### Lessons learned

- **Match semantics before structure.** The twin layout pulls toward symmetry,
  and the worst bugs were where two things *looked* parallel but weren't —
  page-number resume vs cursor resume, bare vs composed processors, a "next"
  flag that didn't advance loc.gov. Confirm the two sides are actually parallel
  before copying a shape/type/default across.
- **Clean interfaces make mocks lie.** The two costliest bugs (a batch path
  calling a removed method; `paginate_search` silently walking one page) were
  correct-looking units with broken *composition* — hidden because tests mocked
  at the seam. Twins organise the code; only integration/live exercise catches
  composition errors.
- **Centralise test fixtures — and anchor them to reality.** Most shape bugs were
  hand-rolled mocks re-deriving a response shape inline. Canonical loc.gov
  response fixtures in `conftest.py` (plus one live-marked test asserting they
  still match the API) would have killed a whole bug class and made false twins
  visible side by side. This was the missing cheap win.
- **Probes + twins + ADRs are one system.** Twins give structure, probes give
  ground truth, ADRs give continuity. None sufficed alone; the places that got
  burned were the places one of the three was skipped.
- **Contributor posture: preserve, defer, document — don't decide for the
  author.** Where a call belonged to the original author (when to retire legacy,
  unfinished features like the threaded request queue), the migration flags and
  defers it (ADR 0004) rather than making it. The goal was changes that stay
  compatible with work-in-progress and assume as little as possible.

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

- [ ] Remove assumptions tied only to the retired API
    - [x] **Newspaper list.** Replaced the legacy `newspapers.json` fetch with a
          `build_newspaper_list` builder method feeding NewspaperInfo objects.
          Why: the loc.gov titles endpoint returns state/city as structured
          fields — a fidelity gain over legacy free-text place-of-publication
          strings, which could only be split into city/state heuristically.
    - [x] **Batch discovery.** Migrated the bulk issue path to parse loc.gov's
          issue-detail shape (`parse_issue`). Why: the batch-list and issue
          structures changed under loc.gov, so the legacy issue parser no longer
          matched the response.
    - [x] **Per-title issue discovery.** Removed rather than migrated. Why:
          loc.gov has no dedicated per-title issue endpoint; the same data comes
          from the page-search endpoint with `dl=issue`, so the standalone
          workflow was redundant. (ADR 0006)
    - [x] **Facet discovery query.** Moved `discover_facet_content` off the retired
          `search_pages` shim onto a config-selected FacetQueryStrategy twin.
          Why: query construction differs per API version — legacy samples LCCNs
          to approximate a state filter, loc.gov filters on `location_state`
          natively — so the divergence lives behind paired strategy classes rather
          than an `if version` branch in the discovery loop. (ADR 0001, 0005)
    - [x] **Pagination.** Replaced page-number pagination with cursor pagination.
          Why: the loc.gov API supports cursor pagination natively (follow
          `pagination.next`) but exposes no result-page number to resume from —
          its `sp` parameter is the *physical newspaper page*, not a response-page
          index. (ADR 0002)
- [x] Separate API-specific query concerns from application workflows
- [ ] **Add item-detail enrichment** — a capability the legacy module never had.
      Why: legacy search results already carried PDF / JP2 / OCR URLs directly, so
      no extra request was ever needed. loc.gov search results deliberately omit
      them — search is for discovery, not asset delivery — so download-ready URLs
      now require a separate per-page item-detail fetch. This is *new capability
      the API requires*, not a migrated legacy assumption, which is why it sits in
      refactoring rather than in cleanup. (ADR 0003)
    - [ ] `fulltext_url` column (pages table) + PageInfo field — somewhere to store
          the resolved OCR/asset URLs.
    - [ ] `enrich_from_detail` on ResponseProcessor — the twin hook that reads
          `resource.pdf` / `resource.image` / `resource.fulltext_file` from the
          item-detail response (legacy is a no-op; its search results already
          carry the URLs).
    - [ ] wire DownloadProcessor._download_page to call enrichment lazily when a
          page's asset URL is NULL, then download.

## Phase 3: Validation

- [ ] **Regression tests** — catch incompatible API changes and pin the migrated
      query/parse behaviour so future API drift is detectable, not silent.
    - [x] **Builder unit tests** (build_newspaper_list / fetch_all_newspapers) —
          pin the new-API request shape (endpoint + params) so a regression in
          query construction fails a test rather than a live run.
    - [x] **Newspaper parse + storage round-trip** — the loc.gov titles response
          parses into NewspaperInfo with structured state/city, and survives a
          storage write/read. Why: the state/city fidelity gain is only real if
          it persists correctly, not just parses.
    - [x] **Batch discovery integration test** — drives the bulk path through to
          `parse_issue` on a real loc.gov issue shape. Why: this path broke
          silently before (mocked seams hid a call to a removed method); an
          integration-shaped test catches composition errors that unit mocks miss.
    - [x] **FacetQueryStrategy unit tests** (tests/test_facet_strategy.py) — cover
          both twins: legacy LCCN-sampling (incl. the no-periodicals None case)
          and loc.gov native state/date/combined filtering. Why: the twins are the
          migration's core seam, so both sides need direct coverage. (ADR 0001)
    - [ ] **Rewrite test_get_page_metadata** — currently a skipped placeholder.
          What's needed: a real test asserting the final page-metadata shape.
          Why deferred: page metadata now depends on item-detail enrichment (PDF/
          JP2/OCR URLs), which isn't built yet, so there's no final shape to assert
          until ingestion (below) lands. (ADR 0003)

- [ ] **Validate discovery workflows** — prove discovery walks loc.gov against the
      live API, not just against mocks.
    - [x] **LIVE: cursor pagination + resume** — verified against loc.gov
          (2026-07-07): paginate_search followed `pagination.next` across distinct
          pages, `start_url=` resumed onto page 2, and the client's retry absorbed
          a live IncompleteRead. Why it matters: mocks can't prove the cursor
          actually advances — an earlier bug walked page 1 forever and passed every
          unit test. (ADR 0002)
    - [x] **Residual: full facet-level resume** — the resume wiring (context reads resume_cursor
          → passes it as paginate_search `start_url` → checkpoints each page's
          `pagination.next`) is covered by two integration tests that drive the
          real discover_facet_content against a fake client.

- [ ] **Validate ingestion workflows** — prove a download actually lands a file on
      disk. This is the migration's real finish line.
    - [ ] **BLOCKED on item-detail enrichment (not yet built).** Why blocked:
          loc.gov search results carry no PDF/JP2/OCR URLs — unlike the legacy
          search response, which included them — so there is nothing to download
          until a per-page item-detail fetch supplies them. Building that unblocks
          this whole group. (ADR 0003)
        - [ ] `fulltext_url` column (pages table) + PageInfo field — somewhere to
              store the resolved OCR/asset URLs.
        - [ ] `enrich_from_detail` on ResponseProcessor — the twin hook that reads
              `resource.pdf` / `resource.image` / `resource.fulltext_file` from the
              item-detail response (legacy is a no-op; its search results already
              carry the URLs).
        - [ ] wire DownloadProcessor._download_page to call enrichment lazily when
              a page's asset URL is NULL, then download.

## Phase 4: Cleanup

- [x] Remove now-dead code (unblocked by the facet work)
    - [x] **Removed `search_pages`** (rate_limited_client.py). Why: the legacy
          shim posting to the retired `search/pages/results/` endpoint;
          `discover_facet_content` was its last caller and now walks the cursor.
    - [x] **Removed `build_search_params`**; collapsed FacetSearchParamsBuilder to a
          standalone `adjust_batch_size_for_facet` function** (facet_processor.py).
          Why: the FacetQueryStrategy twins replaced the param builder, leaving one
          orphaned method (batch-size tuning) that didn't warrant a class. (ADR 0001)
    - [x] **`_extract_city` already gone** — removed earlier with the state/city
          enrichment; this was a stale checklist line.
- [ ] Remove legacy-specific CLI commands
- [ ] Remove obsolete code paths
    - [ ] get_newspapers: filter state via the `state` column, not place LIKE
    - [ ] _migrate_database: split per-table try blocks so one ALTER failure
          can't abort later migrations
- [ ] Update user documentation
- [ ] Resolve deferred, non-blocking TODOs: download_newspaper --estimate-only,
      search_text (get_count vs paginate), tui_monitor (timeout NameError;
      cross-process rate-limit singleton), merge_databases phantom columns

Decisions recorded as ADRs (docs/adr/): 0001 builder/processor split · 0002
loc.gov sp/cursor pagination · 0003 lazy item-detail enrichment (Proposed) ·
0004 defer pre-existing defects · 0005 keep legacy alive for its test suite ·
0006 remove per-title issue discovery.

## Deferred Decisions - Post Migration TO-DOs:
- [ ] Retire page-number tracking (resume_from_page, current_page) from facet
      discovery once cursor resume is proven in the field. Blocked on reworking
      validate_and_fix_facet_status (facet_processor.py), which uses them to
      detect incorrectly-completed facets — don't remove the columns until that
      detection is ported to the cursor model (ADR0004, ADR0007).
- [ ] Retire legacy api knowledge. Remove the legacy option from the config, delete the            legacy-specific classes from api_params.py, processor_new.py, and facet_strategy.py.

# Design Principles

The migration follows these principles:

1. Avoid unnecessary rewrites.
2. Preserve existing behaviour unless change is required.
3. Keep external API dependencies isolated.
4. Prefer incremental, reviewable changes.
5. Add tests alongside migration work.
6. Document assumptions and decisions.

# Known Limitations

### Legacy search pagination is single-page (accepted, ADR 0002/0005)

paginate_search follows the loc.gov pagination.next cursor. Legacy responses
carry no such cursor — legacy paginates via page/totalPages — so a legacy query
yields only its first page. This is accepted, not a defect: the legacy endpoint
is retired and cannot run, and legacy is kept solely as a tested reference. The
original working legacy pagination lived in
api_client.LocApiClient.search_with_faceted_dates, removed with that dead module
and recoverable from git history if ever needed.

Reintegration seam, if legacy runtime pagination is ever required: add a builder
method fetch_all_search_pages(fetch) — LegacyQueryBuilder loops page/totalPages,
LocGovQueryBuilder follows the cursor — and have paginate_search delegate to it,
mirroring get_all_batches → fetch_all_batches. Caveat: this collides with the
facet-discovery resume model (legacy resume is page-number, loc.gov resume is a
cursor URL), so restoring legacy would mean two resume paths for a feature only
one of which can execute.

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