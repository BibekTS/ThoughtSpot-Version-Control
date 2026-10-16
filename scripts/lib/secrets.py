"""Pick the ThoughtSpot credentials for the org being migrated."""
from __future__ import annotations

import os


def secret_env_name(base: str, org_key: str | None = None) -> str | None:
    """
    Name of the environment variable that holds `base` for this org, or None if none is set.

    In order:
      1. <base>_<ORG_KEY_UPPER>   e.g. TS_SOURCE_SECRET_KEY_SALES (local runs, other runners)
      2. <base>__OVERRIDE         filled by the workflow from the repository or environment
                                  secret named <base>_<org_key>; empty when that secret is absent
      3. <base>                   the shared secret every org uses by default
    """
    org_key = org_key if org_key is not None else os.environ.get("ORG_KEY", "")
    candidates = ([f"{base}_{org_key.upper()}"] if org_key else []) + [f"{base}__OVERRIDE", base]
    return next((name for name in candidates if os.environ.get(name)), None)


def resolve_secret(base: str, org_key: str | None = None) -> str:
    """The value for `base`, preferring this org's override. KeyError if nothing is set."""
    name = secret_env_name(base, org_key)
    if name is None:
        raise KeyError(base)
    return os.environ[name]
