"""Apply the complete post-design-approval publishing feature to local sources.

Run after the existing Senior Design Approval + Brand Kit/Gemini Creative Studio
transforms have been applied:

    python staging/apply_meta_publishing_complete.py

This keeps Groq as the content-generation provider. It only adds the publishing
path after final Senior Design Approval.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "staging"


def run_script(name: str) -> None:
    path = STAGING / name
    spec = importlib.util.spec_from_file_location(f"_stage_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load staging script: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()


def main() -> None:
    run_script("apply_meta_publishing_ui.py")
    run_script("apply_publishing_final_hardening.py")
    print("Complete Meta image publishing feature applied. Groq content flow unchanged.")


if __name__ == "__main__":
    main()
