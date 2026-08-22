"""Add Cloudflare Workers AI as the free-first Creative Studio provider.

Run after the professional Brand Kit / Gemini UX and JPEG fixes have been
applied. Gemini remains available and Manual Upload remains unchanged.

The transformation replaces only the in-app AI Creative Studio block. It does
not change content approval hashes, Design Briefs, Senior Design Approval, or
publishing eligibility.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"


def main() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    if "Cloudflare Workers AI (Free)" in text and "generate_cloudflare_image" in text:
        print("Cloudflare Creative Studio provider already applied")
        return
    if 'with st.expander("AI Creative Studio", expanded=False):' not in text:
        raise RuntimeError(
            "Professional AI Creative Studio must be applied before Cloudflare provider patch."
        )
    if "source_provider=\"gemini\"" not in text:
        raise RuntimeError(
            "Creative provenance v10 must be applied before Cloudflare provider patch."
        )

    import_anchor = "from gemini_api import (\n"
    if text.count(import_anchor) != 1:
        raise RuntimeError(
            f"Cloudflare import anchor: expected one match, found {text.count(import_anchor)}"
        )
    cloudflare_imports = '''from cloudflare_images import (
    DEFAULT_CLOUDFLARE_IMAGE_MODEL,
    CloudflareImageError,
    generate_image as generate_cloudflare_image,
)
'''
    text = text.replace(import_anchor, cloudflare_imports + import_anchor, 1)

    start_marker = '    with st.expander("AI Creative Studio", expanded=False):\n'
    end_marker = "    if latest_asset:\n"
    start = text.find(start_marker)
    if start < 0:
        raise RuntimeError("Cloudflare Studio start marker was not found.")
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        raise RuntimeError("Cloudflare Studio end marker was not found.")

    studio = r'''    with st.expander("AI Creative Studio", expanded=False):
        st.caption(
            "Choose an image provider. Cloudflare Workers AI is the free-first default; "
            "Gemini remains available as an optional provider. Both use the same Senior-approved "
            "content, Design Brief and latest Brand Kit. Manual Upload remains available below."
        )
        if not brand_kit:
            st.info(
                "Brand Kit is not configured for this client. You can still generate a creative, "
                "or configure the Brand Kit above for stronger consistency."
            )
        if latest_asset and latest_asset.get("latest_decision") == "approved":
            st.info(
                "The latest creative is already Senior Design Approved. Creating a new version "
                "will reopen design review, so AI generation is disabled here unless a revised version is required."
            )
        else:
            provider_options = ("Cloudflare Workers AI (Free)", "Gemini")
            default_provider = str(
                get_app_setting("DEFAULT_CREATIVE_PROVIDER", "cloudflare") or "cloudflare"
            ).strip().casefold()
            provider_index = 1 if default_provider == "gemini" else 0
            creative_provider = st.selectbox(
                "Creative Provider",
                provider_options,
                index=provider_index,
                key=f"creative_provider_{calendar['id']}_{post_number}",
            )
            using_cloudflare = creative_provider.startswith("Cloudflare")

            recommended_ratio = recommended_aspect_ratio(
                brief_record.get("format", ""),
                approved_post.get("content", {}).get("Platform", ""),
            )
            ratio_options = list(SUPPORTED_IMAGE_ASPECT_RATIOS)
            creative_ratio = st.selectbox(
                "Aspect Ratio",
                ratio_options,
                index=ratio_options.index(recommended_ratio) if recommended_ratio in ratio_options else 0,
                key=f"creative_ratio_{calendar['id']}_{post_number}",
            )

            if using_cloudflare:
                cloudflare_model = str(
                    get_app_setting(
                        "CLOUDFLARE_IMAGE_MODEL", DEFAULT_CLOUDFLARE_IMAGE_MODEL
                    )
                    or DEFAULT_CLOUDFLARE_IMAGE_MODEL
                ).strip()
                st.caption(
                    "Cloudflare model: FLUX.1 Schnell • Free allocation resets daily. "
                    "The selected aspect ratio is added as composition guidance because this "
                    "model's REST schema does not expose width/height controls."
                )
                cloudflare_steps = st.slider(
                    "Quality Steps",
                    min_value=1,
                    max_value=8,
                    value=4,
                    key=f"cloudflare_steps_{calendar['id']}_{post_number}",
                    help="Higher values may improve detail but use more Workers AI compute.",
                )
                gemini_model = DEFAULT_GEMINI_IMAGE_MODEL
                gemini_size = "1K"
            else:
                default_model = get_app_setting(
                    "GEMINI_IMAGE_MODEL", DEFAULT_GEMINI_IMAGE_MODEL
                )
                model_options = list(SUPPORTED_GEMINI_IMAGE_MODELS)
                if default_model not in model_options:
                    default_model = DEFAULT_GEMINI_IMAGE_MODEL
                gemini_model = st.selectbox(
                    "Gemini Image Model",
                    model_options,
                    index=model_options.index(default_model),
                    key=f"gemini_image_model_{calendar['id']}_{post_number}",
                )
                size_options = (
                    ["1K"]
                    if gemini_model == "gemini-3.1-flash-lite-image"
                    else ["1K", "2K", "4K"]
                )
                gemini_size = st.selectbox(
                    "Image Size",
                    size_options,
                    index=0,
                    key=f"gemini_size_{calendar['id']}_{post_number}_{gemini_model}",
                )
                cloudflare_model = DEFAULT_CLOUDFLARE_IMAGE_MODEL
                cloudflare_steps = 4

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

            if is_revision and using_cloudflare:
                st.caption(
                    "Cloudflare revisions regenerate from the approved prompt plus Senior feedback. "
                    "Gemini can also use the previous image as a visual reference when that project has image access."
                )

            provider_slug = "cloudflare" if using_cloudflare else "gemini"
            editable_prompt = st.text_area(
                "Creative Prompt",
                value=generation_prompt,
                height=320,
                key=(
                    f"creative_prompt_{provider_slug}_{calendar['id']}_{post_number}_"
                    f"{latest_asset['id'] if latest_asset else 'new'}"
                ),
                help=(
                    "You may refine visual direction, but do not change approved claims, CTA, "
                    "platform, format, or Senior-approved content."
                ),
            )
            generate_label = (
                "Generate Revised Creative" if is_revision else "Generate Creative"
            )
            draft_key = (
                f"creative_draft_{provider_slug}_{calendar['id']}_{post_number}_"
                f"{latest_asset['id'] if latest_asset else 'new'}"
            )

            if st.button(
                generate_label,
                key=f"generate_{draft_key}",
                use_container_width=True,
            ):
                generated = None
                provider_metadata = {}
                if using_cloudflare:
                    cloudflare_account_id = get_app_setting("CLOUDFLARE_ACCOUNT_ID")
                    cloudflare_token = get_app_setting("CLOUDFLARE_API_TOKEN")
                    missing_cloudflare = []
                    if not cloudflare_account_id:
                        missing_cloudflare.append("CLOUDFLARE_ACCOUNT_ID")
                    if not cloudflare_token:
                        missing_cloudflare.append("CLOUDFLARE_API_TOKEN")
                    if missing_cloudflare:
                        st.error(
                            "Cloudflare creative generation is not configured. Add "
                            + " and ".join(missing_cloudflare)
                            + " to local/deployment secrets."
                        )
                        st.caption(
                            "Create a Workers AI API token in Cloudflare with Workers AI access. "
                            "Do not paste the token into the app or commit it to GitHub."
                        )
                    else:
                        with st.spinner(
                            "Cloudflare is generating a revised creative..."
                            if is_revision
                            else "Cloudflare is generating the creative..."
                        ):
                            try:
                                generated = generate_cloudflare_image(
                                    prompt=editable_prompt,
                                    account_id=cloudflare_account_id,
                                    api_token=cloudflare_token,
                                    model=cloudflare_model,
                                    aspect_ratio=creative_ratio,
                                    steps=cloudflare_steps,
                                )
                            except (CloudflareImageError, TypeError, ValueError) as error:
                                if isinstance(error, CloudflareImageError):
                                    st.error(
                                        f"Creative generation could not complete: {error}"
                                    )
                                    if error.code == "CLOUDFLARE_RATE_LIMIT":
                                        st.info(
                                            "The Cloudflare free allocation/rate limit may be exhausted. "
                                            "Retry after the allocation resets, switch to Gemini if enabled, "
                                            "or continue with Manual Upload."
                                        )
                                    elif error.code == "CLOUDFLARE_AUTH_ERROR":
                                        st.warning(
                                            "Check CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN. "
                                            "The token should have Workers AI access; never commit it to GitHub."
                                        )
                                    elif error.code == "CLOUDFLARE_INVALID_REQUEST":
                                        st.info(
                                            "Cloudflare rejected a request parameter. The provider detail "
                                            "above should identify the unsupported value."
                                        )
                                    with st.expander("Technical details", expanded=False):
                                        st.code(
                                            f"Error code: {error.code}\nRequest ID: {error.request_id}",
                                            language="text",
                                        )
                                else:
                                    st.error(
                                        f"Creative generation could not complete: {error}"
                                    )
                            else:
                                provider_metadata = {
                                    "requested_aspect_ratio": creative_ratio,
                                    "steps": generated.steps,
                                    "seed": generated.seed,
                                    "actual_width": generated.width,
                                    "actual_height": generated.height,
                                    "prompt_compacted": generated.prompt_compacted,
                                    "provider_prompt_chars": generated.provider_prompt_chars,
                                }
                else:
                    gemini_key = get_app_setting("GEMINI_API_KEY")
                    if not gemini_key:
                        st.error(
                            "GEMINI_API_KEY is missing. Add a Gemini Developer API key to the server configuration."
                        )
                    else:
                        reference_bytes = None
                        reference_mime = ""
                        if (
                            is_revision
                            and latest_asset
                            and str(latest_asset.get("mime_type") or "").startswith("image/")
                        ):
                            ok, _path, raw_reference, _error = verify_creative_file_integrity(
                                latest_asset
                            )
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
                                    aspect_ratio=creative_ratio,
                                    image_size=gemini_size,
                                    reference_image_bytes=reference_bytes,
                                    reference_image_mime_type=reference_mime,
                                    api_url=get_app_setting(
                                        "GEMINI_INTERACTIONS_URL",
                                        "https://generativelanguage.googleapis.com/v1beta/interactions",
                                    ),
                                )
                            except (GeminiAPIError, TypeError, ValueError) as error:
                                if isinstance(error, GeminiAPIError):
                                    st.error(
                                        f"Creative generation could not complete: {error}"
                                    )
                                    if error.code == "GEMINI_BILLING_REQUIRED":
                                        st.warning(
                                            "Google reports that billing or another project prerequisite "
                                            "is required for this request. Switch to Cloudflare Workers AI "
                                            "or continue with Manual Upload without changing approvals."
                                        )
                                    elif error.code == "GEMINI_RATE_LIMIT":
                                        st.info(
                                            "The Gemini quota/rate limit is temporarily exhausted. Retry "
                                            "later, switch to Cloudflare, or use Manual Upload."
                                        )
                                    elif error.code == "GEMINI_AUTH_ERROR":
                                        st.warning(
                                            "Check GEMINI_API_KEY in local/deployment secrets. Do not paste "
                                            "the key into the app form or commit it to GitHub."
                                        )
                                    elif error.code == "GEMINI_MODEL_UNAVAILABLE":
                                        st.info(
                                            "Choose a model available to this Gemini project, switch to "
                                            "Cloudflare, or verify Gemini image access."
                                        )
                                    elif error.code == "GEMINI_INVALID_REQUEST":
                                        st.info(
                                            "Google rejected one or more request parameters. The provider "
                                            "detail above should identify the unsupported field or value."
                                        )
                                    with st.expander("Technical details", expanded=False):
                                        st.code(
                                            f"Error code: {error.code}\nRequest ID: {error.request_id}",
                                            language="text",
                                        )
                                else:
                                    st.error(
                                        f"Creative generation could not complete: {error}"
                                    )
                            else:
                                provider_metadata = {
                                    "aspect_ratio": generated.aspect_ratio,
                                    "image_size": generated.image_size,
                                    "reference_image_used": bool(reference_bytes),
                                }

                if generated is not None:
                    st.session_state[draft_key] = {
                        "image_bytes": generated.image_bytes,
                        "mime_type": generated.mime_type,
                        "prompt": editable_prompt,
                        "provider": provider_slug,
                        "model": generated.model,
                        "request_id": generated.request_id,
                        "aspect_ratio": generated.aspect_ratio,
                        "image_size": generated.image_size,
                        "source_metadata": provider_metadata,
                    }
                    st.rerun()

            draft = st.session_state.get(draft_key)
            if draft:
                st.markdown("**Generated Creative Preview**")
                st.image(draft["image_bytes"])
                provider_label = (
                    "Cloudflare Workers AI"
                    if draft["provider"] == "cloudflare"
                    else "Gemini"
                )
                st.caption(
                    f"{provider_label} • {draft['model']} | {draft['aspect_ratio']} | "
                    f"{draft['image_size']} | Request ID: {draft['request_id']}"
                )
                if draft.get("source_metadata", {}).get("prompt_compacted"):
                    st.info(
                        "Cloudflare accepts prompts up to 2,048 characters. The provider request "
                        "was safely compacted while preserving the opening concept and ending "
                        "brand/constraint instructions. The full editable prompt remains stored "
                        "with the creative version."
                    )
                st.caption(
                    "Preview first. Save only the version you want to send through the existing Senior Design Review workflow."
                )
                if st.button(
                    "Save as Creative Version",
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
                            file_name=(
                                f"{draft['provider']}_post_{post_number}_creative{extension}"
                            ),
                            mime_type=draft["mime_type"],
                            storage_path=str(storage_path),
                            file_sha256=hashlib.sha256(draft["image_bytes"]).hexdigest(),
                            file_size=len(draft["image_bytes"]),
                            source_type="ai_generated",
                            design_prompt=draft["prompt"],
                            source_provider=draft["provider"],
                            source_model=draft["model"],
                            source_request_id=draft["request_id"],
                            source_metadata=draft.get("source_metadata") or {},
                        )
                    except (OSError, PERSISTENCE_EXCEPTIONS, TypeError, ValueError) as error:
                        if storage_path is not None:
                            storage_path.unlink(missing_ok=True)
                        st.error(f"Generated creative could not be saved safely: {error}")
                    else:
                        st.session_state.pop(draft_key, None)
                        st.success(
                            "Creative saved as a new immutable version. Send it for Senior Design Review."
                        )
                        st.rerun()

'''

    text = text[:start] + studio + text[end:]
    APP_PATH.write_text(text, encoding="utf-8")
    print("Cloudflare free-first + Gemini optional Creative Studio applied")


if __name__ == "__main__":
    main()
