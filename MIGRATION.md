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

Of the three endpoint pairs above, only **Page search** is currently modeled
in api_params.py — LegacyQueryBuilder/LocGovQueryBuilder's base_url property
and build() method cover its "where" and "what" for both API versions.

**Newspaper list** and **Batch list** are not yet represented on either
builder class. LocApiClient.get_newspapers/get_batches still construct their
own (legacy-only) endpoint and params inline, with no new-API equivalent
wired in. This is the next gap to close before LocApiClient's three list/search
methods can all delegate to a builder uniformly, the way search_pages
partially does already via paginate_search.

### Parameter Mapping

**Direct substitutions (1:1)**

| Legacy | New | Notes |
|---|---|---|
| `andtext=` | `qs=` | keyword search |
| `rows=` | `c=` | results per page |
| `page=` | `sp=` | pagination |
| `format=json` | `fo=json` | response format |

**Changed parameters**

| Legacy | New | Notes |
|---|---|---|
| `date1=YYYY` | `start_date=YYYY-MM-DD` | now requires full ISO date |
| `date2=YYYY` | `end_date=YYYY-MM-DD` | now requires full ISO date |
| `state=` | `location_state=` | renamed |
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

**Batch list response** (`collections/chronicling-america/datasets/batch-summary/`)

Not actually a bare static JSON file — `fo=json` is **required**. Without it,
the request returns HTTP 403 with a Cloudflare bot-challenge page
("Just a moment..."), not JSON. Confirmed live, June 28 2026:

```python
requests.get(url, headers={"User-Agent": "..."})              # -> 403, Cloudflare challenge
requests.get(url, headers={"User-Agent": "..."}, params={"fo": "json"})  # -> 200, real JSON

The batch list itself is nested under the datasets key. 2959 entries as 
of June 28, 2026.

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

### Facets

**Filtering** — legacy `facet_` parameters are replaced by explicit parameters (`location_state=`, `start_date=`, `end_date=`) or the `fa=` filter attribute prefix. Filtering capability is preserved and expanded.

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
- Library of Congress link to OpenSearch XML document, December 20 2023 - Courtesy ofthe Wayback Machine: [link](https://web.archive.org/web/20231220131158/https://chroniclingamerica.loc.gov/search/pages/opensearch.xml)
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
- [ ] Separate API-specific concerns from application workflows
- [ ] Remove assumptions tied only to the retired API

## Phase 3: Validation

- [ ] Add migration regression tests
- [ ] Validate discovery workflows
- [ ] Validate ingestion workflows

## Phase 4: Cleanup

- [ ] Remove legacy-specific CLI commands
- [ ] Remove obsolete code paths
- [ ] Update user documentation

---

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