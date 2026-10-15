# API Verification — ThoughtSpot REST API v2

All endpoints verified against the ThoughtSpot REST API v2 documentation and Spotter code MCP server.
Base path: `https://<cluster>/api/rest/2.0/`

---

## 1. Authentication

| Item | Detail |
|------|---------|
| **Endpoint** | `POST /api/rest/2.0/auth/token/custom` |
| **Method** | POST |
| **Request** | `{username, secret_key, org_identifier, validity_time_in_sec}` |
| **Response** | Bearer token string |
| **Token TTL** | 300 s (5 min) default; configurable via `validity_time_in_sec` |
| **Org scope** | `org_identifier` sets the org context for the issued token |
| **Org scope confirm** | The token is scoped to the org at issue time. Scripts re-authenticate per org and verify by comparing the org from config to the org used in auth. A mismatch aborts the org run. |
| **Verified against** | Spotter MCP docs |
| **Notes** | `secret_key` takes precedence over `password`. Trusted auth must be enabled per-cluster by ThoughtSpot Support. Different clusters have different secret keys — stored separately in GitHub Secrets as `TS_SOURCE_SECRET_KEY` / `TS_TARGET_SECRET_KEY`. For same-cluster orgs, both secrets are set to the same value. |

---

## 2. Metadata Search (Discover tagged objects)

| Item | Detail |
|------|---------|
| **Endpoint** | `POST /api/rest/2.0/metadata/search` |
| **Method** | POST |
| **Key request fields** | `tag_identifiers[]` (name or GUID), `type` (LIVEBOARD/ANSWER/LOGICAL_TABLE/COLLECTION), `subtypes[]` (TABLE/WORKSHEET/SQL_VIEW/AGGR_WORKSHEET), `include_dependent_objects`, `record_size`, `record_offset` |
| **Response fields** | `metadata_id` (GUID), `metadata_name`, `metadata_type`, `metadata_obj_id` (Custom object ID / obj_id — available 10.8.0.cl+), `dependent_objects`, `metadata_header` |
| **Returns `obj_id`?** | Yes — in `metadata_obj_id`. Requires Object ID feature enabled by ThoughtSpot Support (confirmed enabled). |
| **Returns dependents?** | Yes — via `include_dependent_objects: true`, but "dependents" are the objects that depend ON the one searched (e.g. the liveboards and answers built on a worksheet), the opposite of what migration needs. Stage 3 does not use it: it reads an object's prerequisites from its TML `obj_id` references instead. |
| **Pagination** | `record_size` / `record_offset` supported on `tag_identifiers` filter |
| **Verified against** | Spotter MCP docs |
| **Notes** | Objects without `metadata_obj_id` are excluded from migration and recorded as failures at stage `discover`. Do not auto-assign obj_ids. |

### Supported type + subtype values

| Content Type | `type` | `subtypes` |
|---|---|---|
| Tables | `LOGICAL_TABLE` | `["TABLE"]` |
| SQL Views | `LOGICAL_TABLE` | `["SQL_VIEW"]` |
| Models / Worksheets | `LOGICAL_TABLE` | `["WORKSHEET", "AGGR_WORKSHEET"]` |
| Answers | `ANSWER` | — |
| Liveboards | `LIVEBOARD` | — |
| Collections | `COLLECTION` | — |

> **TODO(verify):** Sets object subtype — suspected `AGGR_WORKSHEET` but needs live confirmation. Custom sets (not aggregate worksheets) may have a different subtype.

---

## 3. TML Export

| Item | Detail |
|------|---------|
| **Endpoint** | `POST /api/rest/2.0/metadata/tml/export` |
| **Method** | POST |
| **Request** | `{metadata: [{type, identifier}], export_associated, export_fqn, edoc_format, export_options}` |
| **`export_options` flags** | `include_obj_id: true`, `include_obj_id_ref: true`, `include_guid: false`, `export_with_associated_feedbacks: true` |
| **Exclude GUIDs/FQNs** | Set `include_guid: false` and `export_fqn: false` |
| **Include obj_id** | Set `include_obj_id: true` and `include_obj_id_ref: true` |
| **Spotter coaching/feedback** | Set `export_with_associated_feedbacks: true` (10.7.0.cl+) OR export separately with `type: FEEDBACK` |
| **Verified against** | Spotter MCP docs |
| **Notes** | `include_obj_id` / `include_guid` flags require Object ID feature enabled. Export format: JSON (default) or YAML. |

