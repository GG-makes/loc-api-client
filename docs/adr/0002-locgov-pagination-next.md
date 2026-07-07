# ADR 0002: loc.gov paginates via the pagination.next cursor, not `sp`

Status: Accepted
Date: 2026-07-06

## Context
`LocGovQueryBuilder.build()` initially mapped the result-set page number to the
`sp` query parameter, and `paginate_search` advanced pages by incrementing
`params.page`. A live pagination test then failed: page 1 and page 2 shared item
IDs. Inspecting the returned item URLs showed `sp` is a **sequence-page selector**
— it picks the Nth physical page *within each matching newspaper issue*
(`.../ed-1/?sp=2`), not the Nth page of search results. Single-page issues fell
back to `sp=1`, producing the overlap. `sp` is an in-issue filter, never an
offset.

## Decision
Do not emit `sp` for result pagination. loc.gov endpoints return a
`pagination.next` cursor URL; walk results by following that URL until it is
absent (it carries `c=` page size forward automatically). The legacy API keeps
its own `page`/`totalPages` mechanism. Pagination is implemented per-builder
(`fetch_all_batches`, `fetch_all_newspapers`) and in `paginate_search`, which now
follows `pagination.next` rather than incrementing `page`. The `page` field on
`ChroniclingAmericaSearchParams` remains legacy-only internal state.

## Consequences
+ Correct cross-issue result pagination on loc.gov.
+ Termination is driven by the API's own cursor, not a guessed `totalPages`.
- Legacy and loc.gov pagination diverge; the difference lives in each builder's
  `fetch_all_*` (legacy loops `page`; loc.gov follows `next`).
- Anyone adding a loc.gov query must not reuse `sp` for paging. `sp` is only
  meaningful when deliberately requesting a specific physical page of an issue.
- `paginate_search` uses `pagination.next` for both advancing and stopping.
  Legacy responses carry no `pagination.next`, so on a legacy builder
  `paginate_search` yields only the first page — a known, accepted limitation
  per ADR 0005 (legacy is runnable-but-not-fully-correct), not a bug to fix.

## Alternatives considered
- **`sp = (page - 1) * rows + 1`** (treat `sp` as an offset) — rejected: `sp` is
  not an offset; it is a physical-page filter, so arithmetic on it is meaningless.
- **Keep incrementing `page` and trust `totalPages`** — rejected: loc.gov does
  not return a reliable total-pages count in this shape, and the cursor is
  authoritative.