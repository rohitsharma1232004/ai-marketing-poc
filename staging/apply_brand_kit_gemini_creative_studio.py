"""Apply the Brand Kit + Gemini creative-generation phase in dependency order.

Run once from the repository root after the existing Senior Design Approval
feature/hotfixes are present:

    python staging/apply_brand_kit_gemini_creative_studio.py

This script edits source only. It does not open the campaign database and does
not make any Gemini API call. CampaignStore schema v10 is installed later when
the app/tests instantiate CampaignStore, so back up the real SQLite file before
starting Streamlit after this transformation.
"""

from __future__ import annotations

import apply_brand_kit_store_v9 as brand_store
import apply_creative_provenance_v10 as provenance
import apply_creative_studio_runtime_hotfix as studio_hotfix
import apply_creative_studio_ui as studio_ui
import apply_gemini_text_provider as gemini_text


def main() -> None:
    brand_store.main()
    gemini_text.main()
    studio_ui.main()
    studio_hotfix.main()
    provenance.main()
    print("Brand Kit + Gemini Creative Studio + provenance transformation complete.")


if __name__ == "__main__":
    main()
