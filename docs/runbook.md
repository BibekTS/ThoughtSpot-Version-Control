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
10. [Troubleshooting](#troubleshooting)

---

## Overview

This pipeline migrates ThoughtSpot content (Tables, Models, Answers, Liveboards, Collections, etc.) from a **source (dev)** cluster/org to a **target (prod)** cluster/org. Content is identified by a migration tag applied in the source. The tag name is the `migration_tag` value in the org config; the convention is `<org_key>_readyformigration`, but any non-empty name works.

The pipeline:
- Exports tagged objects as TML into the `ts-dev` GitHub branch
- Exports Spotter model memory and model NL instructions alongside the TML
- Opens a PR into `ts-prod` with auto-merge enabled (not on dry runs), as a record of what was promoted. The `import` job does **not** wait for this PR to merge; the approval gate is the `prod` GitHub Environment's required reviewers (if configured)
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
| `GH_AUTOMATION_TOKEN` | (Optional) GitHub PAT with `repo` + `workflow` scopes, needed for auto-merging PRs |

**Per-org secret overrides:** Stage 2 looks for secrets named `TS_SOURCE_SECRET_KEY_<ORG_KEY_UPPER>` (e.g. `TS_SOURCE_SECRET_KEY_SALES`), but the workflow only passes the four base secrets into the job environment, so overrides currently have no effect. All orgs use the base secrets.

**To configure environments:**
1. Go to **Settings → Environments** in the GitHub repo
2. Create environments named `dev` and `prod`
3. For `prod`: add required reviewers to enforce the approval gate before import runs
4. Add the secrets listed above to each environment

### Branch protection

Configure the `ts-prod` branch with:
- **Require pull request reviews before merging** (at least 1 reviewer)
- **Require status checks to pass** (add the `migrate` job from `ts-migration.yml`)
- **Restrict who can push**: only the `GH_AUTOMATION_TOKEN` user (for auto-merge) and admins

**Auto-merge requirement:** The `GH_AUTOMATION_TOKEN` PAT must belong to a user with write access to the repo. Without it, the pipeline falls back to `GITHUB_TOKEN`, which cannot merge its own PRs — in that case the promote PR will not merge itself and a human must merge it. The `import` job does not wait for the PR either way.

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
| `promote` | 7 | — | Create promote branch, open PR into ts-prod with auto-merge enabled (dry run: PR titled `[DRY RUN]`, no auto-merge) |
| `import` | 8–9c | `prod` | Sync variables, connection preflight, validate, import TML (9), import memory (9b), set NL instructions (9c) |
| `untag_source` | 10 | `dev` | Post-check, remove migration tag from successes |
| `report` | 11 | — | Generate all report files (runs even on failure) |

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
- **Never blocking.** A 403, API error, missing export, or a model that is not in the target yet is logged and skipped; the job still passes. Models are matched by `obj_id`.

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

Reports are in the **workflow artifacts** (90-day retention): download `reports-<org_key>-<run_id>` from the Actions run page.

**Known issue:** the `report` job also tries to commit them to `ts-dev` under `reports/<org_key>/<timestamp>_<run_id>/`, but Stage 11 names that folder with its own clock while the commit step looks for the extract job's timestamp, so the names never match and nothing is committed. Use the artifact.

### Files

| File | Description |
|------|-------------|
| `migration_summary.txt` | Human-readable overview: counts and lists per section |
| `failures_report.txt` | One entry per failed object with error details |
| `failures.json` | Machine-readable failures (same data as txt, structured) |
| `manifest.json` | All objects considered for migration with their status |

### Summary sections

1. **PULLED** — objects identified for migration (tagged + dependencies)
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
- **Stage 7** still pushes a `promote/<org>/<run_id>` branch and opens the PR, titled `[DRY RUN]`, but does **not** enable auto-merge. Close the PR when you are done inspecting it, or merge it only if you intend to promote that content.
- **Stage 8** still runs as normal. It can create missing variable definitions and set org-scoped values in the target when your config provides a `target_value` (or the variable is not a TABLE_MAPPING).

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

Stage 8 only records issues in `prod_prep.json`. It does not exclude objects or block the import, and Stage 11 only treats errors that name a connection as failures. Variable errors matter when you migrate tables that depend on those variables.

### Memory or NL instructions were not migrated
Check the Stage 4b / 4c / 9b / 9c lines in the job logs. The usual reasons, all non-blocking:
- Spotter is off or the user lacks `CAN_USE_SPOTTER` (HTTP 403).
- The cluster is older than 26.8.0.cl (memory) or 10.15.0.cl (instructions).
- The model has no memory or no instructions in dev (the target is left alone on purpose).
- The model does not exist in the target yet (for example, a dry run before the first import).
- Setting instructions needs edit access or `SPOTTER_COACHING_PRIVILEGE` on the target model.

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
