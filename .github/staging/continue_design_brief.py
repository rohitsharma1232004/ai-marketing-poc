from pathlib import Path


def replace_once(path, old, new):
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one match in {path}, found {count}: {old[:80]!r}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_section(path, start_marker, end_marker, replacement):
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    start = text.find(start_marker)
    end = text.find(end_marker, start + len(start_marker))
    if start < 0 or end < 0:
        raise RuntimeError(f"Could not locate section in {path}: {start_marker!r}")
    updated = text[:start] + replacement + text[end:]
    file_path.write_text(updated, encoding="utf-8")


new_render = '''def render_calendar_markdown(
    headers,
    rows,
    *,
    content_status=None,
    design_status_by_post=None,
    design_status_default=DESIGN_STATUS_NOT_GENERATED,
):
    """Render content plus optional app-controlled workflow status columns."""
    display_rows = (
        apply_content_status(
            headers, rows, content_status, week_heading_prefix=WEEK_HEADING_PREFIX
        )
        if content_status
        else [list(map(str, row)) for row in rows]
    )
    display_headers = list(headers)
    include_design_status = design_status_by_post is not None
    if include_design_status:
        display_headers.append("Design Status")

    header_line = "| " + " | ".join(display_headers) + " |"
    separator_line = "| " + " | ".join(["---"] * len(display_headers)) + " |"
    lines = []
    post_number = 0

    for row in display_rows:
        if is_week_heading(row):
            if lines:
                lines.append("")
            lines.append(f"## {row[0][len(WEEK_HEADING_PREFIX):]}")
            lines.append("")
            lines.extend([header_line, separator_line])
        else:
            post_number += 1
            output_row = list(row)
            if include_design_status:
                output_row.append(
                    str(
                        design_status_by_post.get(
                            post_number, design_status_default
                        )
                    )
                )
            lines.append("| " + " | ".join(output_row) + " |")

    return "\\n".join(lines).strip()


'''
replace_section(
    "app.py",
    "def render_calendar_markdown(",
    "def load_campaign_into_session(",
    new_render,
)

replace_once(
    "app.py",
    '''    campaign_record = None
    latest_calendar = None
    client_record = None
''',
    '''    campaign_record = None
    latest_calendar = None
    client_record = None
    design_briefs = []
''',
)

replace_once(
    "app.py",
    '''    if client_record is not None:
        st.caption(f"Client: {client_record['name']}")
''',
    '''    if campaign_store is not None and campaign_id and latest_calendar is not None:
        try:
            design_briefs = campaign_store.list_design_briefs(
                campaign_id, latest_calendar["id"]
            )
        except PERSISTENCE_EXCEPTIONS as design_load_error:
            st.warning(f"Design brief status could not be loaded: {design_load_error}")
            design_briefs = []

    if client_record is not None:
        st.caption(f"Client: {client_record['name']}")
''',
)

replace_once(
    "app.py",
    '''            display_result = render_calendar_markdown(
                latest_calendar["headers"],
                latest_calendar["rows"],
                content_status=status_override,
            )
''',
    '''            final_approval_state = (
                campaign_record is not None
                and campaign_record.get("status") in {"fully_approved", "approved"}
            )
            design_status_by_post = {
                int(item["post_number"]): DESIGN_STATUS_BRIEF_READY
                for item in design_briefs
            }
            design_status_default = (
                DESIGN_STATUS_NOT_GENERATED
                if final_approval_state
                else DESIGN_STATUS_LOCKED
            )
            display_result = render_calendar_markdown(
                latest_calendar["headers"],
                latest_calendar["rows"],
                content_status=status_override,
                design_status_by_post=design_status_by_post,
                design_status_default=design_status_default,
            )
''',
)

