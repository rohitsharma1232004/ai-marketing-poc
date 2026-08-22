"""Add local creative-file integrity guards after the design dashboard patch."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}.")
    return text.replace(old, new, 1)


def main() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    if "def verify_creative_file_integrity(" in text:
        print("app.py creative-file integrity guard already applied")
        return
    if "def render_design_approval_dashboard(" not in text:
        raise RuntimeError(
            "Design approval dashboard improvements are missing. Run "
            "staging/apply_design_approval_dashboard_improvements.py first."
        )

    old_render = r'''
def render_creative_file(asset, *, key_prefix):
    path = Path(asset["storage_path"])
    if not path.exists():
        st.warning("The creative file is not available on this app instance.")
        return
    if str(asset["mime_type"]).startswith("image/"):
        st.image(str(path), caption=f"{asset['file_name']} — v{asset['asset_version']}")
    elif asset["mime_type"] == "application/pdf":
        with open(path, "rb") as handle:
            st.download_button(
                "Open / Download Creative PDF",
                data=handle.read(),
                file_name=asset["file_name"],
                mime="application/pdf",
                key=f"{key_prefix}_pdf_download",
                use_container_width=True,
            )
'''
    new_render = r'''
def verify_creative_file_integrity(asset):
    """Return (ok, path, bytes, error) for a creative stored by this app."""
    output_root = Path(
        get_app_setting("CREATIVE_OUTPUT_DIR", str(DEFAULT_CREATIVE_OUTPUT_DIR))
    ).expanduser().resolve()
    try:
        path = Path(asset["storage_path"]).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return False, None, None, "The creative file is missing from this app instance."
    if path != output_root and output_root not in path.parents:
        return False, None, None, "The creative file path is outside the configured creative storage directory."
    try:
        raw = path.read_bytes()
    except OSError:
        return False, None, None, "The creative file could not be read."
    actual_hash = hashlib.sha256(raw).hexdigest()
    if actual_hash != str(asset.get("file_sha256") or ""):
        return False, None, None, "The creative file changed after upload and failed its SHA-256 integrity check."
    if len(raw) != int(asset.get("file_size") or 0):
        return False, None, None, "The creative file size changed after upload."
    return True, path, raw, ""


def render_creative_file(asset, *, key_prefix):
    ok, path, raw, error = verify_creative_file_integrity(asset)
    if not ok:
        st.error(error)
        return False
    if str(asset["mime_type"]).startswith("image/"):
        st.image(raw, caption=f"{asset['file_name']} — v{asset['asset_version']}")
    elif asset["mime_type"] == "application/pdf":
        st.download_button(
            "Open / Download Creative PDF",
            data=raw,
            file_name=asset["file_name"],
            mime="application/pdf",
            key=f"{key_prefix}_pdf_download",
            use_container_width=True,
        )
    else:
        st.error("The saved creative has an unsupported MIME type.")
        return False
    return True
'''
    text = replace_once(text, old_render, new_render, "secure creative file rendering")

    review_anchor = '''    st.markdown("### Creative to Review")
    render_creative_file(asset, key_prefix=f"design_review_{asset['id']}")

    decision_choice = st.radio(
'''
    review_new = '''    st.markdown("### Creative to Review")
    creative_file_ok = render_creative_file(
        asset, key_prefix=f"design_review_{asset['id']}"
    )
    if not creative_file_ok:
        st.error(
            "Design decision controls are locked because the exact uploaded creative "
            "cannot be verified. Ask the campaign owner to upload a fresh creative version."
        )
        return

    decision_choice = st.radio(
'''
    text = replace_once(text, review_anchor, review_new, "lock review on invalid file")

    controls_anchor = '''        st.markdown(f"**Creative Status:** {creative_status(latest_asset)}")
        render_creative_file(latest_asset, key_prefix=f"dashboard_{latest_asset['id']}")
        if latest_asset.get("latest_decision") == "rejected":
'''
    controls_new = '''        st.markdown(f"**Creative Status:** {creative_status(latest_asset)}")
        creative_file_ok = render_creative_file(
            latest_asset, key_prefix=f"dashboard_{latest_asset['id']}"
        )
        if not creative_file_ok:
            st.error(
                "Senior review is blocked for this version. Upload the creative again as a new version."
            )
        if latest_asset.get("latest_decision") == "rejected":
'''
    text = replace_once(text, controls_anchor, controls_new, "dashboard creative integrity state")

    link_condition_anchor = '''        if latest_asset.get("latest_decision") != "approved":
            review_button_label = (
'''
    link_condition_new = '''        if latest_asset.get("latest_decision") != "approved" and creative_file_ok:
            review_button_label = (
'''
    text = replace_once(
        text, link_condition_anchor, link_condition_new, "block review link for invalid creative"
    )

    gate_anchor = '''    if gate == PUBLISHING_STATUS_READY:
        st.success("Publishing Gate: READY — every latest creative is Senior Design Approved.")
    else:
        st.warning("Publishing Gate: LOCKED — every post needs an approved latest creative.")
'''
    gate_new = '''    integrity_failures = []
    for asset in latest_assets:
        ok, _path, _raw, error = verify_creative_file_integrity(asset)
        if not ok:
            integrity_failures.append((int(asset["post_number"]), error))
    if gate == PUBLISHING_STATUS_READY and not integrity_failures:
        st.success(
            "Publishing Gate: READY — every latest creative is Senior Design Approved "
            "and its stored file passes integrity checks."
        )
    else:
        st.warning(
            "Publishing Gate: LOCKED — every post needs an approved, intact latest creative."
        )
        for post_number, error in integrity_failures:
            st.caption(f"Post {post_number} file check: {error}")
'''
    text = replace_once(text, gate_anchor, gate_new, "integrity-aware publishing gate")

    APP_PATH.write_text(text, encoding="utf-8")
    print("added creative-file integrity guard to app.py")


if __name__ == "__main__":
    main()
