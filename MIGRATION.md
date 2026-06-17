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

### Base URL and Endpoint
 
| | Legacy | New |
|---|---|---|
| Base URL | `chroniclingamerica.loc.gov/search/pages/results/` | `www.loc.gov/collections/chronicling-america/` |
 
### Parameter Mapping
 
The table below documents confirmed mappings between legacy and new API parameters.
 
**Direct substitutions (1:1)**
 
| Legacy parameter | New parameter | Notes |
|---|---|---|
| `andtext=` | `qs=` | keyword search |
| `rows=` | `c=` | results per page |
| `page=` | `sp=` | pagination |
| `format=json` | `fo=json` | response format |
 
**Changed parameters (same purpose, different form)**
 
| Legacy parameter | New parameter | Notes |
|---|---|---|
| `date1=YYYY` | `start_date=YYYY-MM-DD` | now requires full ISO date, not year only |
| `date2=YYYY` | `end_date=YYYY-MM-DD` | now requires full ISO date, not year only |
| `state=` | `location_state=` | renamed |
| `lccn=` | `fa=number_lccn:` | moved to filter attribute pattern |
 
**New parameters with no legacy equivalent**
 
| New parameter | Purpose |
|---|---|
| `ops=` | search type: `PHRASE`, `AND`, `OR`, `~5`, `~10` |
| `dl=` | display level: `all`, `issue`, `page` |
| `front_pages_only=true` | filter to front pages |
| `location_city=` | city-level location filter |
| `location_county=` | county-level location filter |
| `partof_title=` | filter by newspaper title name |
| `fa=batch:` | filter by ingest batch name |
| `subject_ethnicity=` | filter by ethnicity subject heading |
| `searchType=Advanced` | enables advanced search mode |
 
### Facets
 
The legacy API used a `facet_` parameter prefix that served two roles: filtering results and returning aggregate counts per facet (e.g. result counts broken down by state or year range) in the JSON response.
 
The new API handles these differently:
 
- **Filtering** — facet filters are replaced by explicit parameters (`location_state=`, `start_date=`, `end_date=`) or the `fa=` filter attribute prefix (e.g. `fa=language:english`). Filtering capability is broadly preserved and in some cases expanded.
- **Facet counts** — the new API does not return aggregate facet counts in the JSON response. Any logic that consumed these counts will need to be removed or redesigned.
The `fa=` prefix is a general-purpose filter attribute pattern used throughout the new API. Examples: `fa=language:english`, `fa=number_lccn:sn83045462`, `fa=batch:tu_brownie_ver01`.
 
### Features not carried forward
 
| Legacy feature | Status |
|---|---|
| OpenSearch AutoSuggest (`/suggest/titles/?q=`) | no equivalent in new API |
| Linked Data / RDF views (`.rdf` URLs) | not part of `loc.gov` API |
| JSONP support (`callback=` parameter) | CORS only in new API |
| Separate title search endpoint (`/search/titles/results/`) | title browsing via collection URL and LCCN filter instead |
| `facet_subject=` subject heading filter | no direct equivalent |
 
### Open API questions
 
- Are there rate limits or request constraints on the `loc.gov` API not present in the legacy API?
- Does the `fa=` filter attribute pattern support values not yet identified in documentation?
- Which legacy `facet_` behaviours were actively used by existing workflows?

### Parameter Mapping

- Library of Congress' Chronicling America API Guidance, December 18 2023 - Courtesy of the Wayback Machine: [link](https://web.archive.org/web/20231218003023/https://chroniclingamerica.loc.gov/about/api/)
- Library of Congress' Chronicling America API Guidance, Today (June 17 2026): [link](https://libraryofcongress.github.io/data-exploration/loc.gov%20JSON%20API/Chronicling_America/README.html)
---

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
- [ ] Identify response format differences

## Phase 2: Refactoring

- [ ] Introduce centralised query construction logic
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

---

# Open Questions

Items requiring further investigation:

- Which existing discovery behaviours can be preserved directly?
- Which legacy query patterns require redesign?
- Are there new capabilities available through the `loc.gov` API that should be adopted?
- Which legacy CLI commands no longer represent useful workflows?