---

## 4. TML Import

### Synchronous

| Item | Detail |
|------|---------|
| **Endpoint** | `POST /api/rest/2.0/metadata/tml/import` |
| **Method** | POST |
| **Request** | `{metadata_tmls: [string], import_policy, create_new}` |
| **Import policies** | `PARTIAL` (default), `ALL_OR_NONE`, `VALIDATE_ONLY`, `PARTIAL_OBJECT` |
| **`create_new`** | `false` (default) — matches by `obj_id` to update existing objects |
| **Validate-only** | `import_policy: "VALIDATE_ONLY"` — no writes |
| **Partial success** | `import_policy: "PARTIAL_OBJECT"` — one bad object does not sink the batch |
| **`obj_id` matching** | When `create_new: false` and TML contains `obj_id`, ThoughtSpot matches by `obj_id` to update the existing object rather than create a duplicate |
| **Verified against** | Spotter MCP docs |

### Asynchronous

| Item | Detail |
|------|---------|
| **Endpoint** | `POST /api/rest/2.0/metadata/tml/async/import` |
| **Response** | `{task_id, task_status, org_id, ...}` |
| **Status polling** | `POST /api/rest/2.0/metadata/tml/async/status` with `{task_ids: [task_id], include_import_response: true}` |
| **Task statuses** | `IN_QUEUE`, `IN_PROGRESS`, `COMPLETED`, `FAILED` |
| **Available since** | 10.4.0.cl |
| **Verified against** | Spotter MCP docs |

---

## 5. Tags

| Operation | Endpoint | Method | Notes |
|---|---|---|---|
| Create | `POST /api/rest/2.0/tags/create` | POST | Requires `ADMINISTRATION` or `TAGMANAGEMENT` privilege. Returns `{name, id, ...}`. |
| Search | `POST /api/rest/2.0/tags/search` | POST | Returns `[{name, id, ...}]`. |
| Assign | `POST /api/rest/2.0/tags/assign` | POST | `{metadata: [{type, identifier}], tag_identifiers: [name or GUID]}` |
| Unassign | `POST /api/rest/2.0/tags/unassign` | POST | Same shape as assign. Returns 204. |
| **Verified against** | Spotter MCP docs | | |

**Tag types for assign/unassign:** `LIVEBOARD`, `ANSWER`, `LOGICAL_TABLE`, `LOGICAL_COLUMN`, `CONNECTION`, `COLLECTION`

**Notes:** TML does not carry tag assignments — they are re-applied separately via the tags assign API after import. The migration tag is **not** applied in the target org.

---

## 6. Variables (Beta — 26.4.0.cl+)

| Operation | Endpoint | Method | Notes |
|---|---|---|---|
| Create | `POST /api/rest/2.0/template/variables/create` | POST | `{type, name, is_sensitive}`. Types: `TABLE_MAPPING`, `CONNECTION_PROPERTY`, `FORMULA_VARIABLE`. Fails if name already exists. |
| Search | `POST /api/rest/2.0/template/variables/search` | POST | Filter by identifier, type, name pattern. Response format: `METADATA` or `METADATA_AND_VALUES`. |
| Update values | `POST /api/rest/2.0/template/variables/{identifier}/update-values` | POST | Operations: `ADD`, `REPLACE`, `REMOVE`, `RESET`. Org-scoped values set here. |
| **Verified against** | Spotter MCP docs | | |

**Scoping:** Variable values can be scoped to `org_identifier`, `principal_identifier`, or `model_identifier`.

**Beta status:** Variable APIs require explicit enablement by ThoughtSpot Support (26.4.0.cl+). The pipeline detects unavailability (403 / specific error code) and reports clearly rather than failing silently.

**Sensitive values:** `is_sensitive: true` prevents values from being read back. The pipeline never reads or copies source variable values — it only writes target values from config.

**ABAC formula variables:** `FORMULA_VARIABLE` values are set per-user via token at login time. The pipeline creates the variable definition only; values are not set org-wide.

