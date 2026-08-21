"""Apply the final design-approval UX and integrity improvements in one command.

Run from repository root after the Senior Design Approval feature itself is present:
    python staging/apply_design_approval_final_improvements.py
"""

from __future__ import annotations

import apply_design_approval_dashboard_improvements as dashboard
import apply_design_file_integrity_guard as integrity


def main() -> None:
    dashboard.main()
    integrity.main()
    print("Final design approval improvements complete.")


if __name__ == "__main__":
    main()
