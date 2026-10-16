#!/usr/bin/env python3
"""
Stage 2: Authenticate to source and target orgs.

Obtains bearer tokens using Trusted Auth (secret_key).
Verifies that tokens are scoped to the expected org_ids.
Aborts if there is any mismatch.

Secrets: TS_SOURCE_USERNAME, TS_SOURCE_SECRET_KEY,
         TS_TARGET_USERNAME, TS_TARGET_SECRET_KEY
         (read from environment; never logged). An org can use its own credentials by
         defining the same names with an _<ORG_KEY> suffix as GitHub secrets; see lib/secrets.py.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.secrets import resolve_secret, secret_env_name
from lib.ts_client import TSClient, AuthError

TOKEN_TTL = 3600  # 1 hour — enough for a full GH Actions job


def _get_secret(base: str, org_key: str) -> str:
    """The secret for this org: its own override if one is set, else the shared one."""
    name = secret_env_name(base, org_key)
    if name is None:
        print(f"[ERROR] Required secret {base} is not set.", file=sys.stderr)
        sys.exit(1)
    if name != base:
        print(f"[STAGE 2] Using the per-org override {name} for {base}.")   # the name, never the value
    return resolve_secret(base, org_key)


def build_client(label: str, base_url: str, org_id: int, username_var: str, key_var: str,
                 org_key: str) -> TSClient:
    username = _get_secret(username_var, org_key)
    secret_key = _get_secret(key_var, org_key)
    client = TSClient(base_url, org_id, username, secret_key, token_ttl_sec=TOKEN_TTL)
    try:
        client.authenticate()
    except AuthError as e:
        print(f"[ERROR] Auth failed for {label}: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[OK] Authenticated {label}: {base_url} org {org_id}")
    return client


def main():
    cfg_path = os.environ.get("CONFIG_OUT", f"/tmp/ts_migration_{os.environ['ORG_KEY']}_config.json")
    cfg = json.loads(Path(cfg_path).read_text())

    org_key = cfg["org_key"]
    src = cfg["source"]
    tgt = cfg["target"]

    source_client = build_client(
        "source", src["base_url"], src["org_id"],
        "TS_SOURCE_USERNAME", "TS_SOURCE_SECRET_KEY", org_key,
    )
    target_client = build_client(
        "target", tgt["base_url"], tgt["org_id"],
        "TS_TARGET_USERNAME", "TS_TARGET_SECRET_KEY", org_key,
    )

    # Tokens are kept in memory inside each client object.
    # Downstream stages receive the clients via the shared session directory.
    # For the GH Actions single-job execution, we re-build clients in each stage
    # from config + secrets. This avoids passing tokens across steps.
    print(f"[STAGE 2] Auth complete for org '{org_key}'.")


if __name__ == "__main__":
    main()
