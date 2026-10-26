# TS-Version-Control

Version control and promotion for ThoughtSpot content. It migrates tagged objects from a **source (dev)** org to a **target (prod)** org through Git, so every change is exported as TML, committed, reviewed and then imported, with a report for each run.

It also carries over Spotter content that TML does not include: **model memory** (rules and reference questions) and **model-level NL instructions**.

- **Source is read-only**, except that the migration tag is removed from objects that migrated successfully.
- **Nothing is ever deleted** in the target.
- **Objects are matched by `obj_id`**, so re-running updates the existing target object instead of creating a duplicate.

For setup (secrets, environments, branch protection) and troubleshooting, see [`docs/runbook.md`](docs/runbook.md). For API details, see [`docs/api-verification.md`](docs/api-verification.md).

## Console

[`site/`](site/) is a browser UI for the same workflows: start a dry or live run, follow each stage live, review and approve the promote PR, read the run's report, roll back to a restore point, and build a new org's config file. It is a static page that calls the GitHub API with a fine-grained token kept in your browser; there is no backend. `pages.yml` publishes it to GitHub Pages (Settings → Pages → Source: GitHub Actions). To try it locally, run `python3 -m http.server -d site 8000`. **Demo** simulates a run without a token.

## How it works

```
 tag objects in source ──► Actions: "ThoughtSpot Migration" ──► one pipeline per org
```

| Job | Stages | Environment | What happens |
|---|---|---|---|
| `extract` | 1–6 | `dev` | Validate config, authenticate, find tagged objects (and dependencies), export TML, export Spotter memory and NL instructions, sanitize, commit everything to `ts-dev` |
| `promote` | 7, 7b | – | Open a PR from `promote/<org>/<run_id>` into `ts-prod` holding only this run's files, ask GitHub to auto-merge it, then **wait until it is approved and merged**. On a dry run the PR is titled `[DRY RUN]`, auto-merge is **not** requested and nothing waits |
| `import` | 8–9c | `prod` | Take the org's TML from the promote PR's merge commit on `ts-prod` (so reviewer edits count), then sync variables, check connections, validate, import TML, then import memory and NL instructions |
| `untag_source` | 10 | `dev` | Confirm each object exists in the target, then remove the migration tag from the successes |
| `report` | 11 | – | Write the summary and failure reports (runs even if earlier jobs failed) |
| `release_tag` | – | – | After a live run with no failures, tag the imported `ts-prod` commit `migrate/<org>/<run_id>`: a restore point for the redeploy workflow |

### Stages

| Stage | Script | Purpose |
|---|---|---|
| 1 | `stage1_config` | Load and validate `config/orgs/<org>.yml` |
| 2 | `stage2_auth` | Trusted-auth tokens for source and target; abort on org mismatch |
| 3 | `stage3_discover` | Find objects with the migration tag, then what they are built on (a liveboard's model, a model's tables, joined tables), read from their TML and handled per `dependency_mode`. Objects without an `obj_id` are excluded and reported |
| 4 | `stage4_export` | Export TML (obj_id included, GUIDs excluded, coaching feedback included) |
| 4b | `stage4b_export_memory` | Export Spotter memory for each model |
| 4c | `stage4c_export_instructions` | Export NL instructions for each model |
| 5 | `stage5_sanitize` | Strip GUIDs and the migration tag, find `ts_var(name)` references (quoted or not; built-in variables like `ts_user_timezone` are ignored) |
| 6 | `stage6_commit` | Commit `orgs/<org>/` to `ts-dev` (retries on concurrent pushes) |
| 7 | `stage7_promote` | PR into `ts-prod` with this run's files (those stamped with the run's `TIMESTAMP`), auto-merge requested (not on dry runs). If GitHub refuses auto-merge, for example because "Allow auto-merge" is off in the repository settings, the log shows a warning, the PR body says a person must merge it, and `stage7_promote.json` records `auto_merge: false` |
| 7b | `stage7b_wait_for_merge` | Poll the promote PR until it is merged. Fails if the PR is closed unmerged or not merged within 330 minutes (`MERGE_WAIT_TIMEOUT_MIN`); the import is then skipped and the tag stays. Skipped on dry runs |
| 8b | `stage8b_snapshot` | Before importing, save the target's current version of every object about to be overwritten (live runs only). Fails the run if a copy cannot be saved |
| 8 | `stage8_prod_prep` | Create or check variables, check connections exist in the target, validate-only import |
| 9 | `stage9_import` | Import TML in dependency order; substitute table db/schema values, then bind each table's database and schema to their own variables (by GUID). A failed bind is a warning on that table in `stage9_import.json` |
| 9b | `stage9b_import_memory` | Import model memory |
| 9c | `stage9c_import_instructions` | Set model NL instructions |
| 10 | `stage10_postcheck` | Post-check and remove the migration tag from successes |
| 11 | `stage11_report` | Reports and job summary |

