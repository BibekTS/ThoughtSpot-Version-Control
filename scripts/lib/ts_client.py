"""
ThoughtSpot REST API v2 client.

Handles auth (trusted auth / secret_key), per-org token management,
retry with exponential backoff on 429/5xx, and re-auth on 401.

All network calls go through TSClient.request().
Secrets are never logged.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

TOKEN_TTL_BUFFER_SEC = 60   # re-auth this many seconds before the token expires
DEFAULT_TTL_SEC = 3600     # request long-lived tokens for GH Actions runs
MAX_RETRIES = 4
BASE_BACKOFF = 2.0


def session_org_id(me: dict) -> int | None:
    """
    Org the session is in, from the GET /auth/session/user response (`current_org.id`).

    None when the response does not say; the caller then cannot verify and only warns.
    `orgs` is every org the user belongs to, so it is not used to guess the current one.
    """
    current = me.get("current_org")
    if isinstance(current, dict) and current.get("id") is not None:
        return current["id"]
    return me.get("org_id")


class AuthError(RuntimeError):
    pass


class ApiError(RuntimeError):
    """A ThoughtSpot REST call returned a non-2xx status."""

    def __init__(self, what: str, status_code: int, body: str = ""):
        self.what, self.status_code, self.body = what, status_code, (body or "")[:300]
        super().__init__(f"{what} failed: HTTP {status_code} {self.body}".strip())


def raise_for_api_error(resp: requests.Response, what: str) -> None:
    """Raise ApiError unless resp is 2xx. Use for calls whose success the caller reports."""
    if not 200 <= resp.status_code < 300:
        raise ApiError(what, resp.status_code, getattr(resp, "text", ""))


class TSClient:
    """
    One instance per (cluster, org). Use source_client / target_client.
    """

    def __init__(
        self,
        base_url: str,
        org_id: int,
        username: str,
        secret_key: str,
        token_ttl_sec: int = DEFAULT_TTL_SEC,
    ):
        self.base_url = base_url.rstrip("/")
        self.org_id = org_id
        self._username = username
        self._secret_key = secret_key
        self._token_ttl_sec = token_ttl_sec
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._session = requests.Session()
        # mask secrets from any accidental logging
        self._session.headers.update({"Content-Type": "application/json"})

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Obtain a new bearer token for this org. Raises AuthError on failure."""
        url = f"{self.base_url}/api/rest/2.0/auth/token/full"
        payload = {
            "username": self._username,
            "secret_key": self._secret_key,
            "org_identifier": str(self.org_id),
            "validity_time_in_sec": self._token_ttl_sec,
            "persist_option": "NONE",
        }
        resp = self._session.post(url, json=payload, timeout=30)
        if resp.status_code != 200:
            raise AuthError(
                f"Auth failed for org {self.org_id} at {self.base_url}: "
                f"HTTP {resp.status_code}  [secret_key REDACTED]"
            )
        # Newer ThoughtSpot versions return {"token": "..."} (JSON object);
        # older versions return the token as a plain string (sometimes quoted).
        token = ""
        try:
            data = resp.json()
            if isinstance(data, dict):
                token = data.get("token") or data.get("access_token") or ""
            else:
                token = str(data).strip('"')
        except Exception:
            token = resp.text.strip().strip('"')
        if not token:
            raise AuthError(
                f"Auth returned 200 but no token found in response for org {self.org_id}"
            )
        self._token = token
        self._token_expires_at = time.time() + self._token_ttl_sec - TOKEN_TTL_BUFFER_SEC
        self._session.headers["Authorization"] = f"Bearer {token}"
        log.debug("Authenticated org_id=%s base_url=%s", self.org_id, self.base_url)

        # Cluster-level secret keys may land in the default org (0) regardless of
        # org_identifier in the token request. Set X-Org-Id header to scope all
        # subsequent requests to the configured org.
        self._session.headers["X-Org-Id"] = str(self.org_id)
        log.debug("Set X-Org-Id=%s for session", self.org_id)

        # Verify which org the session landed in: a cluster-level key can land in another
        # org than the one requested, and importing into the wrong org must never happen.
        session_org, user = None, "?"
        try:
            me_resp = self._session.get(
                f"{self.base_url}/api/rest/2.0/auth/session/user", timeout=15
            )
            if me_resp.status_code == 200:
                me = me_resp.json()
                session_org = session_org_id(me)
                user = me.get("name") or me.get("username") or "?"
            else:
                print(f"[AUTH][WARN] Could not verify the session org (HTTP {me_resp.status_code}).",
                      file=sys.stderr)
        except Exception as e:
            print(f"[AUTH][WARN] Could not verify the session org: {e}", file=sys.stderr)

        print(
            f"[AUTH] session_org={session_org if session_org is not None else 'unknown'} "
            f"configured_org={self.org_id} user={user}",
            file=sys.stderr,
        )
        if session_org is not None and str(session_org) != str(self.org_id):
            self._token = None
            raise AuthError(
                f"Session is in org {session_org} but org {self.org_id} is configured for "
                f"{self.base_url}. Refusing to continue: check the org_id in the config and "
                "that the service account's key can act in that org."
            )

    def _ensure_token(self) -> None:
        if self._token is None or time.time() >= self._token_expires_at:
            self.authenticate()

    # ------------------------------------------------------------------
    # Core request method
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        stream: bool = False,
        retry_on_not_found: bool = False,
    ) -> requests.Response:
        """
        Make an authenticated REST v2 call. Retries 429/5xx with backoff.
        Re-authenticates on 401 once.
        """
        self._ensure_token()
        url = f"{self.base_url}/api/rest/2.0{path}"
        authed_once = False

        for attempt in range(MAX_RETRIES + 1):
            resp = self._session.request(
                method.upper(),
                url,
                json=json_body,
                stream=stream,
                timeout=120,
            )

            if resp.status_code == 401 and not authed_once:
                log.warning("401 received; re-authenticating for org %s", self.org_id)
                self.authenticate()
                authed_once = True
                continue

            if resp.status_code == 429 or (500 <= resp.status_code < 600):
                if attempt < MAX_RETRIES:
                    wait = BASE_BACKOFF ** (attempt + 1)
                    log.warning(
                        "HTTP %s on %s %s — retry %d/%d in %.1fs",
                        resp.status_code, method, path, attempt + 1, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue

            return resp

        return resp  # exhausted retries

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def post(self, path: str, body: dict) -> requests.Response:
        return self.request("POST", path, json_body=body)

    def post_json(self, path: str, body: dict) -> Any:
        resp = self.post(path, body)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Common API calls
    # ------------------------------------------------------------------

    def search_metadata(
        self,
        *,
        types: list[str] | None = None,
        subtypes: list[str] | None = None,
        tag_identifiers: list[str] | None = None,
        include_dependent_objects: bool = False,
        record_size: int = -1,
    ) -> list[dict]:
        body: dict[str, Any] = {
            "record_size": record_size,
            "record_offset": 0,
            "include_headers": True,
        }
        if types:
            body["metadata"] = [{"type": t} for t in types]
        if subtypes:
            body["subtypes"] = subtypes
        if tag_identifiers:
            body["tag_identifiers"] = tag_identifiers
        if include_dependent_objects:
            body["include_dependent_objects"] = True
        return self.post_json("/metadata/search", body)

    def export_tml(
        self,
        metadata: list[dict],
        *,
        export_associated: bool = False,
        include_obj_id: bool = True,
        include_obj_id_ref: bool = True,
        include_guid: bool = False,
        export_with_feedbacks: bool = True,
    ) -> dict:
        body: dict[str, Any] = {
            "metadata": metadata,
            "export_associated": export_associated,
            "export_fqn": False,
            "edoc_format": "JSON",
            "export_options": {
                "include_obj_id": include_obj_id,
                "include_obj_id_ref": include_obj_id_ref,
                "include_guid": include_guid,
                "export_with_associated_feedbacks": export_with_feedbacks,
            },
        }
        return self.post_json("/metadata/tml/export", body)

    def import_tml(
        self,
        tml_strings: list[str],
        *,
        import_policy: str = "PARTIAL_OBJECT",
        create_new: bool = False,
    ) -> dict:
        body: dict[str, Any] = {
            "metadata_tmls": tml_strings,
            "import_policy": import_policy,
            "create_new": create_new,
        }
        return self.post_json("/metadata/tml/import", body)

    def import_tml_async(
        self,
        tml_strings: list[str],
        *,
        import_policy: str = "PARTIAL_OBJECT",
        create_new: bool = False,
    ) -> str:
        """Start async import; return task_id."""
        body: dict[str, Any] = {
            "metadata_tmls": tml_strings,
            "import_policy": import_policy,
            "create_new": create_new,
        }
        result = self.post_json("/metadata/tml/async/import", body)
        return result["task_id"]

    def poll_async_import(
        self,
        task_id: str,
        *,
        poll_interval: int = 10,
        timeout: int = 600,
    ) -> dict:
        """Poll until task is COMPLETED or FAILED; return full status object."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = self.post_json(
                "/metadata/tml/async/status",
                {"task_ids": [task_id], "include_import_response": True},
            )
            tasks = result if isinstance(result, list) else result.get("data", [result])
            for task in tasks:
                if task.get("task_id") == task_id:
                    status = task.get("task_status", "")
                    if status in ("COMPLETED", "FAILED"):
                        return task
            time.sleep(poll_interval)
        raise TimeoutError(f"Async import task {task_id} did not complete within {timeout}s")

    def search_tags(self, name_pattern: str | None = None) -> list[dict]:
        body: dict[str, Any] = {}
        if name_pattern:
            body["name_pattern"] = name_pattern
        return self.post_json("/tags/search", body)

    def create_tag(self, name: str) -> dict:
        return self.post_json("/tags/create", {"name": name})

    def ensure_tag(self, name: str) -> str:
        """Return GUID of tag by name, creating it if absent. Returns tag id."""
        tags = self.search_tags(name_pattern=name)
        for t in tags:
            if t["name"] == name:
                return t["id"]
        return self.create_tag(name)["id"]

    def assign_tags(self, metadata: list[dict], tag_identifiers: list[str]) -> None:
        """`metadata[].identifier` must be a GUID or name (not an obj_id). Raises ApiError."""
        raise_for_api_error(
            self.post("/tags/assign", {"metadata": metadata, "tag_identifiers": tag_identifiers}),
            "tags/assign")

    def unassign_tags(self, metadata: list[dict], tag_identifiers: list[str]) -> None:
        """`metadata[].identifier` must be a GUID or name (not an obj_id). Raises ApiError."""
        raise_for_api_error(
            self.post("/tags/unassign", {"metadata": metadata, "tag_identifiers": tag_identifiers}),
            "tags/unassign")

    def search_connections(self, name: str | None = None) -> list[dict]:
        body: dict[str, Any] = {"record_size": -1, "record_offset": 0}
        if name:
            body["connections"] = [{"name_pattern": name}]
        return self.post_json("/connection/search", body)

    def search_variables(
        self,
        *,
        variable_type: str | None = None,
        name_pattern: str | None = None,
        include_values: bool = False,
        page_size: int = 100,
        max_pages: int = 50,
    ) -> list[dict]:
        """
        All variables matching the filters, across pages.

        The API reads its filters from `variable_details[]` and returns only 10 records
        unless told otherwise. A top-level `name_pattern` is ignored, so a variable beyond
        the first 10 would look missing.
        """
        detail: dict[str, Any] = {}
        if variable_type:
            detail["type"] = variable_type
        if name_pattern:
            detail["name_pattern"] = name_pattern
        found: list[dict] = []
        for page in range(max_pages):
            body: dict[str, Any] = {
                "record_offset": page * page_size,
                "record_size": page_size,
                "response_content": "METADATA_AND_VALUES" if include_values else "METADATA",
            }
            if detail:
                body["variable_details"] = [detail]
            batch = self.post_json("/template/variables/search", body)
            found.extend(batch)
            if len(batch) < page_size:
                break
        return found

    def create_variable(self, name: str, var_type: str, is_sensitive: bool = False) -> dict:
        return self.post_json("/template/variables/create", {
            "name": name,
            "type": var_type,
            "is_sensitive": is_sensitive,
        })

    def set_variable_org_value(self, identifier: str, value: str) -> None:
        """
        Set org-scoped value for a TABLE_MAPPING or CONNECTION_PROPERTY variable.
        Uses REPLACE to overwrite any existing org-level value.
        """
        raise_for_api_error(self.post(
            f"/template/variables/{requests.utils.quote(identifier, safe='')}/update-values",
            {
                "operation": "REPLACE",
                "values": [{"value": value}],
            },
        ), "variables/update-values")

    def parameterize_fields(
        self,
        table_identifier: str,
        field_names: list[str],
        variable_identifier: str,
    ) -> None:
        """
        Bind databaseName / schemaName / tableName on a LOGICAL_TABLE to a variable.
        Requires ThoughtSpot 26.5.0.cl+. One call binds ALL of field_names to ONE variable,
        so bind databaseName and schemaName in separate calls. table_identifier must be a
        GUID or name (not an obj_id). Raises ApiError.
        """
        raise_for_api_error(self.post(
            "/metadata/parameterize-fields",
            {
                "metadata_type": "LOGICAL_TABLE",
                "metadata_identifier": table_identifier,
                "field_type": "ATTRIBUTE",
                "field_names": field_names,
                "variable_identifier": variable_identifier,
            },
        ), "metadata/parameterize-fields")

    def find_guid_by_obj_id(self, obj_type: str, obj_id: str) -> str | None:
        """
        Resolve an object's GUID in this org from its custom obj_id; None if absent.
        Most write APIs (tags, parameterize) take a GUID or name, not an obj_id.
        obj_type is LOGICAL_TABLE / ANSWER / LIVEBOARD / COLLECTION.
        """
        resp = self.post("/metadata/search", {
            "metadata": [{"type": obj_type, "obj_identifier": obj_id}],
            "include_headers": False,
            "record_size": 5,
            "record_offset": 0,
        })
        if resp.status_code != 200:
            return None
        for item in resp.json() or []:
            if item.get("metadata_obj_id") == obj_id and item.get("metadata_id"):
                return item["metadata_id"]
        return None

    def find_model_guid(self, obj_id: str) -> str | None:
        """Resolve a table / view / model's GUID from its obj_id; None if absent."""
        return self.find_guid_by_obj_id("LOGICAL_TABLE", obj_id)

    def get_nl_instructions(self, data_source_identifier: str) -> requests.Response:
        """Beta (10.15.0.cl+, Spotter). Returns the raw response so callers can branch on 403."""
        return self.post("/ai/instructions/get", {
            "data_source_identifier": data_source_identifier,
        })

    def set_nl_instructions(
        self, data_source_identifier: str, nl_instructions_info: list[dict]
    ) -> requests.Response:
        """Beta (10.15.0.cl+, Spotter). Needs edit access or SPOTTER_COACHING_PRIVILEGE."""
        return self.post("/ai/instructions/set", {
            "data_source_identifier": data_source_identifier,
            "nl_instructions_info": nl_instructions_info,
        })

    def search_collections(self, record_size: int = -1) -> list[dict]:
        return self.post_json("/collections/search", {"record_size": record_size})
