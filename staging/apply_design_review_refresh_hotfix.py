"""Make Senior design-review status refresh obvious beside the review-link controls.

Run from the repository root after the Senior Design Approval UI is present:
    python staging/apply_design_review_refresh_hotfix.py

The patch is idempotent. It adds a post-level Refresh Senior Design Status button
and immediately reruns after a review link is generated so the pending-review
state is visible without pressing Generate/Replace a second time.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"


def replace_once_if_present(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    count = text.count(old)
    if count == 0:
        return text, False
    if count != 1:
        raise RuntimeError(f"{label}: expected at most one anchor, found {count}.")
    return text.replace(old, new, 1), True


def main() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    if "def render_creative_asset_controls(" not in text:
        raise RuntimeError(
            "app.py does not contain the Senior Design Approval feature. "
            "Apply that feature before running this hotfix."
        )

    changed = False

    # Link creation happens after the current asset state has already been loaded.
    # Rerun immediately so the same screen reflects Pending Senior Design Review.
    text, did_change = replace_once_if_present(
        text,
        "                    st.session_state[f\"design_review_url_{latest_asset['id']}\"] = url\n",
        "                    st.session_state[f\"design_review_url_{latest_asset['id']}\"] = url\n"
        "                    st.rerun()\n",
        "rerun after design review link creation",
    )
    changed = changed or did_change

    refresh_anchor = '''            if saved_url:
                st.markdown("**Senior Design Review Link**")
                st.code(saved_url, language=None)
                st.caption(
                    "Share this URL only with the intended Senior reviewer. Creating a replacement revokes the prior pending link."
                )
'''
    refresh_new = refresh_anchor + '''
            st.caption(
                "After the Senior approves or requests changes in the shared link, click Refresh below. "
                "Do not generate a replacement link just to check the decision."
            )
            if st.button(
                "Refresh Senior Design Status",
                key=f"refresh_design_status_post_{calendar['id']}_{post_number}_{latest_asset['id']}",
                use_container_width=True,
            ):
                st.rerun()
'''
    text, did_change = replace_once_if_present(
        text,
        refresh_anchor,
        refresh_new,
        "inline design review refresh",
    )
    changed = changed or did_change

    if "refresh_design_status_post_" not in text:
        raise RuntimeError("Inline Senior design-status refresh was not installed in app.py.")

    # Ensure link creation has an immediate rerun. If the exact assignment appears
    # but no rerun follows it, the pending status can look stale until another click.
    assignment = "st.session_state[f\"design_review_url_{latest_asset['id']}\"] = url"
    assignment_index = text.find(assignment)
    if assignment_index == -1:
        raise RuntimeError("Senior Design Review link assignment was not found in app.py.")
    nearby = text[assignment_index : assignment_index + len(assignment) + 80]
    if "st.rerun()" not in nearby:
        raise RuntimeError("Design review link creation still does not rerun the dashboard.")

    if changed:
        APP_PATH.write_text(text, encoding="utf-8")
        print("added inline Senior design status refresh to app.py")
    else:
        print("Senior design status refresh hotfix already applied")

    print("Design review refresh hotfix complete.")


if __name__ == "__main__":
    main()
