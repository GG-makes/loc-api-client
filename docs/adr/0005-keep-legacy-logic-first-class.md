# ADR 0005: Keep the legacy API path alive to preserve its test suite as a reference

Status: Accepted
Date: 2026-07-06

## Context
The legacy Chronicling America API was retired in August 2025, so a clean
migration could have deleted all legacy query/parsing/ingestion code outright.
Two things argued against that.

First — and primarily — identifying *how* behaviour changed between the two APIs
is genuinely hard: parameter semantics, response shapes, and pagination all
differ in subtle ways (see ADR 0001, ADR 0002). The legacy test suite encodes the
known-correct pre-migration behaviour. If legacy code were deleted, those tests
would have to be deleted or rewritten alongside it — and a rewritten test is no
longer a trustworthy baseline. A failure could mean "the migration broke
something" or "the rewrite is subtly wrong," and there would be no way to tell
them apart. The suite would become unreliable, or worse, unpredictably reliable.

Second, the storage/ingestion layer carries legacy-shaped database operations
that existing data and the download pipeline depend on; disrupting them mid-migration
adds risk on a side of the system the query migration doesn't need to touch yet.

## Decision
Keep both API versions runnable, selected at runtime by `Config`
(`API_VERSION` → `LEGACY` | `LOC_2026`), rather than doing a hard cutover. The
legacy `QueryBuilder` / `ResponseProcessor` remain first-class, fully-tested
classes — not deprecated stubs. The legacy test suite stays green and paired with
the code it exercises, serving as the behavioural reference the loc.gov path is
measured against. Storage/ingestion retains legacy-compatible operations where
the two paths overlap.

## Consequences
+ Legacy tests stay meaningful: a failure signals a real regression, not test
  churn. Tests remain reliably paired with the code they cover.
+ Provides a behavioural reference to diff loc.gov output against while migrating.
+ Lower risk — changes are incremental and reversible per version.
- Double the surface area: builder/processor pairs, and their tests, must stay
  symmetric. This is the soil the ADR 0001 bare-vs-composed-processor trap grew in.
- "Alive" means *runnable and tested*, not *fully correct*. Some legacy paths are
  knowingly broken and won't be fixed (e.g. legacy `paginate_search` yields a
  single page — see ADR 0002).
- Defers the decision of *when* to drop legacy. That needs an explicit future
  trigger, not passive decay — e.g. once loc.gov ingestion is validated
  end-to-end and no cached legacy-shaped data needs reprocessing.

## Alternatives considered
- **Hard cutover — delete legacy code and its tests** — rejected: destroys the
  reference behaviour and makes regressions indistinguishable from test rewrites,
  exactly when the migration most needs a trustworthy baseline.
- **Keep legacy code but delete only the legacy tests** — rejected: untested code
  rots, and the tests are the entire point of keeping the path.