Import order for Stage 9 is tables, views, models, collections, answers, liveboards, then feedback. A tagged object that no step handles is reported as a failure and keeps its tag. Stage 9 fails closed: an import counts as successful only if every object in the response is OK or WARNING, and a table whose post-import read-back cannot be done is failed rather than trusted.

### Branches

| Branch | Holds |
|---|---|
| `ts-prod` | Default branch: pipeline scripts, config, and approved TML |
| `ts-dev` | Exports only (TML, memory, instructions, reports). Scripts are overlaid from `ts-prod` at run time |
| `promote/<org>/<run_id>` | One short-lived branch per run, the head of that run's promote PR. Deleted automatically once the PR merges or is closed (Stage 7b, and `cleanup-promote-branches.yml` for PRs closed by hand) |

The `extract` and `report` jobs run scripts from the branch you dispatch the workflow from. The `promote` and `import` jobs always check out and run `ts-prod`. A script change therefore only takes full effect once it is merged to `ts-prod`. The `import` job runs the current `ts-prod` scripts but imports the org's TML exactly as it was at the promote PR's merge commit (`import_source.json` and the job summary say which commit), never a later run's files.

## What is migrated

| Migrated | Not migrated |
|---|---|
| Tables, SQL views, models, answers, liveboards, collections, coaching feedback (as TML) | Connections (they must already exist in the target) |
| Variable definitions that the content references (`ts_var()`) | Per-user Spotter conversation history |
| Model memory: rules and recipes (reference questions) | Schedules, custom actions, custom calendars, column security rules |
| Model NL instructions | Deletions of any kind |
| | Sets (the search API has no type for them, so they cannot be discovered) |

## Spotter memory and NL instructions

Both are exported from the source for every model in the manifest, committed next to the TML, and imported after the TML import so the target model already exists.

| | Memory | NL instructions |
|---|---|---|
| Export / import stages | 4b / 9b | 4c / 9c |
| File | `orgs/<org>/memory/memory__<TIMESTAMP>.yaml` | `orgs/<org>/instructions/instructions__<TIMESTAMP>.json` |
| API | `/ai/memory/export`, `/ai/memory/import` | `/ai/instructions/get`, `/ai/instructions/set` |
| Needs | 26.8.0.cl or later (beta), Spotter | 10.15.0.cl or later (beta), Spotter, `CAN_USE_SPOTTER`; edit access or `SPOTTER_COACHING_PRIVILEGE` to set |

Rules that apply to both:

- **Content in dev replaces the target.** Both imports replace what is on the target model. Anything typed directly into a target model is overwritten on the next run that has content for that model.
- **Nothing in dev means the target is left alone.** A model with no memory or no instructions in dev is never cleared on the target. This is enforced in the code, not just by convention.
- **Each run imports its own export.** The import reads the file stamped with the same `TIMESTAMP` as the run and never falls back to an older run's file, so stale content cannot overwrite the target.
- **Not-applicable cases are quiet, real failures are not.** Spotter not enabled (403), a model with no memory or no instructions, and a model that is not in the target yet are logged and skipped (Spotter-off is listed as a note in the report). Anything else, such as an export or validation error, a failed memory apply, or a failed instruction set, is listed in the run report, makes the run red, and **keeps the migration tag** on the affected models (all models for a memory failure) so the next run retries them.
- **Instructions are matched by `obj_id`.** The target model's GUID is looked up from the `obj_id`. A model that is not in the target yet is skipped.

What to look for in the `import` job log:

