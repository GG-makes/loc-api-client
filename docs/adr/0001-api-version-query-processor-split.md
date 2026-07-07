# ADR 0001: Isolate each API version behind QueryBuilder and ResponseProcessor

Status: Accepted
Date: 2026-07-06

## Context
In August 2025 the Library of Congress retired the Chronicling America legacy
API and moved search to the loc.gov platform. The two APIs differ in both
directions: request parameters (e.g. legacy `andtext`/`dateFilterType` vs
loc.gov `qs`/`dates`/`fa` filters) and response shapes (legacy `items`/`newspapers`
keys vs loc.gov nested `results`/`pages[2].children[0].results`). The project must
run against either API during the transition, and early code was accumulating
`if api_version == ...` conditionals across the client, discovery, and CLI layers.

## Decision
Isolate each API version behind two abstract class hierarchies:
- **QueryBuilder** (`api_params.py`) — owns request parameter construction and
  pagination shape (`build`, `build_batch_list`, `build_newspaper_list`,
  `fetch_all_*`).
- **ResponseProcessor** (`processor_new.py`) — owns parsing
  (`parse_pages`, `parse_newspapers`, `parse_issue`, `parse_page_details`).

`Config` selects the concrete pair from `API_VERSION` (`query_builder_class`,
`processor_class`). Application code depends only on the abstract interfaces.
Cross-cutting behaviours (deduplication, newspaper filtering/summary utilities)
are mixins composed into the production processors — `LegacyProcessor` /
`LocGovProcessor` = `DeduplicationMixin + NewspaperUtilsMixin + <bare processor>`.

## Consequences
+ Adding or retiring an API version touches two classes, not every call site.
+ Call sites are API-agnostic; switching versions is a config change.
+ Parsing is unit-testable per API without network access.
- The **bare** `LegacyResponseProcessor` / `LocGovResponseProcessor` do NOT carry
  the mixin methods (`deduplicate=`, `filter_newspapers_by_criteria`,
  `get_newspaper_summary`). Only the **composed** `LegacyProcessor` /
  `LocGovProcessor` are safe in production. Handing out a bare class is a latent
  bug that `Mock`-based tests hide — this cost a debugging cycle. `Config` must
  always point at the composed classes.
- Per-API pagination quirks must be encoded on the builder (`fetch_all_*`), not
  in shared client code. See ADR 0002.

## Alternatives considered
- **Inline `if api_version` branching** — rejected: spreads version knowledge
  across every layer; the exact problem this replaces.
- **One processor with runtime response-shape detection** — rejected: the two
  response shapes are divergent enough that detection is as complex as two
  explicit parsers, with worse failure modes.