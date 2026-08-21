"""Apply the final design-approval UX, integrity, and runtime improvements.

Run from repository root after the Senior Design Approval feature itself is present:
    python staging/apply_design_approval_final_improvements.py
"""

from __future__ import annotations

import apply_design_approval_dashboard_improvements as dashboard
import apply_design_file_integrity_guard as integrity
import apply_design_approval_runtime_hotfix as runtime_hotfix


def main() -> None:
    dashboard.main()
    integrity.main()
    runtime_hotfix.main()
    print("Final design approval improvements complete.")


if __name__ == "__main__":
    main()
