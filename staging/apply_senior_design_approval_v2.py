"""Retry the Senior Design Approval transformation after the v1 dashboard-anchor ambiguity.

This wrapper reuses the original transformation but makes the one ambiguous
`design_briefs = []` replacement context-specific. It is safe to run after v1
already patched campaign_store.py because the original store patch is idempotent.

Run from repository root:
    python staging/apply_senior_design_approval_v2.py
"""

from __future__ import annotations

import apply_senior_design_approval as original


_original_replace_once = original.replace_once


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if label == "creative dashboard state" and old == "    design_briefs = []\n":
        unique_old = "    client_record = None\n    design_briefs = []\n"
        unique_new = (
            "    client_record = None\n"
            "    design_briefs = []\n"
            "    latest_creative_assets = []\n"
        )
        return _original_replace_once(
            text,
            unique_old,
            unique_new,
            "creative dashboard state (context-specific)",
        )
    return _original_replace_once(text, old, new, label)


def main() -> None:
    original.replace_once = replace_once
    original.main()


if __name__ == "__main__":
    main()
