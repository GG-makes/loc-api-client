# Architecture Decision Records

This directory records the significant architectural decisions made in this
project — primarily during the August 2025 migration from the retired
Chronicling America legacy API to the loc.gov API.

An ADR captures one decision: the **context** that forced it, the **decision**
itself, and the **consequences** it commits us to (plus the alternatives we
rejected). The goal is to answer "why is it built this way, and what did they
already rule out?" for anyone — including future us — arriving without the
original context.

## When to write one

Write an ADR when a choice is **costly to reverse** and **not obvious from the
code**. Architecture boundaries, cross-cutting patterns, API-compatibility
strategy, and deliberate deferrals qualify. Renames, local refactors, and
one-line facts do not — those live in code comments or MIGRATION.md.

## Conventions

- **Numbering**: zero-padded, sequential, assigned once and never reused
  (`0001`, `0002`, …). The number is a stable identifier; it does not imply
  priority.
- **Filename**: `NNNN-kebab-case-title.md`.
- **Status**: `Proposed` → `Accepted` → (`Superseded by NNNN` | `Deprecated`).
- **Immutability**: once an ADR is `Accepted`, don't rewrite its decision. If the
  decision changes, write a *new* ADR and mark the old one
  `Superseded by NNNN`. The history is the point. Fixing typos or adding a
  clarifying consequence is fine; reversing the decision in place is not.

## How to add one

1. Copy `template.md` to `NNNN-your-title.md` with the next free number.
2. Fill in the sections. Keep it short — a screen or two.
3. Add a row to the index below.
4. Open it in the same PR as the change it describes, when possible.

## Index

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-api-version-builder-processor-split.md) | Isolate each API version behind QueryBuilder and ResponseProcessor | Accepted |
| [0002](0002-locgov-sp-sequence-page-pagination.md) | loc.gov paginates via the pagination.next cursor, not `sp` | Accepted |
| [0003](0003-lazy-item-detail-enrichment.md) | Fetch loc.gov PDF/JP2 URLs lazily at download time | Accepted |
| [0004](0004-defer-preexisting-defects-during-migration.md) | Defer pre-existing defects and half-built features to post-migration | Accepted |
| [0005](0005-keep-legacy-api-path-alive.md) | Keep the legacy API path alive to preserve its test suite as a reference | Accepted |
| [0006](0006-remove-periodical-discovery.md) | An exception to keep pre-existing defects and half-built features - remove when post-Aug 2025 api logic does not support their future implementation. In this case, the half-built feature can never be fully implemented and should
be removed.