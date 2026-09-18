# Engineering journal

This is not a changelog. It is a record of the real investigations behind
this pipeline: what was suspected, what was actually tested, what the test
showed, what was decided, and what is still open. Every number here comes
from an actual run against real data or a real fixture, nothing is estimated
or rounded for effect. Where a decision was made not to fix something, that
is stated plainly too, this project treats an honestly documented gap as
more useful than a silently closed one.

Entries are in the order the investigations happened, Phase A through the
README diagram fix.

---

## 1. `comp_total_usd` never actually converted anything to USD

**Question:** the original salary mart exposed a column named
`comp_total_usd`. Does it actually hold a USD-converted figure?

**Hypothesis:** given the ingest code only parses `CompTotal` from the raw
survey and writes it straight through, the column is very likely just the
respondent's self-reported figure in whatever currency they answered in, not
a converted USD amount.

**Fixture / experiment:** read `scripts/ingest_survey.py` and the mart SQL
directly, end to end, looking for any currency-conversion step (a rate table,
an API call, a hardcoded exchange rate). Cross-checked against Stack
Overflow's own published survey methodology, which documents that their
`ConvertedCompYearly` field applies a specific conversion-date exchange rate,
a step this pipeline never had.

**Observed result:** confirmed, no conversion step exists anywhere in the
pipeline. `comp_total_usd` held the raw local-currency figure for every
respondent, unconverted, under a name that told readers the opposite.

**Decision:** rename the column to `comp_total_raw` throughout the mart,
dbt models, and docs, and add an explicit note in `docs/data_contracts.md`
that currency conversion is not implemented, contrasted with Stack Overflow's
own conversion-date methodology so a reader understands what is and is not
being done here.

**Regression test:** none needed, this was a naming and documentation fix,
not a logic bug, there is no wrong output to regress against.

**Remaining limitation:** salary comparisons across countries in
`mart_salary_analytics` are still not currency-normalized. A reader comparing
a US cell to a country with a very different currency is comparing
unconverted local-currency figures. This is stated directly in
`docs/data_contracts.md`, not hidden behind the old misleading column name.

---

## 2. Two competing publish candidates could resolve last-write-wins

**Question:** the release/candidate/publish pattern is supposed to guarantee
that a bad or partial run can never silently become the official published
answer. Does it actually hold up if two different candidate releases are
racing to publish at once?

**Hypothesis:** if `publish_release` naively sets `dwh.active_release` to
whatever candidate it is currently handling, two overlapping publishes could
resolve as last-write-wins, letting an older, already-superseded candidate
overwrite a newer one that published first.

**Fixture / experiment:** a regression test that deliberately drives two
different candidate releases through `publish_release` out of order and
asserts which one ends up as the active pointer.

**Observed result:** confirmed. Without an ordering guard, the second
`publish_release` call to run would win regardless of which candidate was
actually newer, purely based on timing, not correctness.

**Decision:** add a `seq`-based monotonicity guard: `publish_release` only
moves the active pointer if the candidate's `seq` is greater than the
currently active release's `seq`. An older candidate publishing after a newer
one is a no-op, not a rollback.

**Regression test:** `tests/test_publication_safety.py::test_older_candidate_cannot_roll_back_newer_publish`,
independently re-run against real Postgres in this session and confirmed
passing alongside the rest of the publication-safety suite.

**Remaining limitation:** the guard is correct for a single warehouse-wide
pointer, but `seq` was still a single global sequence at this point in the
project. Phase E later found a related, more subtle version of this same
class of bug once a second survey year existed, see entry 5 below.

---

## 3. A truncated source file was silently published as a valid release

**Question:** as part of the Phase D failure-injection suite, what actually
happens if the survey source file is truncated mid-download or corrupted
before ingest, does the pipeline notice?

**Hypothesis:** the existing data-quality checks (`run_dq_checks`) should
catch an abnormally small or malformed load and block publication.

**Fixture / experiment:** `scripts/chaos/` injection 1: truncate a real
source CSV partway through and run it through the full pipeline, ingest, DQ,
dbt run/test, publish, against the live Docker Compose stack.

