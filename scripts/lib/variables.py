"""Read the optional `variables` block of an org config without assuming it is complete."""
from __future__ import annotations

FIELDS = ("ts_variable_name", "source_value", "target_value")


def variable_config(cfg: dict, field: str) -> dict[str, str]:
    """
    Settings for the `database` or `schema` variable, every key present as a string.

    A config may leave out the whole `variables` block, one of its entries, or any key in
    them (or set them to null). Missing means "not configured": an empty string, which the
    callers already treat as "nothing to substitute or bind".
    """
    block = (cfg.get("variables") or {}).get(field) or {}
    return {k: (block.get(k) or "") for k in FIELDS}
