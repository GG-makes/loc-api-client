# ADR 0006: Remove per-title issue discovery — unsupported by the new API

Status: Accepted
Date: 2026-07-07

## Context
The pre-migration code carried a per-title issue-discovery feature:
DiscoveryManager.discover_periodical_issues → LocApiClient.get_newspaper_issues,
which hit the legacy endpoint lccn/{lccn}.json and returned a clean `issues`
list (date_issued + url per issue). It was only ever half-wired — graph analysis
confirms no CLI command reaches discover_periodical_issues (its only caller is a
test), so it was never user-invokable.

This project's default policy (ADR 0004) is to preserve the original author's
work — even half-implemented — and defer, not delete. Deferral, however,
presumes a supported future target to migrate toward. Here there is none.

Live probing (2026-07-07) confirmed loc.gov has no per-title issues endpoint:
- item/{lccn}/?fo=json returns only coverage metadata (dates_of_publication,
  e.g. "1876-1880"), no issue list, empty resources.
- The only lccn-scoped access is the collection search filtered by
  fa=number_lccn:{lccn} (verified working: sn89053729 → 3,680 items / 57,132
  pages), which returns PAGE results, not issues. Issues could only be *derived*
  by paginating every page of a title and collapsing to distinct (date, edition).

That derivation would crawl tens of thousands of pages per title and duplicate
batch discovery, which already enumerates issues natively (batch → issues →
pages) and was repaired this cycle. loc.gov's model is batch-oriented for issue
enumeration; a per-title path fights the grain of the new API.

## Decision
Remove the per-title issue-discovery feature rather than migrate it: delete
DiscoveryManager.discover_periodical_issues, LocApiClient.get_newspaper_issues,
and their tests. Issue enumeration on loc.gov is served by batch-based discovery.

This is a deliberate, documented exception to ADR 0004. The distinction that
justifies it: ADR 0004 defers work that is *unfinished*; this feature's
data-access pattern ("all issues of a title by LCCN") is *structurally
unsupported* by the new API. There is nothing to defer toward.

Note: the supporting database features in storage.py were not touched. 
Allowing periodical data to be saved does not affect the rest of the code.

## Consequences
+ Removes dead, legacy-coupled code that would otherwise demand a wasteful,
  redundant redesign.
+ Aligns issue discovery with the API's native batch-oriented model.
- Loses a per-title issue-enumeration entry point. If that access pattern is
  ever genuinely wanted, it must be rebuilt as a NEW feature on the batch path
  (lccn → containing batches via the batch_utils mapping → issues), not
  resurrected from this method.
- Establishes a precedent: ADR 0004's "preserve and defer" yields when the new
  API cannot structurally support a feature's access pattern.
- get_newspaper_issues was also referenced by the deprecated api_client.py
  module and scratch scripts; those are independently dead and removed/accepted-
  broken, so this deletion strands nothing live.

## Alternatives considered
- **Migrate via lccn-filtered search + issue derivation** — rejected: paginates
  thousands of pages per title, duplicates batch discovery, and fights the API's
  grain for no unique capability.
- **Keep it legacy-only** (runnable under API_VERSION=LEGACY per ADR 0005) —
  rejected: the legacy API is retired, so it cannot actually run; it would be
  permanently dead even on the legacy path, and it was never CLI-wired.
- **Defer per ADR 0004 (TODO + keep)** — rejected: deferral implies a reachable
  future target; there is no supported loc.gov mechanism to defer toward.