```
[STAGE 9b] Preview — model <guid>: RULES: existing=3, delete=3, insert=3
[STAGE 9b] Memory import status: SUCCESS
[STAGE 9c] 'Model': target has 0 instruction(s), source has 5 — replacing.
[STAGE 9c] 'Model': APPLIED — 5 instruction(s) set on target (HTTP 200, success=true).
[STAGE 9c] Summary (LIVE): 1 model(s) — 1 applied, 0 would change, 0 unchanged, 0 skipped, 0 failed.
```

## Dry run and live run

Dispatch **Actions → ThoughtSpot Migration → Run workflow** with an `org` key (or `all`) and `dry_run`.

| | Dry run (`dry_run: true`) | Live run |
|---|---|---|
| Stages 1–6 (extract, export, commit to `ts-dev`) | Run, **including the commit to `ts-dev`** | Run |
| Stage 7 (promote PR) | Runs and opens the PR titled `[DRY RUN]`, **without auto-merge** | Runs, auto-merge requested |
| Stage 8 (variables, connections, validate) | Runs, **writes nothing**: reports the variables it would create or set | Runs |
| Stage 9 (TML import) | Skipped | Runs |
| Stage 9b (memory) | Validates and logs counts, writes nothing | Validates, then applies |
| Stage 9c (instructions) | Reads the target and logs `would replace`, writes nothing | Applies |
| Stage 10 (untag source) | Skipped | Runs |

A dry run does not import any objects, memory or instructions, but it is not write-free: Stages 6–7 write to this repository. Stage 8 writes nothing to the target on a dry run; it logs `DRY RUN: would create variable ...` instead.

Stage 8 prints one line per issue it finds, for example `[STAGE 8] ERROR [<variable>]: <message>`. It records them in `prod_prep.json`. An object that fails its validate-only import is **skipped** in Stage 9, together with whatever depends on it, and keeps its migration tag (`validation_gate: block`, the default; `warn` imports it anyway). Stage 11 reports every Stage 8 error, and every validation failure, as a failure of the run; on a dry run this is where a failure shows up before a live run. Variable and connection errors name no single object, so they fail the run but do not skip anything.

## Configuration

One file per org in `config/orgs/<org_key>.yml` (the filename must match `org_key`). Copy `config/orgs/example.yml`.

```yaml
org_key: sales
display_name: Sales
migration_tag: sales_readyformigration   # the tag you apply in the source
# migration_tag_guid: "..."              # optional; needed if the service account can't search tags

source: { base_url: https://dev-ts.example.com,  org_id: 12 }
target: { base_url: https://prod-ts.example.com, org_id: 3 }   # same cluster, different org_id is fine

dependency_mode: include   # include | skip_if_exists | require_tagged
validation_gate: block     # block (default) | warn: what Stage 9 does with an object that failed Stage 8's validate-only import
# dependency_exclude: [TSBIServer-eaab6de7]   # obj_ids never pulled in as dependencies

variables:                 # TABLE_MAPPING variables for table database / schema
  database: { ts_variable_name: ts_db_name,     source_value: DEV_DB,     target_value: PROD_DB }
  schema:   { ts_variable_name: ts_schema_name, source_value: DEV_SCHEMA, target_value: PROD_SCHEMA }
  extra: []
```

**Dependencies.** For each tagged object Stage 3 exports its TML and follows the `obj_id` references it contains, repeatedly, so a tagged liveboard brings its model and the model brings its tables. Connections are not followed (they must already exist in the target).

| `dependency_mode` | Behavior |
|---|---|
| `include` | Pull every dependency that is not already in the manifest |
| `skip_if_exists` | As `include`, but leave out dependencies that already exist in the target (matched by `obj_id`) |
| `require_tagged` | Pull nothing. A tagged object that depends on an untagged object is excluded and reported, and so is anything that depends on an excluded object |

`dependency_exclude` is an optional list of `obj_id`s that are never treated as dependencies, in any mode. Use it for objects that exist everywhere and must not be migrated, such as ThoughtSpot's built-in `TS: BI Server` worksheet or sample data. **Do a dry run first:** the `extract` log prints every dependency it pulls (`[STAGE 3] Dependency: ...`), so you can see what a liveboard really depends on before anything is imported.

If `target_value` is blank, the variable must already exist in the target org. Stage 8 reports an error when it is missing or has a different type.