**Observed result:** it was **not** caught. `pandas` loaded the truncated
file without error (a partial but well-formed CSV parses fine), the existing
`null_country` check is log-only, and `total_rows_loaded` only blocked
publication if the row count was exactly zero. A file cut down to a
fraction of its real size sailed through every existing check and was
published as a valid release.

**Decision:** add a new DQ check, `row_count_drop`, that blocks publication
if the new raw row count falls more than 50% below the last published
release's row count for that year. The 50% threshold was reasoned about
explicitly, not picked arbitrarily: it needed to tolerate a real, legitimate
~27% year-over-year respondent count drop seen between the 2023 and 2024
datasets, while still catching the 13-row fixture case, which is nowhere
close to a real survey's scale either direction.

**Regression test:** `tests/test_dq_row_count_drop.py`, 2 tests, one proving
a truncated load is now blocked with the previous release staying live, one
proving a legitimate smaller-but-not-truncated load still publishes.
Independently re-run against real Postgres in this session, both passed.

**Remaining limitation:** the threshold is a heuristic, not a guarantee.
A source file truncated to exactly 51% of its previous size would still slip
through. This is stated as a known limitation rather than presented as a
hard correctness boundary.

---

## 4. Killing Postgres mid-run took the Airflow scheduler down with it

**Question:** what happens if the warehouse Postgres instance dies in the
middle of `dbt_run_models`, does the pipeline recover cleanly, and does the
eventual published data stay correct?

**Hypothesis:** since Airflow retries failed tasks, the expectation going in
was that killing Postgres mid-run would fail that task, Airflow would retry
after Postgres came back, and the pipeline would complete normally on retry.

**Fixture / experiment:** `scripts/chaos/` injection 3, against the live,
fully-running Docker Compose stack (not fixture-only CI): kill the Postgres
container while `dbt_run_models` is actively running, then bring it back and
observe.

**Observed result:** more severe than hypothesized. Airflow's own metadata
database shares the same Postgres instance as the warehouse in this stack's
current setup, so killing Postgres took the Airflow scheduler itself down,
not just the one task. Manual recovery was required. Once recovered, though,
the DAG ran a genuinely fresh, complete `dbt_run_models`, and the eventual
published mart data was correct, confirmed by directly comparing the
published salary cell (n=7, avg 249999.86, including the same quirky stale
`R008` value the fixture always produces due to its missing `loaded_at`)
against a clean baseline publish run immediately before the injection. The
data held; the operational recovery did not.

**Decision:** reclassify this finding in `docs/failure_modes.md` from an
initial read of "failed to fail closed" to the more accurate "correctness
held for the data; the gap is operational (shared Postgres, manual recovery
required), not a partial or corrupted publish." Do not fix the shared-Postgres
architecture in this phase, that is a real infrastructure change (a separate
metadata database for Airflow) out of scope for a chaos-testing phase.

**Regression test:** none, this is an infrastructure/operational finding, not
a code-level bug with a deterministic fixture to regress against.

**Remaining limitation:** documented explicitly in `docs/known_limitations.md`
and `docs/failure_modes.md`, Airflow's metadata DB still shares the warehouse
Postgres instance. A future hardening pass would split these, this project
chose to document the gap honestly rather than silently accept the risk or
overstate a fix that was never made.

---

## 5. A cross-year publish could have spuriously "superseded" the other year

**Question:** Phase E added a second real survey year (2023) publishing
independently alongside 2024. Given entry 2's `seq`-based monotonicity guard
was built for a single global pointer, does it still behave correctly once
two years can each have their own active release?

**Hypothesis, checked in design review before any code was written for this
phase:** `seq` remained a single global `BIGSERIAL` shared across all years.
A 2023 release built and published after several 2024 releases would have a
numerically *higher* `seq` than 2024's currently active release. If the
guard compared `seq` globally rather than per year, publishing 2023 could
either wrongly move 2024's active pointer, or a legitimate later 2024 rerun
could be spuriously treated as "already superseded" by an unrelated 2023
`seq` value.

