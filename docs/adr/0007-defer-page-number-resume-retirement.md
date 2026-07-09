# ADR 0007: Cursor resume supersedes page-number resume; retire the page-number state later

Status: Accepted
Date: 2026-07-08

## Context
The migration introduced cursor-based resume: paginate_search (rate_limited_client.py)
follows the loc.gov `pagination.next` cursor, and discover_facet_content
(discovery_manager.py) checkpoints the next-page URL per response into the
`resume_cursor` column (storage.py), read back via FacetDiscoveryContext
(facet_processor.py) as `start_url`. This is the live, live-verified resume path
(see ADR 0002).

The original page-number resume mechanism still exists alongside it:
`current_page` / `resume_from_page` columns, and validate_and_fix_facet_status
(FacetStatusValidator, facet_processor.py), which detects an interrupted-but-
marked-completed facet via page-number heuristics (`current_page > 1`,
`resume_from_page > 1`) and flips it back to `discovering`.

The two do not conflict, because the cursor drives the actual resume and the
page numbers are now bookkeeping. Verified against the live CLI (newsagger.cli:cli;
cli_new.py is an abandoned stub):
- reset_stuck_facets (cli.py:1258) sets `current_page` and echoes "resume from
  page N", but never clears `resume_cursor`; re-discovery resumes from the cursor.
  The page echo is cosmetically stale, functionally correct.
- validate_and_fix_facet_status still fires on page heuristics; a genuinely
  interrupted facet now also carries `resume_cursor`, so the fix-up is redundant
  with the cursor path, not in tension with it.
- split_database (cli.py:1360) copies `current_page`/`resume_from_page` into
  worker DBs but not `resume_cursor`, so distributed workers restart a facet
  instead of resuming mid-cursor — graceful degradation (re-discovery + dedup),
  on an edge feature, and pre-existing.

Removing the page-number state cleanly is not a deletion: it means reworking
validate_and_fix_facet_status's interruption detection onto the cursor model
(likely: "resume_cursor is not None" fully supersedes the page heuristic), plus
its tests, plus a live captcha-interrupt/resume run to confirm the heuristic is
truly obsolete before it is removed. That is its own workstream, and — per the
migration's contributor posture (ADR 0005) — whether the author's CAPTCHA-recovery
heuristic dies is the author's call, not the migrator's.

## Decision
Leave the page-number resume state (`current_page`, `resume_from_page`,
validate_and_fix_facet_status) in place. Do not retire it as part of the
migration. `resume_cursor` is the authoritative resume mechanism; the
page-number state is vestigial-but-harmless bookkeeping.

Retirement is deferred with an explicit path: confirm via a live
captcha-interrupt/resume run that a non-NULL `resume_cursor` on a non-completed
facet fully covers what validate_and_fix_facet_status was catching; if so, delete
the page heuristic and columns; if not, adjust the heuristic to key off the
cursor. This follows the defer-don't-delete pattern of ADR 0004.

## Consequences
+ The migration bows out at a working, honest state without a risky removal of
  the author's recovery logic on incompletely-verified grounds.
+ The single live resume path (cursor) is unaffected; users downloading data are
  not touched by the vestigial state.
+ The retirement path is documented for the author or a future contributor
  instead of living in memory.
- Two resume mechanisms coexist, which is extra surface area for a contributor
  reading the code to understand (mitigated by this ADR).
- reset_stuck_facets prints a stale "resume from page N" message.
- split_database drops `resume_cursor`, so distributed workers re-discover rather
  than resume; if distributed processing becomes primary, carrying `resume_cursor`
  in that INSERT is the fix.

## Alternatives considered
- **Retire it now** — rejected for now: requires the validate_and_fix rework +
  live captcha validation, and unilaterally deletes the author's recovery heuristic
  (ADR 0005 posture). Legitimate only *paired with* the live validation.
- **Rip out the columns only, keep validate_and_fix** — rejected: breaks the
  heuristic's page-number reads with no replacement signal.
- **Fix split_database's resume_cursor drop now** — rejected: edge feature,
  graceful degradation today; folded into the deferred path.