Secrets live in the `dev` and `prod` GitHub Environments (a repository variable `GH_APP_CLIENT_ID` and secret `GH_APP_PRIVATE_KEY` make a GitHub App open the promote PRs, so a lone maintainer can approve them; see the runbook): `TS_SOURCE_USERNAME`, `TS_SOURCE_SECRET_KEY`, `TS_TARGET_USERNAME`, `TS_TARGET_SECRET_KEY`, optionally `GH_AUTOMATION_TOKEN` for auto-merging promote PRs, and optionally per-org overrides such as `TS_TARGET_SECRET_KEY_SALES`. See the runbook.

## Snapshot, redeploy and rollback

**Snapshot.** Before Stage 9 overwrites anything, Stage 8b exports what the target holds for each object about to be imported and uploads it as the `snapshot-<org>-<run_id>` artifact (`snapshot/<org>/<folder>/<obj_id>.tml`, kept 90 days). Objects not yet in the target are listed as `new`. If a copy of an existing object cannot be saved the run stops before the import. Not covered: Spotter memory and NL instructions, sharing, and anything TML does not carry.

**Redeploy / rollback.** *Actions → ThoughtSpot Redeploy → Run workflow* with an `org` and a `ref` (a tag or commit on `ts-prod`) puts the target back into the state recorded in git, without touching the source org and without extracting, opening a PR or removing tags. It restores `orgs/<org>/` exactly as it was at the ref, rebuilds the run inputs from those files, and runs the normal stages 5, 8b, 8, 9, 9b and 9c, then writes the report.

- `dry_run` defaults to **true**; untick it to apply. It runs in the `prod` environment, so its required reviewers apply.
- The ref must be part of `ts-prod`'s history; anything else is refused, so only reviewed and merged content can be deployed.
- "State as of the ref" is the newest file for each object at that ref, and the newest Spotter memory and instructions files.
- Nothing is ever deleted: an object created after the ref stays in the target as it is, and a redeploy replaces Spotter memory and NL instructions with the ref's.
- It shares the `ts-migration-<org>` concurrency group, so it never runs alongside a migration of the same org.

Every live migration that finishes with no failed object is tagged `migrate/<org>/<run_id>` on the `ts-prod` commit it imported (a tag-creation problem shows as a failed `release_tag` job but does not fail the migration). Use such a tag as the redeploy `ref`. To roll back one bad migration, redeploy the tag of the run before it, or the commit on `ts-prod` just before that run's promote PR. The snapshot artifact is the other route, for objects that were not in git yet or that someone edited by hand in the target: it is plain TML you can import yourself.

## Running a migration

1. Make sure each object has an `obj_id`.
2. Apply the migration tag to the objects in the source org.
3. Run the workflow with `dry_run: true` and read the `import` and `extract` job logs.
4. Run it again with `dry_run: false`.
5. Check the run summary and the reports. Objects that failed or were skipped keep their tag, so a re-run retries them.

## Reading the results

- **Job logs:** the quickest view. Every stage prefixes its lines with `[STAGE n]`.
- **Artifacts** (kept 90 days): `extract-*` (exports and manifest), `import-*` (`prod_prep.json`, `stage9_import.json`, `stage9b_memory_import.json`, `stage9c_instructions_import.json`), `postcheck-*`, `reports-*`.
- **Reports:** `migration_summary.txt`, `failures_report.txt`, `failures.json`, `manifest.json`. They are also committed to `ts-dev` under `reports/<org>/<TIMESTAMP>_<run_id>/`, including for failed runs.

## Repository layout

```
.github/workflows/   ts-migration.yml (entry point), org-migration.yml (per-org pipeline), ts-redeploy.yml (redeploy/rollback), ci.yml (tests)
config/orgs/         one YAML per org
scripts/             stage*.py and lib/ (ts_client, tml_utils, artifacts, report)
orgs/<org>/          exported TML, memory/ and instructions/ (written by the pipeline)
docs/                runbook, API verification notes, open questions
tests/               pytest suite (run by ci.yml)
```

## Development

```
pip install -r requirements-dev.txt
python -m pytest
```

The tests are hermetic (no ThoughtSpot or GitHub calls). The `Tests` job in `.github/workflows/ci.yml` runs them on every pull request and on pushes to `ts-prod`.