design_ui = '''        st.markdown("### Design Brief Generator")
        st.caption(
            "Design briefs are generated only from this exact Senior-approved content "
            "version. Approved content fields remain immutable."
        )

        if design_briefs:
            st.success(
                f"Design briefs ready for {len(design_briefs)} approved post(s)."
            )
            for record in design_briefs:
                brief = record["brief"]
                with st.expander(
                    f"Post {record['post_number']} — {record['format']} — View Design Brief"
                ):
                    for section_label, section_value in display_design_brief_sections(
                        brief
                    ):
                        st.markdown(f"**{section_label}**")
                        if isinstance(section_value, list):
                            for item_index, item in enumerate(section_value, start=1):
                                st.write(f"{item_index}. {item}")
                        else:
                            st.write(section_value)
        elif campaign_store is not None and latest_calendar is not None:
            if st.button(
                "Generate Design Briefs",
                use_container_width=True,
                key=f"generate_design_briefs_{latest_calendar['id']}",
            ):
                brief_provider = get_app_setting(
                    "CALENDAR_GENERATION_PROVIDER",
                    DEFAULT_CALENDAR_GENERATION_PROVIDER,
                ).strip().lower()
                brief_groq_key = get_app_setting("GROQ_API_KEY")
                brief_groq_url = get_app_setting(
                    "GROQ_API_URL", DEFAULT_GROQ_API_URL
                )
                brief_model = get_app_setting("GROQ_MODEL", DEFAULT_GROQ_MODEL)
                brief_n8n_url = get_app_setting("N8N_CALENDAR_WEBHOOK_URL")
                brief_n8n_secret = get_app_setting("N8N_WEBHOOK_SECRET")

                brief_config_error = None
                if brief_provider not in {"groq", "n8n"}:
                    brief_config_error = (
                        "CALENDAR_GENERATION_PROVIDER must be either 'groq' or 'n8n'."
                    )
                elif brief_provider == "groq" and not brief_groq_key:
                    brief_config_error = "GROQ_API_KEY is missing."
                elif brief_provider == "n8n" and not brief_n8n_url:
                    brief_config_error = "N8N_CALENDAR_WEBHOOK_URL is missing."
                elif brief_provider == "n8n" and not brief_n8n_secret:
                    brief_config_error = "N8N_WEBHOOK_SECRET is missing."

                if brief_config_error:
                    st.error(brief_config_error)
                else:
                    brief_request_id = str(uuid4())
                    brief_label = "n8n" if brief_provider == "n8n" else "Groq"
                    try:
                        brief_prompt, source_posts = build_design_brief_prompt(
                            latest_calendar["headers"],
                            latest_calendar["rows"],
                            week_heading_prefix=WEEK_HEADING_PREFIX,
                            client_metadata=latest_calendar.get("client_metadata"),
                            campaign_intake=(campaign_record or {}).get("intake", {}),
                        )
                    except PERSISTENCE_EXCEPTIONS as brief_prompt_error:
                        st.error(f"Design brief request could not be prepared: {brief_prompt_error}")
                    else:
                        with st.spinner(
                            f"Generating {len(source_posts)} design brief(s) through "
                            f"{brief_label} ({brief_model})..."
                        ):
                            try:
                                brief_result = generate_calendar_content(
                                    provider=brief_provider,
                                    system_prompt=DESIGN_BRIEF_SYSTEM_PROMPT,
                                    user_prompt=brief_prompt,
                                    model=brief_model,
                                    expected_posts=len(source_posts),
                                    groq_api_key=brief_groq_key,
                                    groq_api_url=brief_groq_url,
                                    n8n_webhook_url=brief_n8n_url,
                                    n8n_webhook_secret=brief_n8n_secret,
                                    campaign_id=campaign_id,
                                    request_id=brief_request_id,
                                )
                                parsed_briefs = parse_design_brief_response(
                                    brief_result.content,
                                    source_posts=source_posts,
                                )
                                campaign_store.save_design_briefs(
                                    campaign_id,
                                    latest_calendar["id"],
                                    latest_calendar["content_hash"],
                                    parsed_briefs,
                                    generation_metadata={
                                        "request_id": brief_result.request_id,
                                        "provider": brief_result.provider,
                                        "model": brief_result.model,
                                        "finish_reason": brief_result.finish_reason,
                                        "usage": dict(brief_result.usage or {}),
                                    },
                                )
                            except GenerationProviderError as brief_provider_error:
                                st.error(
                                    "Design briefs could not be generated: "
                                    f"{brief_provider_error} Request ID: "
                                    f"{brief_provider_error.request_id}"
                                )
                            except PERSISTENCE_EXCEPTIONS as brief_error:
                                st.error(f"Design briefs could not be saved safely: {brief_error}")
                            else:
                                st.success(
                                    "Design briefs generated from the approved content package."
                                )
                                st.rerun()

'''
replace_once(
    "app.py",
    '    elif senior_rejection is not None:\n',
    design_ui + '    elif senior_rejection is not None:\n',
)

replace_once(
    "APPROVAL_WORKFLOW.md",
    '       -> Approve -> Excel download unlocked\n',
    '       -> Approve -> Excel download + Design Brief Generator unlocked\n',
)
replace_once(
    "APPROVAL_WORKFLOW.md",
    '- Excel export is allowed only when the latest version has a matching Senior `approved` decision.\n',
    '- Excel export is allowed only when the latest version has a matching Senior `approved` decision.\n'
    '- Design briefs can be generated only for the latest hash-matched, finally Senior-approved content version.\n'
    '- Design briefs are stored separately from approved content, so creative instructions cannot rewrite the approved package.\n'
    '- The dashboard derives `Design Status` as Locked, Not Generated, or Design Brief Ready without changing the approved content hash.\n',
)
replace_once(
    "DEVELOPMENT_ROADMAP.md",
    '## Milestone 4 — complete content package\n\nStatus: planned\n',
    '## Milestone 4 — complete content package\n\nStatus: **in progress — captions/reel scripts complete; Design Brief Generator implemented for local validation**\n',
)
replace_once(
    "DEVELOPMENT_ROADMAP.md",
    '- Design briefs and platform variants.\n',
    '- Format-aware Design Brief Generator for Image, Carousel, Reel, Video, and Story posts after final Senior approval.\n'
    '- Design briefs stay version/hash-bound and separate from immutable approved content; the UI shows per-post expanders and derived Design Status.\n'
    '- Platform variants.\n',
)

print("Design Brief continuation transformation applied successfully.")
