# ThoughtSpot Migration Pipeline — Runbook

## Table of Contents

1. [Overview](#overview)
2. [Adding a New Org](#adding-a-new-org)
3. [Required Secrets and Environments](#required-secrets-and-environments)
4. [Branch Protection Setup](#branch-protection-setup)
5. [Running the Workflow](#running-the-workflow)
6. [Spotter Memory and NL Instructions](#spotter-memory-and-nl-instructions)
7. [Reading the Reports](#reading-the-reports)
8. [Re-queuing a Failed Object](#re-queuing-a-failed-object)
9. [Dry Run Mode](#dry-run-mode)
10. [Redeploy and Rollback](#redeploy-and-rollback)
11. [Cleaning up promote branches](#cleaning-up-promote-branches)
12. [Troubleshooting](#troubleshooting)

---

## Overview

This pipeline migrates ThoughtSpot content (Tables, Models, Answers, Liveboards, Collections, etc.) from a **source (dev)** cluster/org to a **target (prod)** cluster/org. Content is identified by a migration tag applied in the source. The tag name is the `migration_tag` value in the org config; the convention is `<org_key>_readyformigration`, but any non-empty name works.

The pipeline:
- Exports tagged objects as TML into the `ts-dev` GitHub branch
- Exports Spotter model memory and model NL instructions alongside the TML
- Opens a PR into `ts-prod` with auto-merge enabled (not on dry runs) and **waits for it to be approved and merged** (Stage 7b). Nothing is imported until then; if the PR is closed without merging, the run fails and the migration tag stays
- Imports TML, then model memory and NL instructions, into the target cluster
- Removes the migration tag from successfully migrated objects
- Produces a full report per run

**No deletions** ever occur. **Source is read-only** except for tag removal.

---

## Adding a New Org

### 1. Create the org config file

Create `config/orgs/<org_key>.yml` (org_key must be lowercase, no spaces):

```yaml
org_key: my_org                          # Must match filename without .yml
display_name: My Organization
migration_tag: my_org_readyformigration  # Tag you apply in the source (convention: <org_key>_readyformigration)
# migration_tag_guid: "..."             # Optional: needed if the service account cannot search tags

source:
  base_url: https://dev-ts.mycompany.com
  org_id: 12                             # ThoughtSpot org ID on the source cluster

target:
  base_url: https://prod-ts.mycompany.com
  org_id: 3                              # ThoughtSpot org ID on the target cluster

dependency_mode: include  # include | skip_if_exists | require_tagged
# dependency_exclude: [TSBIServer-eaab6de7]  # obj_ids never pulled in as dependencies

variables:
  database:
    ts_variable_name: ts_db_name         # Name of the TABLE_MAPPING variable
    source_value: DEV_DB                 # Source literal value (Path B detection only)
    target_value: PROD_DB                # Value to set in target (NEVER hardcode in TML)
  schema:
    ts_variable_name: ts_schema_name
    source_value: DEV_SCHEMA
    target_value: PROD_SCHEMA
  extra:
    # Optional: formula variables, connection properties used in RLS, etc.
    # - { ts_variable_name: region_filter, type: FORMULA_VARIABLE, target_value: "EMEA" }
```

**Dependencies.** Stage 3 finds what a tagged object is built on by exporting its TML and following the `obj_id` references in it (a liveboard's model, a model's tables, joined tables), repeatedly. It does not use the search API's `dependent_objects`, which lists the opposite direction (what depends on an object). `dependency_mode` controls what happens next: `include` pulls them all; `skip_if_exists` leaves out those already in the target; `require_tagged` pulls nothing and excludes (and reports) any tagged object that depends on an untagged one. Connections are never followed.

**`dependency_exclude`** lists `obj_id`s that must never be pulled in as dependencies. Built-in objects such as `TS: BI Server` or the sample retail data show up as dependencies of real content but exist everywhere; add their `obj_id`s here. A dry run prints every dependency (`[STAGE 3] Dependency: ...`) so you can check the list before a live run.

**`target_value` is optional.** If it is blank, the variable must already exist in the target org (with the same type); the pipeline checks this in Stage 8 and records an error if not. Provide a value only if you want the pipeline to create the variable and manage its org value.

**Same-cluster (org-to-org) migration:** Use the same `base_url` but different `org_id` values. Both secret keys will be the same value; set them identically in both `dev` and `prod` environments.

### 2. Add secrets to GitHub Environments

See [Required Secrets and Environments](#required-secrets-and-environments).

### 3. Tag content in the source cluster

In the source ThoughtSpot org, create the tag named in `migration_tag` (convention: `<org_key>_readyformigration`) and apply it to every object you want to migrate. Objects without this tag are ignored.

**Important:** Every object must have an `obj_id` set (contact ThoughtSpot Support to enable the Object ID feature on both clusters if not already active).

### 4. Run a dry run first

Trigger the workflow with `org=<org_key>` and `dry_run=true`. Review the generated report before running live.

---

## Required Secrets and Environments

The pipeline uses two GitHub Environments:

| Environment | Holds | Notes |
|-------------|-------|-------|
| `dev` | Source cluster credentials | `extract` and `untag_source` jobs use this |
| `prod` | Target cluster credentials | `import` job uses this; configure required reviewers here for approval gate |

### Secrets to configure

**In both environments:**

| Secret name | Description |
|-------------|-------------|
| `TS_SOURCE_USERNAME` | ThoughtSpot username for the source cluster |
| `TS_SOURCE_SECRET_KEY` | Trusted Authentication secret key for the source cluster |
| `TS_TARGET_USERNAME` | ThoughtSpot username for the target cluster |
| `TS_TARGET_SECRET_KEY` | Trusted Authentication secret key for the target cluster |
| `GH_AUTOMATION_TOKEN` | (Optional) GitHub PAT with `repo` + `workflow` scopes, needed for auto-merging PRs. If it is *your own* PAT, you cannot approve the PRs it opens; see below |
| `GH_APP_CLIENT_ID` (repository **variable**) and `GH_APP_PRIVATE_KEY` (secret) | (Optional) A GitHub App that opens the promote PRs, so one maintainer can approve them. Takes priority over `GH_AUTOMATION_TOKEN` |

**Per-org secret overrides:** by default every org uses the four secrets above. To give one org its own credentials, add secrets with the org key as a suffix, in the same environments (`dev` for the source side, `prod` for the target side):

| Secret name | Replaces, for that org only |
|-------------|-----------------------------|
| `TS_SOURCE_USERNAME_<ORG_KEY>` | `TS_SOURCE_USERNAME` |
| `TS_SOURCE_SECRET_KEY_<ORG_KEY>` | `TS_SOURCE_SECRET_KEY` |
| `TS_TARGET_USERNAME_<ORG_KEY>` | `TS_TARGET_USERNAME` |
| `TS_TARGET_SECRET_KEY_<ORG_KEY>` | `TS_TARGET_SECRET_KEY` |

For the org `sales` that is `TS_TARGET_SECRET_KEY_SALES`. Each one is optional and independent: set only what differs, and the rest fall back to the shared secret. GitHub secret names are case-insensitive, and may contain only letters, digits and underscores, so an org key with a hyphen cannot have overrides. Stage 2 prints which override it used (the name, never the value). The workflow reads the override with `secrets[format('TS_..._{0}', inputs.org_key)]`, which needs the caller's `secrets: inherit`; every stage that logs in goes through `scripts/lib/secrets.py`.

**To configure environments:**
1. Go to **Settings → Environments** in the GitHub repo
2. Create environments named `dev` and `prod`
3. For `prod`: add required reviewers to enforce the approval gate before import runs
4. Add the secrets listed above to each environment

### Branch protection

Configure the `ts-prod` branch with:
- **Require pull request reviews before merging** (at least 1 approving review). This is the approval the pipeline waits for, so it is required, not optional. Also turn on **Allow auto-merge** in the repository settings so the PR merges itself once approved
- **Require status checks to pass**: add `Tests` (the job in `ci.yml`). It runs on every PR, including the pipeline's own promote PRs, so it always reports. Do not require jobs from `ts-migration.yml`: that workflow is dispatched by hand and never reports on a PR, so requiring it would block every merge
- **Restrict who can push**: only the `GH_AUTOMATION_TOKEN` user (for auto-merge) and admins

### Approving the promote PRs as a solo maintainer

GitHub never lets you approve a pull request you authored, and no branch-protection setting changes that. The promote PRs are authored by whoever owns the token that opens them, so if that is your own personal token you cannot approve them. The fix is to have a *different identity* open them: a GitHub App.

1. **Create the app.** GitHub → your profile → *Settings → Developer settings → GitHub Apps → New GitHub App*. Name it (for example `ts-version-control-bot`), set any homepage URL, untick *Webhook → Active*, and under *Repository permissions* grant **Contents: Read and write**, **Pull requests: Read and write** (Metadata read-only is added automatically). Create it.
2. **Generate a private key** on the app's page (a `.pem` file downloads) and note the app's **Client ID**.
3. **Install the app** on this repository only (*Install App* in the app's sidebar).
4. **Add them to the repository** (*Settings → Secrets and variables → Actions*): a **variable** `GH_APP_CLIENT_ID` with the Client ID, and a **secret** `GH_APP_PRIVATE_KEY` with the whole contents of the `.pem` file.
5. **Branch protection on `ts-prod`**: require a pull request with **1 approval**, turn on *Allow auto-merge* in the repository settings, and do **not** add the app to any bypass list.
6. **Check it**: run a migration. The promote PR's author is now `<app name>[bot]`, you can approve it, and auto-merge then merges it and the import continues. If the variable or secret is wrong the first step of the `promote` job fails loudly rather than falling back silently.

You can then delete `GH_AUTOMATION_TOKEN`; with the app configured it is not used. Teams use the same setup (or a machine-user token) and simply have a teammate approve. The app token lasts about an hour, so it is only used to open the PR and request auto-merge; the long wait for the merge uses the job's own token.

Code PRs (like the ones that change this repository) are authored by whoever opened them. Without a second account you cannot approve those either; leave "Require approvals" off for them, or merge as an administrator.

**Auto-merge requirement:** The `GH_AUTOMATION_TOKEN` PAT must belong to a user with write access to the repo. Without it, the pipeline falls back to `GITHUB_TOKEN`, which cannot merge its own PRs — in that case the promote PR will not merge itself and a human must merge it. The `import` job waits for the PR either way (Stage 7b polls every 30 seconds for up to 330 minutes, set by `MERGE_WAIT_TIMEOUT_MIN` on the `promote` job). The token's owner cannot approve their own PR, so use a bot or service account for `GH_AUTOMATION_TOKEN`, not a reviewer's personal account. If the wait times out, merge the PR and re-run: objects keep their tag, and a re-run exports and promotes again.

---

## Running the Workflow

1. Go to **Actions → ThoughtSpot Migration → Run workflow**
2. Fill in:
   - **org**: org key (e.g. `sales`) or `all` to run every configured org
   - **dry_run**: `true` for validate-only (no writes); `false` for a live migration
3. Click **Run workflow**

Each org runs in its own job with its own concurrency group (`ts-migration-<org_key>`). A failure in one org does not block others.

### Jobs per org (sequential)

| Job | Stage | Environment | What it does |
|-----|-------|-------------|-------------|
| `extract` | 1–6 (incl. 4b, 4c) | `dev` | Config, auth, discover, export TML, export Spotter memory (4b) and NL instructions (4c), sanitize, commit to ts-dev |
| `promote` | 7, 7b | — | Create promote branch, open PR into ts-prod with auto-merge enabled, then wait for it to be approved and merged (dry run: PR titled `[DRY RUN]`, no auto-merge, no wait) |
| `import` | 8–9c | `prod` | Sync variables, connection preflight, validate, import TML (9), import memory (9b), set NL instructions (9c) |
| `untag_source` | 10 | `dev` | Post-check, remove migration tag from successes |
| `report` | 11 | — | Generate all report files (runs even on failure) |
| `release_tag` | — | — | Only after a live run with no failed object: tag the imported `ts-prod` commit `migrate/<org>/<run_id>` (restore point for `ts-redeploy.yml`) |

---

## Spotter Memory and NL Instructions

Model memory (rules and recipes) and model-level NL instructions are not part of TML, so they have their own stages.

| | Memory | NL instructions |
|---|---|---|
| Export / import | Stage 4b / 9b | Stage 4c / 9c |
| Committed file | `orgs/<org>/memory/memory__<TIMESTAMP>.yaml` | `orgs/<org>/instructions/instructions__<TIMESTAMP>.json` |
| API | `/ai/memory/export`, `/ai/memory/import` | `/ai/instructions/get`, `/ai/instructions/set` |
| Needs | 26.8.0.cl+ (beta), Spotter | 10.15.0.cl+ (beta), Spotter, `CAN_USE_SPOTTER`; edit access or `SPOTTER_COACHING_PRIVILEGE` to set |

Behavior:

- **Content in dev replaces the target.** Anything typed directly into a target model's memory or instructions is overwritten on the next run that has content for that model.
- **Nothing in dev means the target is left alone.** A model with no memory or no instructions in dev is never cleared in the target.
- **Each run imports its own export** (same `TIMESTAMP`). An older run's file is never used, so stale content cannot overwrite the target.
- **Not-applicable cases are quiet, real failures are not.** Spotter not enabled (403), a model with no memory or no instructions, and a model that is not in the target yet are logged and skipped. Anything else (an export or validation error, a failed memory apply, a failed instruction set) is listed under FAILED in the run report as `SPOTTER_MEMORY` / `SPOTTER_INSTRUCTIONS`, makes the run red, and **keeps the migration tag** on the affected models (every model for a memory failure), so re-running retries them. Models are matched by `obj_id`.

What to look for in the `import` job log:

```
[STAGE 9b] Preview — model <guid>: RULES: existing=3, delete=3, insert=3
[STAGE 9b] Memory import status: SUCCESS
[STAGE 9c] 'Model': target has 0 instruction(s), source has 5 — replacing.
[STAGE 9c] 'Model': APPLIED — 5 instruction(s) set on target (HTTP 200, success=true).
[STAGE 9c] Summary (LIVE): 1 model(s) — 1 applied, 0 would change, 0 unchanged, 0 skipped, 0 failed.
```

The per-stage results are also in the `import-<org>-<run_id>` artifact (`stage9b_memory_import.json`, `stage9c_instructions_import.json`).

---

## Reading the Reports

Reports are in two places:
1. **Workflow artifacts** (90-day retention): download `reports-<org_key>-<run_id>` from the Actions run page
2. **Git history**: committed to `ts-dev` under `reports/<org_key>/<TIMESTAMP>_<run_id>/`, where `TIMESTAMP` is the run's timestamp from the extract job (the same one in the exported file names)

The commit runs even when the run had failures. It is best-effort and never fails the job: if the push cannot be completed after 5 rebase-and-retry attempts, the job logs a warning and the artifact is the copy.

### Files

| File | Description |
|------|-------------|
| `migration_summary.txt` | Human-readable overview: counts and lists per section |
| `failures_report.txt` | One entry per failed object with error details |
| `failures.json` | Machine-readable failures (same data as txt, structured) |
| `manifest.json` | All objects considered for migration with their status |

### Summary sections

1. **PULLED** — objects identified for migration (tagged + dependencies). Problems found while discovering them (no `obj_id`, a dependency that cannot be found, a `require_tagged` exclusion) appear under FAILED with stage `discover`
2. **SUCCEEDED** — imported and post-check confirmed
3. **FAILED** — import failed; see `failures_report.txt` for details
4. **SKIPPED** — excluded due to missing obj_id, failed dependency, missing connection, etc.
5. **POST-MIGRATION CHECK** — target vs manifest counts; any missing objects
6. **TAG CLEANUP** — how many migration tags removed vs retained
7. **UNSUPPORTED CONTENT** — content the pipeline does not migrate (currently per-user Spotter conversation history; model memory and NL instructions are migrated)

### GitHub job summary

Each org's `report` job writes a summary table directly to the GitHub Actions job summary page (visible in the Actions UI without downloading artifacts).

---

## Re-queuing a Failed Object

When an object fails or is skipped due to a dependency failure:

1. Diagnose the error in `failures_report.txt` or `failures.json`
2. Fix the root cause (e.g. add the missing connection in the target, enable the Variable API)
3. **Re-apply the migration tag** to the failed object in the source cluster
   - Objects that failed keep their migration tag automatically
   - Objects where the dependency failed: re-tag the root dependency's parent object
4. Re-run the workflow

Because matching is done by `obj_id`, re-running **updates** the existing target object rather than creating a duplicate.

**Note:** An object whose migration tag was already removed (successful migration) will not be picked up on re-run unless you re-apply the tag in the source. Re-tagging is safe — the next run will update the target object in place.

---

## Dry Run Mode

A dry run runs Stages 1–8 and the read-only forms of 9b and 9c. It does **not** import objects, memory or instructions into the target, and it does not remove source tags. It is **not** write-free:

- **Stage 6** still commits the exports to `ts-dev`.
- **Stage 7** still pushes a `promote/<org>/<run_id>` branch and opens the PR, titled `[DRY RUN]`, but does **not** enable auto-merge. Nothing waits for this PR on a dry run (Stage 7b is skipped). Close the PR when you are done inspecting it, or merge it only if you intend to promote that content.
- **Stage 8** still runs, but writes nothing to the target on a dry run: it logs `DRY RUN: would create variable ...` / `would set the org value ...` and still checks connections and runs the validate-only import. A validation failure is reported as a failure of the dry run.

What is skipped or read-only:

- Stage 9 (live TML import) is skipped.
- Stage 9b validates the memory payload and logs the preview counts; nothing is applied.
- Stage 9c reads the target and logs `would replace`; nothing is set.
- Stage 10 (tag cleanup) is skipped.

Use dry runs to:
- Validate TML before committing to a migration
- Detect missing connections or variable mismatches
- Preview what would be migrated (objects, memory, instructions) before anything is imported

---

## Redeploy and Rollback

### What the snapshot is
Every live migration saves, before Stage 9, the target's current version of each object it is about to overwrite. It is the `snapshot-<org>-<run_id>` artifact of the run: `snapshot/<org>/<folder>/<obj_id>.tml` plus `stage8b_snapshot.json`, which lists each object as `captured`, `new` (not in the target yet, so there is nothing to save), `skipped` or `failed`. A `failed` object stops the run before the import; set `SNAPSHOT_REQUIRED=false` on the step to import anyway. To undo one object by hand, import its snapshot file into the target with the TML import API (or the UI).

### Redeploying an approved state
1. Find the ref you want the target to match: a restore-point tag `migrate/<org>/<run_id>` (the state after that run; list them with `git tag -l 'migrate/<org>/*'`), or any commit on `ts-prod`. For a rollback, use the tag of the run before the bad one, or the commit just before the bad run's promote PR was merged. Runs that had failures are not tagged.
2. *Actions → ThoughtSpot Redeploy → Run workflow*: set `org`, `ref` (commit SHA or tag) and leave `dry_run` ticked first.
3. Read the log: `[REDEPLOY] '<ref>' = <sha>: N object(s) ...` shows what will be applied. Stage 8 shows variable, connection and validation problems as in a migration.
4. Run it again with `dry_run` unticked. It waits for the `prod` environment's reviewers.
5. Read the report (`redeploy-<org>-<run>` artifact, `reports/<org>/...`). Failed objects make the job red.

Rules it follows: the ref must be in `ts-prod`'s history (otherwise: `is not part of origin/ts-prod's history`); the content is the newest file per object at that ref; nothing is deleted in the target, so objects created after the ref remain; Spotter memory and NL instructions are replaced with the newest files at the ref; the source org and the migration tags are never touched.

### Troubleshooting a redeploy
- **`Cannot resolve '<ref>'`**: the workflow checks out the full history and tags of `ts-prod`; use a commit SHA or tag that exists there.
- **`There is no content for org '<org>' at <sha>`**: nothing had been promoted for that org yet at that commit; pick a later one.
- **`Safety gate` / validation errors**: same causes as in a migration; see below.

---

## Cleaning up promote branches

Every run pushes a `promote/<org>/<run_id>` branch for its promote PR. They are removed automatically:

- **Stage 7b** deletes the branch as soon as the PR merges or is closed without merging.
- **`cleanup-promote-branches.yml`** deletes it when any promote PR is closed, which covers a `[DRY RUN]` PR you close by hand. A PR left open keeps its branch.
- **One-off sweep** for branches left over from before this existed: *Actions → Clean up promote branches → Run workflow*. It starts as a **preview** (`dry_run` ticked) and only lists what it would delete; untick `dry_run` to delete.

It only ever deletes branches named exactly `promote/<org>/<run_id>`, and never one that still has an open pull request. `ts-prod`, `ts-dev` and your own branches are never touched. Deleting a promote branch loses nothing: the exported TML is on `ts-dev`, and merged content is on `ts-prod`.

---

## Troubleshooting

### `migration_tag is required and must be non-empty`
The org config has no `migration_tag`. Set it in `config/orgs/<org_key>.yml`. The `<org_key>_readyformigration` naming is a convention, not enforced.

### `Token org mismatch`
The auth token returned by the cluster has a different `org_id` than configured. Check `org_id` in the config and confirm you're using the correct secret key for that org.

### `obj_id missing` for an object
The Object ID feature is not enabled on the source cluster, or the specific object was created before obj_ids were enabled. Contact ThoughtSpot Support to enable the Object ID feature and backfill existing objects.

### `Connection "X" missing in target org`
The table references a data source connection that doesn't exist in the target org. Create the connection in the target first (connections are out of scope for migration).

### `Variable API not available (403)`
The Variable API (beta) is not enabled on the cluster. Contact ThoughtSpot Support to enable it. The pipeline logs a warning and continues; variable sync will be skipped.

### Stage 8 reports errors about variables
Stage 8 prints one line per issue, e.g. `[STAGE 8] ERROR [bat_db_var]: ...`. A configured TABLE_MAPPING variable with a blank `target_value` raises an error when the variable is missing in the target org or exists with a different type. Create it in the target (Admin > Variables) with the right type, or set `target_value` in the org config so the pipeline creates it.

Stage 8 records its issues in `prod_prep.json`, and Stage 11 reports every error in it as a failure of the run. Variable errors do not skip objects by themselves, but they matter when you migrate tables that depend on those variables.

### An object was skipped with "validation failed in Stage 8"
Stage 8's validate-only import rejected it, so Stage 9 did not import it (or anything that depends on it) and it kept its migration tag. The reason is in the report and in the `[STAGE 8]` log. Fix it in the source and re-run. If the validate-only call is rejecting objects that would import fine (for example, on a first run where a dependency exists only in the same batch), set `validation_gate: warn` in the org config to import them anyway.

### Memory or NL instructions were not migrated
Check the Stage 4b / 4c / 9b / 9c lines in the job logs and the FAILED section of the report. The usual reasons that are *not* failures (nothing to do):
- Spotter is off or the user lacks `CAN_USE_SPOTTER` (HTTP 403).
- The cluster is older than 26.8.0.cl (memory) or 10.15.0.cl (instructions): the API answers 404 / 405 / 501 and the stage is skipped with a note.
- The model has no memory or no instructions in dev (the target is left alone on purpose).
- The model does not exist in the target yet (for example, a dry run before the first import).

These **are** failures (listed in the report, run red, tag kept): the export or validation returned an error, the memory apply did not succeed, or setting instructions was refused. A common cause of the last one is that setting instructions needs edit access or `SPOTTER_COACHING_PRIVILEGE` on the target model.

### Stage 9 skipped unexpectedly
Check that `dry_run` is set to `false` in the workflow dispatch inputs.

### PR auto-merge not working
`GH_AUTOMATION_TOKEN` is missing or the token user doesn't have write access to the repo. Either configure the secret or merge the PR manually.

### Push to ts-dev fails with `non-fast-forward`
Two orgs pushed simultaneously and one lost the race. The pipeline retries with jitter up to 6 times. If it still fails, re-run the workflow — this is a transient conflict.

### Safety gate fails: `source db/schema value found in target table`
After table import, the table still resolves to a source database/schema value. This means Path B substitution or variable binding did not apply correctly. Check:
1. `source_value` / `target_value` in config match the actual values in the TML
2. The Variable API is enabled and `ts_db_name` / `ts_schema_name` exist in the target
3. The `parameterize-fields` call succeeded (check stage 9 logs)