**Fixture / experiment:** traced every read and write of `dwh.active_release`
and every `seq` comparison in `publish_release` and `mark_candidate` against
the planned schema, `survey_year INTEGER PRIMARY KEY` on `active_release`,
before implementation began, specifically looking for any comparison that
was not scoped to a single year.

**Observed result:** confirmed as a real design flaw, caught before it
shipped rather than after. The naive implementation of the year-aware schema
still would have compared `seq` globally in at least one code path.

**Decision:** scope every `dwh.active_release` read/write and every `seq`
comparison to `WHERE survey_year = %s`, and change the upsert to
`ON CONFLICT (survey_year) DO UPDATE`. Also scope `checksum`,
`row_count_drop`, and `duplicate_response_id` per year, since the same
`ResponseId` appearing in both the 2023 and 2024 datasets is not actually a
duplicate.

**Regression test:** `tests/test_year_2023.py::test_publishing_2023_does_not_move_2024_active`,
independently re-run against real Postgres in this session and confirmed
passing, along with the rest of the year-2023 suite (4 tests total).

**Remaining limitation:** `seq` itself is still a single global sequence
across all years, only the comparison logic is year-scoped. This was a
deliberate choice: rebuilding `seq` as a per-year sequence would have been a
larger schema change for no additional safety benefit once every comparison
that matters is already correctly scoped.

---

## 6. `avg_job_satisfaction` was silently NULL for every published cell since Phase A

This is the most significant finding in the whole project. It was not caught
by any passing test, it was caught because Phase F's dashboard forced a
direct look at real per-cell coverage instead of trusting a green test suite.

