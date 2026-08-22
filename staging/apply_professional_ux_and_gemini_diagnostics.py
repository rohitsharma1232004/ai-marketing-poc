"""Final professional UX pass for Brand Kit and Gemini creative diagnostics.

Run after Brand Kit/Gemini Creative Studio and Meta publishing transforms:
    python staging/apply_professional_ux_and_gemini_diagnostics.py

This patch is intentionally limited to presentation/diagnostics. It does not
change approval hashes, content generation routing, publishing eligibility, or
stored campaign data.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"
GEMINI_PATH = ROOT / "gemini_api.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text and old not in text:
        print(f"{label}: already applied")
        return text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    print(f"{label}: applied")
    return text.replace(old, new, 1)


def patch_gemini_diagnostics() -> None:
    text = GEMINI_PATH.read_text(encoding="utf-8")
    if "GEMINI_BILLING_REQUIRED" in text and "_provider_error_details" in text:
        print("gemini_api.py professional diagnostics already applied")
        return

    helper = r'''

def _provider_error_details(response: Any, *, api_key: str = "") -> tuple[str, str]:
    """Extract a bounded, safe provider code/message from Gemini error JSON."""

    try:
        payload = response.json()
    except (TypeError, ValueError):
        return "", ""
    if not isinstance(payload, Mapping):
        return "", ""
    raw_error = payload.get("error")
    if not isinstance(raw_error, Mapping):
        return "", ""

    provider_code = str(raw_error.get("code") or raw_error.get("status") or "").strip()
    provider_message = re.sub(
        r"\s+", " ", str(raw_error.get("message") or "")
    ).strip()
    if api_key and api_key in provider_message:
        provider_message = provider_message.replace(api_key, "[redacted]")
    if len(provider_message) > 600:
        provider_message = provider_message[:597] + "..."
    return provider_code, provider_message


def _provider_suffix(provider_message: str) -> str:
    return f" Google response: {provider_message}" if provider_message else ""

'''
    text = replace_once(
        text,
        "\ndef _post_interaction(\n",
        helper + "\ndef _post_interaction(\n",
        "Gemini provider error helpers",
    )

    old_status = '''    status_code = int(getattr(response, "status_code", 0))
    if status_code in {401, 403}:
        raise GeminiAPIError(
            "The configured Gemini API key is invalid or unauthorized.",
            request_id=request_id,
            code="GEMINI_AUTH_ERROR",
        )
    if status_code == 429:
        raise GeminiAPIError(
            "Gemini API quota or rate limit reached. Check Google AI Studio billing/quota.",
            request_id=request_id,
            code="GEMINI_RATE_LIMIT",
            retryable=True,
        )
    if status_code < 200 or status_code >= 300:
        raise GeminiAPIError(
            "Gemini returned an upstream error.",
            request_id=request_id,
            code="GEMINI_UPSTREAM_ERROR",
            retryable=status_code >= 500,
        )
    try:
        data = response.json()
'''
    new_status = '''    status_code = int(getattr(response, "status_code", 0))
    provider_code, provider_message = _provider_error_details(response, api_key=key)
    normalized_provider_code = provider_code.strip().casefold().replace("-", "_")
    normalized_provider_message = provider_message.casefold()
    suffix = _provider_suffix(provider_message)

    billing_blocked = (
        normalized_provider_code == "failed_precondition"
        or "billing" in normalized_provider_message
        or "paid plan" in normalized_provider_message
        or "payment" in normalized_provider_message
    )
    if status_code >= 400 and billing_blocked:
        raise GeminiAPIError(
            "Gemini cannot run this request because billing or another required project "
            "prerequisite is not enabled." + suffix,
            request_id=request_id,
            code="GEMINI_BILLING_REQUIRED",
        )
    if status_code in {401, 403}:
        raise GeminiAPIError(
            "The configured Gemini API key is invalid or unauthorized." + suffix,
            request_id=request_id,
            code="GEMINI_AUTH_ERROR",
        )
    if status_code == 429:
        raise GeminiAPIError(
            "Gemini API quota or rate limit was reached." + suffix,
            request_id=request_id,
            code="GEMINI_RATE_LIMIT",
            retryable=True,
        )
    if status_code == 404 or (
        "model" in normalized_provider_message
        and ("not found" in normalized_provider_message or "not available" in normalized_provider_message)
    ):
        raise GeminiAPIError(
            "The selected Gemini model is not available for this project or endpoint." + suffix,
            request_id=request_id,
            code="GEMINI_MODEL_UNAVAILABLE",
        )
    if status_code == 400:
        raise GeminiAPIError(
            "Gemini rejected the request parameters." + suffix,
            request_id=request_id,
            code="GEMINI_INVALID_REQUEST",
        )
    if status_code < 200 or status_code >= 300:
        raise GeminiAPIError(
            "Gemini returned an upstream error." + suffix,
            request_id=request_id,
            code="GEMINI_UPSTREAM_ERROR",
            retryable=status_code >= 500,
        )
    try:
        data = response.json()
'''
    text = replace_once(text, old_status, new_status, "Gemini human-readable API diagnostics")
    GEMINI_PATH.write_text(text, encoding="utf-8")
    print("Gemini diagnostics improved.")


def patch_app_ux() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    if "Professional Brand Kit UX" in text and "Technical details" in text:
        print("app.py professional UX already applied")
        return
    if "def render_brand_kit_editor(" not in text or "AI Creative Studio (Gemini)" not in text:
        raise RuntimeError("Apply Brand Kit + Gemini Creative Studio before this UX pass.")

    old_brand_open = '''    with st.expander("Client Brand Kit", expanded=not bool(current_record)):
        st.caption(
            "Save the client's visual identity once. Design Briefs and Gemini creative prompts "
            "will reuse it without changing approved marketing content."
        )
        if current_record:
            st.success(f"Brand Kit v{current_record['version']} is active.")
'''
    new_brand_open = '''    # Professional Brand Kit UX: compact status first; editor opens only on demand.
    st.markdown("#### Brand Kit")
    editor_key = f"brand_kit_editor_open_{client['id']}"
    editor_open = bool(st.session_state.get(editor_key, False))
    if current_record:
        status_cols = st.columns(3)
        status_cols[0].metric("Status", "Configured")
        status_cols[1].metric("Version", f"v{current_record['version']}")
        status_cols[2].metric(
            "Logo", "Added" if defaults.get("logo_storage_path") else "Not added"
        )
        summary_bits = []
        if defaults.get("primary_color"):
            summary_bits.append(f"Primary {defaults['primary_color']}")
        if defaults.get("visual_style"):
            summary_bits.append(defaults["visual_style"][:120])
        st.caption(
            "Saved once per client and reused across future campaigns."
            + ("  •  " + "  •  ".join(summary_bits) if summary_bits else "")
        )
        toggle_label = "Close Brand Kit Editor" if editor_open else "View / Edit Brand Kit"
    else:
        st.info(
            "Brand Kit is not configured yet. Creative generation can continue, but adding "
            "the client's logo, colors and visual direction improves brand consistency."
        )
        toggle_label = "Close Brand Kit Setup" if editor_open else "Set Up Brand Kit"

    if st.button(
        toggle_label,
        key=f"brand_kit_toggle_{client['id']}",
        use_container_width=False,
    ):
        st.session_state[editor_key] = not editor_open
        st.rerun()

    if st.session_state.get(editor_key, False):
        st.caption(
            "Brand identity is separate from approved marketing content. Saving a Brand Kit "
            "changes visual guidance only."
        )
'''
    text = replace_once(text, old_brand_open, new_brand_open, "compact Brand Kit status/editor")

    old_saved = '''            else:
                st.success("Brand Kit saved. New creative prompts will use this version.")
                st.rerun()
'''
    new_saved = '''            else:
                st.session_state[editor_key] = False
                st.success("Brand Kit saved. New creative prompts will use this version.")
                st.rerun()
'''
    text = replace_once(text, old_saved, new_saved, "close Brand Kit editor after save")

    old_studio = '''    with st.expander("AI Creative Studio (Gemini)", expanded=False):
        if latest_asset and latest_asset.get("latest_decision") == "approved":
'''
    new_studio = '''    with st.expander("AI Creative Studio", expanded=False):
        st.caption(
            "Provider: Gemini • Uses the Senior-approved content, Design Brief and the latest "
            "saved Brand Kit. Approved copy, CTA, platform and format are not changed here."
        )
        if not brand_kit:
            st.info(
                "Brand Kit is not configured for this client. You can still generate a "
                "creative, or configure the Brand Kit above for stronger consistency."
            )
        if latest_asset and latest_asset.get("latest_decision") == "approved":
'''
    text = replace_once(text, old_studio, new_studio, "professional Creative Studio header")

    text = text.replace('"Generate Revised Creative with Gemini"', '"Generate Revised Creative"')
    text = text.replace('"Generate Creative with Gemini"', '"Generate Creative"')
    text = text.replace('"Save Gemini Creative as New Version"', '"Save as Creative Version"')

    old_error = '''                        except (GeminiAPIError, TypeError, ValueError) as error:
                            request_suffix = (
                                f" Request ID: {error.request_id}"
                                if isinstance(error, GeminiAPIError)
                                else ""
                            )
                            st.error(f"Gemini creative generation failed: {error}.{request_suffix}")
'''
    new_error = '''                        except (GeminiAPIError, TypeError, ValueError) as error:
                            if isinstance(error, GeminiAPIError):
                                st.error(f"Creative generation could not complete: {error}")
                                if error.code == "GEMINI_BILLING_REQUIRED":
                                    st.warning(
                                        "Google reports that billing or another project prerequisite "
                                        "is required for this request. Check the Gemini project in "
                                        "Google AI Studio / Google Cloud Billing. You can continue "
                                        "the workflow with Manual Upload without changing approvals."
                                    )
                                elif error.code == "GEMINI_RATE_LIMIT":
                                    st.info(
                                        "The Gemini quota/rate limit is temporarily exhausted. Retry "
                                        "later or use Manual Upload for this creative."
                                    )
                                elif error.code == "GEMINI_AUTH_ERROR":
                                    st.warning(
                                        "Check GEMINI_API_KEY in local/deployment secrets. Do not paste "
                                        "the key into the app form or commit it to GitHub."
                                    )
                                elif error.code == "GEMINI_MODEL_UNAVAILABLE":
                                    st.info(
                                        "Choose a model available to this Gemini project or verify that "
                                        "the project has access to image generation."
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
                                st.error(f"Creative generation could not complete: {error}")
'''
    text = replace_once(text, old_error, new_error, "Gemini user-facing diagnostic UX")

    APP_PATH.write_text(text, encoding="utf-8")
    print("Professional Brand Kit and Creative Studio UX applied.")


def main() -> None:
    patch_gemini_diagnostics()
    patch_app_ux()
    print("Professional UX + Gemini diagnostics complete.")


if __name__ == "__main__":
    main()