---

## 7. Parameterization

| Item | Detail |
|------|---------|
| **Endpoint** | `POST /api/rest/2.0/metadata/parameterize-fields` |
| **Available since** | 26.5.0.cl |
| **Request** | `{metadata_type: "LOGICAL_TABLE", metadata_identifier, field_type: "ATTRIBUTE", field_names: ["databaseName", "schemaName"], variable_identifier}` |
| **Deprecated predecessor** | `POST /api/rest/2.0/metadata/parameterize` (10.9.0.cl, single field only) |
| **Verified against** | Spotter MCP docs |
| **Notes** | Used in Path B: after a table imports into the target, call `parameterize-fields` to bind `databaseName` and `schemaName` to the TABLE_MAPPING variable. One call can parameterize multiple fields simultaneously. |

---

## 8. Path A vs Path B — Parameterized Table Export

**Status: TODO(verify) — live test required**

The docs do not specify whether exported TML for a parameterized table carries:
- **Path A:** a variable reference (e.g., `db: "${ts_db_name}"`) — import preserves the binding automatically.
- **Path B:** the literal resolved value (e.g., `db: "DEV_DB"`) — substitution + re-parameterization required at import time.

**Working assumption: Path B.** The pipeline implements Path B (in-memory substitution before import, then `parameterize-fields` call after the table exists in the target).

**Required action:** Export one parameterized table from the dev cluster and inspect the TML `db`/`schema` fields. If Path A, remove the substitution logic. See `docs/open-questions.md` item #1.

---

## 9. Collections and Memories

### Collections

| Operation | Endpoint | Available since |
|---|---|---|
| Search | `POST /api/rest/2.0/collections/search` | 26.4.0.cl |
| Create | `POST /api/rest/2.0/collections/create` | 26.4.0.cl |
| TML export type | `COLLECTION` in export metadata type | 26.4.0.cl |
| obj_id support | Yes — `obj_id` / `collection_identifiers` accept custom object IDs | |

**Collections are in scope.** Exported as TML type `COLLECTION`, imported with the same obj_id-matching policy.

### Model memory and NL instructions (Spotter)

| Content | Export (Stage 4b / 4c) | Import (Stage 9b / 9c) | Available since |
|---|---|---|---|
| Model memory (RULE / RECIPE) | `POST /api/rest/2.0/ai/memory/export` | `POST /api/rest/2.0/ai/memory/import` (replaces target memory; validated with `dry_run` first) | 26.8.0.cl (beta) |
| Model NL instructions | `POST /api/rest/2.0/ai/instructions/get` (`getNLInstructions`) | `POST /api/rest/2.0/ai/instructions/set` (`setNLInstructions`; replaces target instructions) | 10.15.0.cl (beta) |

- Both require Spotter. NL instructions need `CAN_USE_SPOTTER` plus view access (get) or edit access / `SPOTTER_COACHING_PRIVILEGE` (set) on the model. Only `GLOBAL` scope exists today.
- Exports are written as `orgs/<org>/memory/memory__<TIMESTAMP>.yaml` and `orgs/<org>/instructions/instructions__<TIMESTAMP>.json`. The import job reads only the file carrying **its own run's** `TIMESTAMP`; it never falls back to an older run's file.
- Per-user Spotter conversation history is not migrated and is logged as unsupported in each run report.

---

## 10. Connection Preflight

| Item | Detail |
|------|---------|
| **Endpoint** | `POST /api/rest/2.0/connection/search` |
| **Request** | `{connections: [{name_pattern: "<name>"}], record_size: -1}` |
| **Response** | `[{id, name, data_warehouse_type, ...}]` |
| **Use** | Stage 8: verify a connection with the exact name from the table TML exists in the target org before import. |
| **Verified against** | Spotter MCP docs |

---

## 11. RLS and Variables

RLS rules using `ts_var()` appear in Table TML as formula expressions. The referenced variables must exist in the target org before the table can be imported successfully. The pipeline scans exported Table TML for `ts_var(...)` references and adds those variable names to the variable sync set (Stage 5), ensuring they are created in the target before import (Stage 8).

---

## Other Potentially Migratable Content

See `docs/open-questions.md` for content types identified but not yet in scope.