**Question:** while building the Phase F dashboard's AI-sentiment panel, a
review of the real published 2024 data showed every `avg_job_satisfaction`
cell in `mart_ai_sentiment` as NULL. Is that correct (the question genuinely
wasn't asked, or nobody answered it), or is it a bug?

**Hypothesis:** initially assumed correct, 2023's `JobSat` field is
genuinely absent from that year's survey (confirmed directly in Phase E), so
a NULL result looked at first like it might be expected behavior extending
to 2024 as well, or a coverage gap worth documenting rather than fixing.

**Fixture / experiment:** queried the real raw 2024 data directly for
non-null `job_sat` answers before assuming anything. 29,126 raw rows had a
real, non-null `job_sat` answer. That ruled out "the question wasn't asked"
immediately, the question was answered by tens of thousands of real
respondents, but the mart still showed 0 non-null published averages. Read
the actual mart SQL filter next: `job_sat ~ '^[0-9]+$'`. Tested that regex
directly against a real 2024 answer value, `'8.0'`.

**Observed result:** the regex was the bug. Every real 2024 `job_sat` answer
was formatted as a decimal string, `'8.0'`, `'6.5'`, and so on, not a bare
integer. `^[0-9]+$` rejected every single one of them. All 29,126 real
answers were silently filtered out before the average was ever computed.
0 of 3,828 published 2024 AI-sentiment cells had a real average, and the
dashboard was about to badge every one of them "Not asked in 2024," which
would have been false, the question was asked and answered at scale, the
mart just could not parse the real answer format. This had been happening
silently since Phase A, no test in the suite up to that point exercised a
decimal-formatted fixture value.

**Decision:** widen the regex to `^[0-9]+(\.[0-9]+)?$`, and republish 2024
against the real CDN data to confirm the fix against real numbers, not just
a fixture.

**Regression test:** added a decimal-formatted value (`'8.0'`) to the Phase A
hand-calculated raw fixture, and a new test,
`tests/test_mart_correctness.py::test_phase_a_raw_fixture_job_sat_uses_decimal_text`,
so this exact bug class cannot silently ship again. Independently re-run
against real Postgres in this session and confirmed passing, along with the
rest of the 25-test CI suite.

**Real before/after numbers, from an actual republish against the real CDN
dataset, not estimated:** before the fix, 0 of 3,828 published 2024
AI-sentiment cells had a non-null average. After the fix, 3,374 of 3,828
cells have a genuine average (minimum 0.00, maximum 10.00, mean 6.89 across
cells), the remaining 454 cells are correctly NULL because that specific
cell genuinely had no numeric answer, not because of a parsing failure.
2023 was confirmed unaffected and still correctly shows "Not asked in 2023,"
since `JobSat` genuinely was not on that year's survey.

**Remaining limitation:** the fix widens the regex to accept a decimal with
any number of digits after the point. It does not validate that the value is
within a sane 0 to 10 range before averaging, a malformed answer like
`'99.0'` would still be averaged in as-is if it ever appeared in the source
data. This is a real, if narrow, remaining gap, not currently guarded by a
range check.

---

## 7. Rebuilding the README diagram silently dropped two wildcard characters

**Question:** after the diagram in `README.md` was rebuilt from Unicode
box-drawing characters to plain ASCII to fix a rendering-alignment problem
(commit `074024b`), was the rebuilt content actually identical to what was
drafted, or did anything change along the way?

**Hypothesis:** since the rebuild was described as being reconstructed from
how the prompt's fenced code block arrived (wrapped as a single long line)
rather than copied verbatim, there was a real risk of a transcription error,
even though every automated check on the commit (em-dash count, a
line-width-verification script, and the full 25-test CI suite) passed clean.

**Fixture / experiment:** rather than trusting the passing checks as proof of
correctness, diffed the committed `README.md` against the exact drafted
content, byte for byte, in a fresh clone.

**Observed result:** confirmed a real transcription error the passing checks
had no way to catch. Two `*` wildcard characters were silently dropped inside
the diagram: `int_*_exploded` (shorthand for the
`int_languages_exploded`/`int_databases_exploded` model family) became
`int_exploded`, and `marts.mart_* directly for an official number` (referring
to the mart-prefixed tables readers are told never to query directly) became
`marts.mart directly for an official number`. Neither `int_exploded` nor
`marts.mart` are real objects anywhere in the repo, both changes silently
introduced incorrect information into a public-facing document.

**Decision:** fix only the two affected lines in a second, narrowly-scoped
commit (`634179b`), confirmed via `git diff` between the two commits that
nothing else in the file was touched.

**Regression test:** none, this is a documentation-content bug, not a code
path with a deterministic fixture to regress against. The mitigation instead
is the practice change captured below.

**Remaining limitation, and the actual takeaway from this finding:** an
automated check that verifies structure (width, character count, line count)
does not verify meaning. This project's standing practice was updated as a
direct result: any content meant to render in a fixed-width context, ASCII
art, tables, gets built with a width-verification script rather than
hand-aligned, but its final committed output is also diffed byte for byte
against the drafted source before being treated as correct, not just
spot-checked or judged by whether the automated checks pass.

---

## Also investigated, more briefly

- **A live CI teardown crash (Phase C):** the first real CI run built all 9
  dbt models successfully but crashed on teardown, protobuf 5 had removed a
  keyword argument that dbt 1.7.0's `MessageToJson` call still used. Fixed by
  pinning `protobuf>=4.21,<5`. Not a data-correctness bug, an environment
  dependency mismatch, but a real failure a green local test run would not
  have caught, only real CI did.
- **A dead production ingest URL (Phase D):** the real `SURVEY_ZIP_URL` used
  by the weekly `real-dataset-check.yml` workflow was a live 404, the Stack
  Overflow survey CDN had moved to `github.com/StackExchange/Survey` at some
  point after the URL was first written. This had been silently failing the
  weekly workflow. Fixed, and verified with a live ingest against the
  corrected URL: 65,437 real rows, 0 null `response_id`.
- **PostgreSQL `seq` jumping 7 to 40 after a kill injection (Phase D):**
  investigated as a possible data-loss signal, confirmed instead to be
  expected PostgreSQL sequence behavior, `nextval()` is never rolled back on
  an aborted transaction. Documented explicitly in `docs/failure_modes.md`
  so a future reader does not mistake it for unexplained data loss.

---

## What this journal is not

This is not a list of every commit. It is a record of investigations that
actually changed what was believed to be true about the pipeline, or that
changed the code as a direct result of a real, verified finding. Routine
refactors, dependency bumps, and formatting passes are
