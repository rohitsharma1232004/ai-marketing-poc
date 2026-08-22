"""Add Brand Kit + in-app Gemini creative generation to the transformed app.

Prerequisites:
- Senior Design Approval feature and hotfixes already applied.
- CampaignStore Brand Kit schema v9 applied.

Run from repository root:
    python staging/apply_creative_studio_ui.py
"""

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
    if "def render_brand_kit_editor(" in text and "AI Creative Studio (Gemini)" in text:
        print("app.py Brand Kit + Gemini Creative Studio already applied")
        return
    if "def render_creative_asset_controls(" not in text:
        raise RuntimeError("Senior Design Approval UI must be applied first.")
    if "def verify_creative_file_integrity(" not in text:
        raise RuntimeError("Creative-file integrity guard must be applied first.")

    imports = '''from brand_design_brief import build_brand_aware_design_brief_prompt
from brand_kit import normalize_brand_kit, validate_logo_upload
from creative_studio import (
    build_branded_design_prompt,
    build_design_revision_prompt,
    generated_image_extension,
    recommended_aspect_ratio,
)
from gemini_api import (
    DEFAULT_GEMINI_IMAGE_MODEL,
    SUPPORTED_GEMINI_IMAGE_MODELS,
    SUPPORTED_IMAGE_ASPECT_RATIOS,
    GeminiAPIError,
    generate_image,
)
'''
    text = replace_once(
        text,
        "from revision_logic import (\n",
        imports + "from revision_logic import (\n",
        "Creative Studio imports",
    )

    text = replace_once(
        text,
        'DEFAULT_CREATIVE_OUTPUT_DIR = DEFAULT_GENERATED_OUTPUT_DIR / "creative_assets"\n',
        'DEFAULT_CREATIVE_OUTPUT_DIR = DEFAULT_GENERATED_OUTPUT_DIR / "creative_assets"\n'
        'DEFAULT_BRAND_ASSET_DIR = DEFAULT_GENERATED_OUTPUT_DIR / "brand_assets"\n',
        "Brand Kit asset directory",
    )

    brand_editor = r'''

def _brand_kit_logo_is_intact(kit):
    path_value = str((kit or {}).get("logo_storage_path") or "").strip()
    if not path_value:
        return False, None, None
    try:
        path = Path(path_value).expanduser().resolve(strict=True)
        raw = path.read_bytes()
    except (OSError, RuntimeError):
        return False, None, None
    if hashlib.sha256(raw).hexdigest() != str(kit.get("logo_sha256") or ""):
        return False, path, None
    if len(raw) != int(kit.get("logo_file_size") or 0):
        return False, path, None
    return True, path, raw


def render_brand_kit_editor(store, client):
    """Create or version a client Brand Kit used by every creative provider."""
    try:
        current_record = store.get_latest_brand_kit(client["id"])
    except PERSISTENCE_EXCEPTIONS as error:
        st.warning(f"Brand Kit could not be loaded: {error}")
        current_record = None
    current = dict((current_record or {}).get("kit") or {})
    defaults = normalize_brand_kit(current)

    with st.expander("Client Brand Kit", expanded=not bool(current_record)):
        st.caption(
            "Save the client's visual identity once. Design Briefs and Gemini creative prompts "
            "will reuse it without changing approved marketing content."
        )
        if current_record:
            st.success(f"Brand Kit v{current_record['version']} is active.")
        logo_ok, _logo_path, logo_raw = _brand_kit_logo_is_intact(defaults)
        if defaults.get("logo_storage_path"):
            if logo_ok:
                st.image(logo_raw, caption=f"Current logo: {defaults['logo_file_name']}", width=180)
            else:
                st.warning(
                    "The saved Brand Kit logo is missing or changed. Upload the logo again before relying on it."
                )

        with st.form(f"brand_kit_form_{client['id']}"):
            brand_name = st.text_input(
                "Brand Name",
                value=defaults.get("brand_name") or client.get("name") or "",
                max_chars=200,
            )
            col1, col2, col3 = st.columns(3)
            with col1:
                primary_color = st.text_input(
                    "Primary Color (hex)", value=defaults.get("primary_color") or ""
                )
            with col2:
                secondary_color = st.text_input(
                    "Secondary Color (hex)", value=defaults.get("secondary_color") or ""
                )
            with col3:
                accent_color = st.text_input(
                    "Accent Color (hex)", value=defaults.get("accent_color") or ""
                )
            font1, font2 = st.columns(2)
            with font1:
                heading_font = st.text_input(
                    "Heading Font Preference", value=defaults.get("heading_font") or ""
                )
            with font2:
                body_font = st.text_input(
                    "Body Font Preference", value=defaults.get("body_font") or ""
                )
            brand_voice = st.text_area(
                "Brand Voice", value=defaults.get("brand_voice") or "", max_chars=1200
            )
            visual_style = st.text_area(
                "Visual Style", value=defaults.get("visual_style") or "", max_chars=1200
            )
            preferred_imagery = st.text_area(
                "Preferred Imagery", value=defaults.get("preferred_imagery") or "", max_chars=1200
            )
            web1, web2 = st.columns(2)
            with web1:
                website = st.text_input(
                    "Website (optional)", value=defaults.get("website") or "", max_chars=500
                )
            with web2:
                instagram_handle = st.text_input(
                    "Instagram Handle (optional)",
                    value=defaults.get("instagram_handle") or "",
                    max_chars=200,
                )
            do_rules = st.text_area(
                "Brand DO Rules (one per line)",
                value="\n".join(defaults.get("do_rules") or []),
                max_chars=5000,
            )
            dont_rules = st.text_area(
                "Brand DON'T Rules (one per line)",
                value="\n".join(defaults.get("dont_rules") or []),
                max_chars=5000,
            )
            notes = st.text_area(
                "Additional Brand Notes", value=defaults.get("notes") or "", max_chars=4000
            )
            logo_upload = st.file_uploader(
                "Logo (optional — PNG/JPG, max 5 MB)",
                type=["png", "jpg", "jpeg"],
                key=f"brand_logo_{client['id']}_{(current_record or {}).get('version', 0)}",
            )
            save_brand = st.form_submit_button("Save Brand Kit", use_container_width=True)

        if save_brand:
            new_logo_path = None
            try:
                logo_metadata = {
                    key: defaults.get(key)
                    for key in (
                        "logo_file_name",
                        "logo_mime_type",
                        "logo_storage_path",
                        "logo_sha256",
                        "logo_file_size",
                    )
                }
                if logo_upload is not None:
                    logo_raw_new = logo_upload.getvalue()
                    validated_logo = validate_logo_upload(
                        logo_upload.name, logo_upload.type, logo_raw_new
                    )
                    logo_root = Path(
                        get_app_setting("BRAND_ASSET_DIR", str(DEFAULT_BRAND_ASSET_DIR))
                    )
                    client_dir = logo_root / client["id"]
                    client_dir.mkdir(parents=True, exist_ok=True)
                    new_logo_path = client_dir / (
                        f"{uuid4().hex}{validated_logo['extension']}"
                    )
                    new_logo_path.write_bytes(logo_raw_new)
                    logo_metadata = {
                        "logo_file_name": validated_logo["logo_file_name"],
                        "logo_mime_type": validated_logo["logo_mime_type"],
                        "logo_storage_path": str(new_logo_path),
                        "logo_sha256": validated_logo["logo_sha256"],
                        "logo_file_size": validated_logo["logo_file_size"],
                    }
                kit = normalize_brand_kit(
                    {
                        "brand_name": brand_name,
                        "primary_color": primary_color,
                        "secondary_color": secondary_color,
                        "accent_color": accent_color,
                        "heading_font": heading_font,
                        "body_font": body_font,
                        "brand_voice": brand_voice,
                        "visual_style": visual_style,
                        "preferred_imagery": preferred_imagery,
                        "website": website,
                        "instagram_handle": instagram_handle,
                        "do_rules": do_rules,
                        "dont_rules": dont_rules,
                        "notes": notes,
                        **logo_metadata,
                    }
                )
                store.save_brand_kit(client["id"], kit)
            except (OSError, PERSISTENCE_EXCEPTIONS) as error:
                if new_logo_path is not None:
                    new_logo_path.unlink(missing_ok=True)
                st.error(f"Brand Kit could not be saved: {error}")
            else:
                st.success("Brand Kit saved. New creative prompts will use this version.")
                st.rerun()
    return current

'''
    text = replace_once(
        text,
        "\ndef render_design_approval_dashboard(calendar, design_briefs, latest_assets):\n",
        brand_editor
        + "\ndef render_design_approval_dashboard(calendar, design_briefs, latest_assets):\n",
        "Brand Kit editor placement",
    )

    client_anchor = '''    if client_record is not None:
        st.caption(f"Client: {client_record['name']}")
'''
    client_new = client_anchor + '''        if campaign_store is not None:
            render_brand_kit_editor(campaign_store, client_record)
'''
    text = replace_once(text, client_anchor, client_new, "Brand Kit dashboard entry")

    # Brand-aware design brief generation.
    text = replace_once(
        text,
        "                        brief_prompt, source_posts = build_design_brief_prompt(\n",
        "                        brief_prompt, source_posts = build_brand_aware_design_brief_prompt(\n",
        "Brand-aware design brief builder",
    )
    brief_arg_anchor = '''                            campaign_intake=(campaign_record or {}).get("intake", {}),
                        )
'''
    brief_arg_new = '''                            campaign_intake=(campaign_record or {}).get("intake", {}),
                            brand_kit=(
                                (campaign_store.get_latest_brand_kit(client_record["id"]) or {}).get("kit")
                                if client_record is not None
                                else None
                            ),
                        )
'''
    text = replace_once(text, brief_arg_anchor, brief_arg_new, "Brand Kit in design brief prompt")

    old_controls_start = r'''def render_creative_asset_controls(store, campaign, calendar, client, brief_record):
    """Show provider-neutral prompt, upload/versioning, and secure design review controls."""
    post_number = int(brief_record["post_number"])
    try:
        approved_post = content_post_by_number(
            calendar["headers"],
            calendar["rows"],
            post_number,
            week_heading_prefix=WEEK_HEADING_PREFIX,
        )
        prompt = build_ai_design_prompt(
            brief_record["brief"],
            approved_post,
            client_metadata={
                **dict(calendar.get("client_metadata") or {}),
                "client_name": client.get("name") if client else "",
                "language": (campaign.get("intake") or {}).get("language", ""),
            },
        )
    except (TypeError, ValueError) as error:
        st.warning(f"Creative production controls are unavailable: {error}")
        return
'''
    new_controls_start = r'''def render_creative_asset_controls(store, campaign, calendar, client, brief_record):
    """Show provider-neutral prompt, upload/versioning, AI generation, and review controls."""
    post_number = int(brief_record["post_number"])
    brand_kit = None
    if client:
        try:
            brand_record = store.get_latest_brand_kit(client["id"])
            brand_kit = (brand_record or {}).get("kit")
        except PERSISTENCE_EXCEPTIONS as error:
            st.warning(f"Brand Kit could not be loaded for this creative: {error}")
    try:
        approved_post = content_post_by_number(
            calendar["headers"],
            calendar["rows"],
            post_number,
            week_heading_prefix=WEEK_HEADING_PREFIX,
        )
        prompt = build_branded_design_prompt(
            brief_record["brief"],
            approved_post,
            client_metadata={
                **dict(calendar.get("client_metadata") or {}),
                "client_name": client.get("name") if client else "",
                "language": (campaign.get("intake") or {}).get("language", ""),
            },
            brand_kit=brand_kit,
        )
    except (TypeError, ValueError) as error:
        st.warning(f"Creative production controls are unavailable: {error}")
        return
'''
    text = replace_once(text, old_controls_start, new_controls_start, "Brand-aware creative prompt")

    latest_anchor = '''    latest_asset = next(
        (item for item in assets if int(item["post_number"]) == post_number), None
    )
    if latest_asset:
'''
    gemini_studio = r'''    latest_asset = next(
        (item for item in assets if int(item["post_number"]) == post_number), None
    )

    with st.expander("AI Creative Studio (Gemini)", expanded=False):
        if latest_asset and latest_asset.get("latest_decision") == "approved":
            st.info(
                "The latest creative is already Senior Design Approved. Creating a new version "
                "will reopen design review, so AI generation is disabled here unless a revised version is required."
            )
        else:
            default_model = get_app_setting("GEMINI_IMAGE_MODEL", DEFAULT_GEMINI_IMAGE_MODEL)
            model_options = list(SUPPORTED_GEMINI_IMAGE_MODELS)
            if default_model not in model_options:
                default_model = DEFAULT_GEMINI_IMAGE_MODEL
            gemini_model = st.selectbox(
                "Gemini Image Model",
                model_options,
                index=model_options.index(default_model),
                key=f"gemini_image_model_{calendar['id']}_{post_number}",
            )
            recommended_ratio = recommended_aspect_ratio(
                brief_record.get("format", ""),
                approved_post.get("content", {}).get("Platform", ""),
            )
            ratio_options = list(SUPPORTED_IMAGE_ASPECT_RATIOS)
            gemini_ratio = st.selectbox(
                "Aspect Ratio",
                ratio_options,
                index=ratio_options.index(recommended_ratio) if recommended_ratio in ratio_options else 0,
                key=f"gemini_ratio_{calendar['id']}_{post_number}",
            )
            size_options = ["1K"] if gemini_model == "gemini-3.1-flash-lite-image" else ["1K", "2K", "4K"]
            gemini_size = st.selectbox(
                "Image Size",
                size_options,
                index=0,
                key=f"gemini_size_{calendar['id']}_{post_number}_{gemini_model}",
            )

            generation_prompt = prompt
            is_revision = bool(
                latest_asset and latest_asset.get("latest_decision") == "rejected"
            )
            if is_revision:
                try:
                    generation_prompt = build_design_revision_prompt(
                        original_prompt=latest_asset.get("design_prompt") or prompt,
                        senior_feedback=latest_asset.get("design_feedback") or "",
                        change_fields=latest_asset.get("design_change_fields") or [],
                        approved_post=approved_post,
                        brand_kit=brand_kit,
                    )
                except (TypeError, ValueError) as error:
                    st.warning(f"Revision prompt could not be prepared: {error}")
                    generation_prompt = prompt
                    is_revision = False

            editable_prompt = st.text_area(
                "Creative Prompt",
                value=generation_prompt,
                height=320,
                key=(
                    f"gemini_prompt_{calendar['id']}_{post_number}_"
                    f"{latest_asset['id'] if latest_asset else 'new'}"
                ),
                help=(
                    "You may refine visual direction, but do not change approved claims, CTA, "
                    "platform, format, or Senior-approved content."
                ),
            )
            generate_label = (
                "Generate Revised Creative with Gemini"
                if is_revision
                else "Generate Creative with Gemini"
            )
            draft_key = (
                f"gemini_creative_draft_{calendar['id']}_{post_number}_"
                f"{latest_asset['id'] if latest_asset else 'new'}"
            )
            if st.button(
                generate_label,
                key=f"generate_gemini_{draft_key}",
                use_container_width=True,
            ):
                gemini_key = get_app_setting("GEMINI_API_KEY")
                if not gemini_key:
                    st.error(
                        "GEMINI_API_KEY is missing. Add a Gemini Developer API key to the server configuration."
                    )
                else:
                    reference_bytes = None
                    reference_mime = ""
                    if is_revision and latest_asset and str(latest_asset.get("mime_type") or "").startswith("image/"):
                        ok, _path, raw_reference, _error = verify_creative_file_integrity(latest_asset)
                        if ok:
                            reference_bytes = raw_reference
                            reference_mime = latest_asset["mime_type"]
                    with st.spinner(
                        "Gemini is generating a revised creative..."
                        if is_revision
                        else "Gemini is generating the creative..."
                    ):
                        try:
                            generated = generate_image(
                                prompt=editable_prompt,
                                api_key=gemini_key,
                                model=gemini_model,
                                aspect_ratio=gemini_ratio,
                                image_size=gemini_size,
                                reference_image_bytes=reference_bytes,
                                reference_image_mime_type=reference_mime,
                                api_url=get_app_setting(
                                    "GEMINI_INTERACTIONS_URL",
                                    "https://generativelanguage.googleapis.com/v1beta/interactions",
                                ),
                            )
                        except (GeminiAPIError, TypeError, ValueError) as error:
                            request_suffix = (
                                f" Request ID: {error.request_id}"
                                if isinstance(error, GeminiAPIError)
                                else ""
                            )
                            st.error(f"Gemini creative generation failed: {error}.{request_suffix}")
                        else:
                            st.session_state[draft_key] = {
                                "image_bytes": generated.image_bytes,
                                "mime_type": generated.mime_type,
                                "prompt": editable_prompt,
                                "model": generated.model,
                                "request_id": generated.request_id,
                                "aspect_ratio": generated.aspect_ratio,
                                "image_size": generated.image_size,
                            }
                            st.rerun()

            draft = st.session_state.get(draft_key)
            if draft:
                st.markdown("**Generated Creative Preview**")
                st.image(draft["image_bytes"])
                st.caption(
                    f"{draft['model']} | {draft['aspect_ratio']} | {draft['image_size']} | "
                    f"Request ID: {draft['request_id']}"
                )
                st.caption(
                    "Preview first. Save only the version you want to send through the existing Senior Design Review workflow."
                )
                if st.button(
                    "Save Gemini Creative as New Version",
                    key=f"save_{draft_key}",
                    use_container_width=True,
                ):
                    storage_path = None
                    try:
                        extension = generated_image_extension(draft["mime_type"])
                        output_root = Path(
                            get_app_setting(
                                "CREATIVE_OUTPUT_DIR", str(DEFAULT_CREATIVE_OUTPUT_DIR)
                            )
                        )
                        post_dir = output_root / campaign["id"] / f"post_{post_number:02d}"
                        post_dir.mkdir(parents=True, exist_ok=True)
                        storage_path = post_dir / f"{uuid4().hex}{extension}"
                        storage_path.write_bytes(draft["image_bytes"])
                        store.save_creative_asset(
                            campaign["id"],
                            calendar["id"],
                            calendar["content_hash"],
                            post_number,
                            file_name=f"gemini_post_{post_number}_creative{extension}",
                            mime_type=draft["mime_type"],
                            storage_path=str(storage_path),
                            file_sha256=hashlib.sha256(draft["image_bytes"]).hexdigest(),
                            file_size=len(draft["image_bytes"]),
                            source_type="ai_generated",
                            design_prompt=draft["prompt"],
                        )
                    except (OSError, PERSISTENCE_EXCEPTIONS, TypeError, ValueError) as error:
                        if storage_path is not None:
                            storage_path.unlink(missing_ok=True)
                        st.error(f"Generated creative could not be saved safely: {error}")
                    else:
                        st.session_state.pop(draft_key, None)
                        st.success(
                            "Gemini creative saved as a new immutable version. Send it for Senior Design Review."
                        )
                        st.rerun()

    if latest_asset:
'''
    text = replace_once(text, latest_anchor, gemini_studio, "Gemini Creative Studio")

    upload_label_anchor = '''    upload_label = (
        "Upload Replacement Creative (new version)" if latest_asset else "Upload Creative"
    )
'''
    upload_label_new = '''    if latest_asset and latest_asset.get("latest_decision") == "approved":
        st.warning(
            "Uploading a new creative after approval creates a new latest version and re-locks publishing until that version is approved."
        )
        upload_label = "Upload New Creative (reopens Senior design approval)"
    elif latest_asset:
        upload_label = "Upload Replacement Creative (new version)"
    else:
        upload_label = "Upload Creative"
'''
    text = replace_once(text, upload_label_anchor, upload_label_new, "approved creative upload warning")

    APP_PATH.write_text(text, encoding="utf-8")
    print("added Client Brand Kit and in-app Gemini Creative Studio")


if __name__ == "__main__":
    main()
