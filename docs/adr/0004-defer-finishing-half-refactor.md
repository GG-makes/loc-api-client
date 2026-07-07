# ADR 0004: Defer pre-existing defects and half-built features to post-migration

Status: Accepted
Date: 2026-07-06

## Context
Migration work keeps surfacing code that was broken or unfinished before the
migration began: `download_newspaper --estimate-only` (half-implemented — DB
opened before the check, hardcoded builder, "1836-None" date display),
`search_text` (walks all result pages to produce only a count; --limit sets
page size, not a cap), two tui_monitor bugs (a silently-caught NameError that
makes ETAs read "Calculating..." forever; a cross-process rate-limit singleton
that always reads empty), and `merge_databases` referencing columns that don't
exist. Fixing these inline would bloat migration changesets, blur commit
scope, and risk regressions in code the migration doesn't otherwise touch.
Deleting them would discard the original author's intent before it's evaluated.

## Decision
During the migration, pre-existing defects and half-implemented features found
in passing are neither fixed nor removed. Each gets a detailed TODO comment at
the site (what's wrong, why it's deferred) and an entry in MIGRATION.md's
Phase 4 checklist. Migration commits contain only migration work. The one
exception: a defect is fixed immediately if it blocks the migration path
itself (e.g. the parse_newspapers None-crash, fixed because integration tests
of migrated code exercised it).

## Consequences
+ Migration changesets stay reviewable and single-purpose.
+ The deferred list is explicit and lives in two greppable places (TODOs,
  MIGRATION.md Phase 4) instead of in anyone's memory.
- Known-broken features remain shipped on the branch; users can invoke
  --estimate-only or search_text and get misleading behaviour today.
- The Phase 4 backlog grows and must actually be burned down, or this becomes
  permanent deferral.

## Alternatives considered
- **Fix everything found** — rejected: unbounded scope creep; several finds
  (TUI singleton) are design problems, not quick fixes.
- **Delete half-built features** — rejected: destroys recoverable intent;
  --estimate-only is likely worth finishing.
- **Comment out broken paths** — rejected (considered explicitly for
  --estimate-only): leaves dangling dead code that confuses more than a
  documented TODO.