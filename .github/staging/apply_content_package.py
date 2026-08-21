from pathlib import Path


def replace_once(path, old, new):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one match, found {count}\nOLD:\n{old[:500]}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# campaign_store.py: centralize the editable-field contract and enforce
# version/format-aware Senior change requests.
replace_once(
    "campaign_store.py",
    "from uuid import UUID, uuid4\n\n\nSCHEMA_VERSION = 6",
    "from uuid import UUID, uuid4\n\nfrom content_package import (\n"
    "    REEL_SCRIPT_FORMATS,\n"
    "    REVISION_FIELDS,\n"
    "    require_supported_calendar_headers,\n"
    ")\n\n\nSCHEMA_VERSION = 6",
)
replace_once(
    "campaign_store.py",
    'REVISION_SCOPES = frozenset({"specific_post", "whole_calendar"})\nREVISION_FIELDS = frozenset({"Content Idea", "SEO Keyword Focus", "CTA"})\n',
    'REVISION_SCOPES = frozenset({"specific_post", "whole_calendar"})\n',
)
replace_once(
    "campaign_store.py",
    '''            if normalized_change is not None:\n                rows = _deserialize_json(calendar["rows_json"])\n                content_rows = [\n                    (index, row) for index, row in enumerate(rows)\n                    if isinstance(row, list) and len(row) == 7\n                ]\n                if normalized_change["scope"] == "specific_post":\n                    post_number = normalized_change["post_number"]\n                    if post_number > len(content_rows):\n                        raise ValueError("The selected post is outside this calendar version.")\n                    expected_row_index = content_rows[post_number - 1][0]\n                    if normalized_change["row_index"] != expected_row_index:\n                        raise ValueError("The selected post no longer matches this calendar version.")\n''',
    '''            if normalized_change is not None:\n                headers = list(\n                    require_supported_calendar_headers(\n                        _deserialize_json(calendar["headers_json"])\n                    )\n                )\n                rows = _deserialize_json(calendar["rows_json"])\n                unavailable_fields = [\n                    field\n                    for field in normalized_change["fields"]\n                    if field not in headers\n                ]\n                if unavailable_fields:\n                    raise ValueError(\n                        "This calendar version does not contain: "\n                        + ", ".join(unavailable_fields)\n                        + "."\n                    )\n                expected_columns = len(headers)\n                content_rows = [\n                    (index, row) for index, row in enumerate(rows)\n                    if isinstance(row, list) and len(row) == expected_columns\n                ]\n                format_index = headers.index("Format")\n                if normalized_change["scope"] == "specific_post":\n                    post_number = normalized_change["post_number"]\n                    if post_number > len(content_rows):\n                        raise ValueError("The selected post is outside this calendar version.")\n                    expected_row_index, selected_row = content_rows[post_number - 1]\n                    if normalized_change["row_index"] != expected_row_index:\n                        raise ValueError("The selected post no longer matches this calendar version.")\n                    if (\n                        "Reel Script" in normalized_change["fields"]\n                        and str(selected_row[format_index]).strip().casefold()\n                        not in REEL_SCRIPT_FORMATS\n                    ):\n                        raise ValueError(\n                            "Reel Script changes are available only for Reel or Video posts."\n                        )\n                elif "Reel Script" in normalized_change["fields"] and not any(\n                    str(row[format_index]).strip().casefold() in REEL_SCRIPT_FORMATS\n                    for _, row in content_rows\n                ):\n                    raise ValueError(\n                        "This calendar has no Reel or Video posts with a Reel Script to change."\n                    )\n''',
)
replace_once(
    "campaign_store.py",
    '''    fields = [\n        field for field in ("Content Idea", "SEO Keyword Focus", "CTA")\n        if field in requested\n    ]\n''',
    '''    fields = [field for field in REVISION_FIELDS if field in requested]\n''',
)

# Direct Groq and n8n both need enough completion room for captions/scripts.
replace_once(
    "generation_providers.py",
    '        "max_completion_tokens": 4096,\n',
    '        "max_completion_tokens": 8192,\n',
)
replace_once(
    "n8n_workflows/calendar_generate_v1.json",
    "max_completion_tokens: 4096",
    "max_completion_tokens: 8192",
)
replace_once(
    "tests/test_generation_providers.py",
    '''        self.assertFalse(call["json"]["include_reasoning"])\n        self.assertEqual(call["timeout"], (5, 90))\n''',
    '''        self.assertFalse(call["json"]["include_reasoning"])\n        self.assertEqual(call["json"]["max_completion_tokens"], 8192)\n        self.assertEqual(call["timeout"], (5, 90))\n''',
)

# app.py: new content-package contract, format-aware Senior revisions, derived
# statuses, and approved Excel export.
replace_once(
    "app.py",
    '''from campaign_store import CampaignStore, CampaignStoreError\nfrom generation_providers import (\n''',
    '''from campaign_store import CampaignStore, CampaignStoreError\nfrom content_package import (\n    CONTENT_PACKAGE_HEADERS,\n    CONTENT_STATUS_APPROVED,\n    CONTENT_STATUS_NEEDS_CHANGES,\n    CONTENT_STATUS_READY,\n    GENERATION_HEADERS,\n    apply_content_status,\n    normalize_generated_content_row,\n    require_supported_calendar_headers,\n    revision_fields_for_row,\n    revision_fields_for_rows,\n)\nfrom generation_providers import (\n''',
)
replace_once(
    "app.py",
    '''CONTENT_CALENDAR_HEADERS = [\n    "Date",\n    "Platform",\n    "Pillar",\n    "Format",\n    "Content Idea",\n    "SEO Keyword Focus",\n    "CTA",\n]\n''',
    '''CONTENT_CALENDAR_HEADERS = list(CONTENT_PACKAGE_HEADERS)\n''',
)
replace_once(
    "app.py",
    '''    st.caption(\n        f"Client details → {provider_label} → Content Calendar → Senior Approval → Excel Download"\n    )\n    st.info(\n        "Single-approval POC: the generated calendar must be approved by a Senior "\n        "before Excel download is unlocked. Client approval and WhatsApp delivery are disabled."\n    )\n''',
    '''    st.caption(\n        f"Client details → {provider_label} → Content Package → Senior Approval → Excel Download"\n    )\n    st.info(\n        "Single-approval POC: the generated content package must be approved by a Senior "\n        "before Excel download is unlocked. Client approval and WhatsApp delivery are disabled."\n    )\n''',
)
replace_once(
    "app.py",
    '''def parse_markdown_table(text):\n    """Strictly parse the model table instead of silently fixing malformed rows."""\n    expected_headings = [\n        normalized_heading(value) for value in CONTENT_CALENDAR_HEADERS\n    ]\n    data_rows = []\n    table_header_found = False\n\n    for line_number, raw_line in enumerate(text.splitlines(), start=1):\n        line = raw_line.strip()\n        if "|" not in line:\n            continue\n\n        cells = [cell.strip() for cell in line.strip("|").split("|")]\n        if is_markdown_separator(cells):\n            continue\n\n        row_headings = [normalized_heading(value) for value in cells]\n        if row_headings == expected_headings:\n            table_header_found = True\n            continue\n\n        if not table_header_found:\n            continue\n\n        if len(cells) != len(CONTENT_CALENDAR_HEADERS):\n            raise ValueError(\n                f"Calendar row {line_number} has {len(cells)} columns; "\n                f"expected {len(CONTENT_CALENDAR_HEADERS)}."\n            )\n        if row_headings and row_headings[0] == expected_headings[0]:\n            raise ValueError(\n                "The calendar header must exactly match the required seven columns."\n            )\n        if not any(cells):\n            raise ValueError(f"Calendar row {line_number} is empty.")\n        blank_columns = [\n            CONTENT_CALENDAR_HEADERS[index]\n            for index, value in enumerate(cells)\n            if not value.strip()\n        ]\n        if blank_columns:\n            raise ValueError(\n                f"Calendar row {line_number} has blank required cells: "\n                + ", ".join(blank_columns)\n                + "."\n            )\n\n        data_rows.append(cells)\n\n    if not table_header_found:\n        raise ValueError(\n            "The response did not contain the required Markdown table header."\n        )\n    if not data_rows:\n        raise ValueError("The response did not contain any calendar rows.")\n\n    return CONTENT_CALENDAR_HEADERS.copy(), data_rows\n''',
    '''def parse_markdown_table(text, expected_headers=None):\n    """Strictly parse one model table against the caller-selected column contract."""\n    required_headers = list(expected_headers or CONTENT_CALENDAR_HEADERS)\n    expected_headings = [normalized_heading(value) for value in required_headers]\n    data_rows = []\n    table_header_found = False\n\n    for line_number, raw_line in enumerate(text.splitlines(), start=1):\n        line = raw_line.strip()\n        if "|" not in line:\n            continue\n\n        cells = [cell.strip() for cell in line.strip("|").split("|")]\n        if is_markdown_separator(cells):\n            continue\n\n        row_headings = [normalized_heading(value) for value in cells]\n        if row_headings == expected_headings:\n            table_header_found = True\n            continue\n\n        if not table_header_found:\n            continue\n\n        if len(cells) != len(required_headers):\n            raise ValueError(\n                f"Content row {line_number} has {len(cells)} columns; "\n                f"expected {len(required_headers)}."\n            )\n        if row_headings and row_headings[0] == expected_headings[0]:\n            raise ValueError(\n                "The content-package header must exactly match the required columns."\n            )\n        if not any(cells):\n            raise ValueError(f"Content row {line_number} is empty.")\n        blank_columns = [\n            required_headers[index]\n            for index, value in enumerate(cells)\n            if not value.strip()\n        ]\n        if blank_columns:\n            raise ValueError(\n                f"Content row {line_number} has blank required cells: "\n                + ", ".join(blank_columns)\n                + "."\n            )\n\n        data_rows.append(cells)\n\n    if not table_header_found:\n        raise ValueError(\n            "The response did not contain the required Markdown table header."\n        )\n    if not data_rows:\n        raise ValueError("The response did not contain any content rows.")\n\n    return required_headers.copy(), data_rows\n''',
)
replace_once(
    "app.py",
    '''    calendar_rows = []\n    current_week = None\n    for model_row, schedule_item in zip(model_rows, schedule):\n        if len(model_row) != len(CONTENT_CALENDAR_HEADERS):\n            raise ValueError("A generated calendar row does not have seven columns.")\n\n        week_title = schedule_item["week_title"]\n        if week_title != current_week:\n            calendar_rows.append([WEEK_HEADING_PREFIX + week_title])\n            current_week = week_title\n\n        cleaned_row = [\n            re.sub(r"\\s+", " ", str(value)).strip().replace("|", "/")\n            for value in model_row\n        ]\n        cleaned_row[0] = schedule_item["date_label"]\n        calendar_rows.append(cleaned_row)\n\n    return calendar_rows\n\n\ndef render_calendar_markdown(headers, rows):\n    """Render the validated calendar in the weekly format shown in the UI."""\n    header_line = "| " + " | ".join(headers) + " |"\n    separator_line = "| " + " | ".join(["---"] * len(headers)) + " |"\n    lines = []\n\n    for row in rows:\n''',
    '''    calendar_rows = []\n    current_week = None\n    for model_row, schedule_item in zip(model_rows, schedule):\n        if len(model_row) != len(GENERATION_HEADERS):\n            raise ValueError(\n                "A generated content-package row does not have the required columns."\n            )\n\n        week_title = schedule_item["week_title"]\n        if week_title != current_week:\n            calendar_rows.append([WEEK_HEADING_PREFIX + week_title])\n            current_week = week_title\n\n        calendar_rows.append(\n            normalize_generated_content_row(\n                model_row, date_label=schedule_item["date_label"]\n            )\n        )\n\n    return calendar_rows\n\n\ndef render_calendar_markdown(headers, rows, *, content_status=None):\n    """Render the validated calendar in the weekly format shown in the UI."""\n    display_rows = (\n        apply_content_status(\n            headers, rows, content_status, week_heading_prefix=WEEK_HEADING_PREFIX\n        )\n        if content_status\n        else [list(map(str, row)) for row in rows]\n    )\n    header_line = "| " + " | ".join(headers) + " |"\n    separator_line = "| " + " | ".join(["---"] * len(headers)) + " |"\n    lines = []\n\n    for row in display_rows:\n''',
)
replace_once(
    "app.py",
    '''def validate_calendar_for_export(headers, rows, schedule):\n    """Protect approval/export from incomplete or tampered calendar state."""\n    if headers != CONTENT_CALENDAR_HEADERS:\n        raise ValueError("The calendar headers are not valid.")\n\n    data_rows = [row for row in rows if not is_week_heading(row)]\n    if len(data_rows) != len(schedule):\n        raise ValueError("The calendar does not contain the requested number of posts.")\n\n    for index, (row, schedule_item) in enumerate(zip(data_rows, schedule), start=1):\n        if len(row) != len(CONTENT_CALENDAR_HEADERS):\n            raise ValueError(f"Calendar row {index} does not have seven columns.")\n        if any(not str(value).strip() for value in row):\n            raise ValueError(f"Calendar row {index} contains a blank required cell.")\n        if row[0] != schedule_item["date_label"]:\n            raise ValueError("The saved calendar dates do not match the selected start date.")\n\n    return data_rows\n''',
    '''def validate_calendar_for_export(headers, rows, schedule):\n    """Protect approval/export from incomplete or tampered calendar state."""\n    required_headers = list(require_supported_calendar_headers(headers))\n\n    data_rows = [row for row in rows if not is_week_heading(row)]\n    if len(data_rows) != len(schedule):\n        raise ValueError("The calendar does not contain the requested number of posts.")\n\n    for index, (row, schedule_item) in enumerate(zip(data_rows, schedule), start=1):\n        if len(row) != len(required_headers):\n            raise ValueError(f"Calendar row {index} does not match its headers.")\n        if any(not str(value).strip() for value in row):\n            raise ValueError(f"Calendar row {index} contains a blank required cell.")\n        if row[0] != schedule_item["date_label"]:\n            raise ValueError("The saved calendar dates do not match the selected start date.")\n\n    return data_rows\n''',
)
replace_once(
    "app.py",
    '''    path.parent.mkdir(parents=True, exist_ok=True)\n    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:\n''',
    '''    width_by_header = {\n        "Date": 16,\n        "Platform": 22,\n        "Pillar": 22,\n        "Format": 18,\n        "Content Idea": 52,\n        "SEO Keyword Focus": 32,\n        "CTA": 30,\n        "Caption": 70,\n        "Reel Script": 90,\n        "Content Status": 24,\n    }\n    calendar_column_widths = [\n        width_by_header.get(str(header), 24) for header in headers\n    ]\n\n    path.parent.mkdir(parents=True, exist_ok=True)\n    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:\n''',
)
replace_once(
    "app.py",
    '''                cal_rows,\n                [16, 22, 22, 18, 52, 32, 30],\n                WEEK_HEADING_PREFIX,\n''',
    '''                cal_rows,\n                calendar_column_widths,\n                WEEK_HEADING_PREFIX,\n''',
)
replace_once(
    "app.py",
    '''    schedule = list(campaign.get("intake", {}).get("schedule", []))\n    validate_calendar_for_export(calendar["headers"], calendar["rows"], schedule)\n\n    export_metadata = build_base_export_metadata(calendar, client)\n''',
    '''    schedule = list(campaign.get("intake", {}).get("schedule", []))\n    validate_calendar_for_export(calendar["headers"], calendar["rows"], schedule)\n    export_rows = apply_content_status(\n        calendar["headers"],\n        calendar["rows"],\n        CONTENT_STATUS_APPROVED,\n        week_heading_prefix=WEEK_HEADING_PREFIX,\n    )\n\n    export_metadata = build_base_export_metadata(calendar, client)\n''',
)
replace_once(
    "app.py",
    '''            calendar["headers"],\n            calendar["rows"],\n        )\n    return output_file, campaign, calendar, client\n''',
    '''            calendar["headers"],\n            export_rows,\n        )\n    return output_file, campaign, calendar, client\n''',
)
replace_once(
    "app.py",
    '''        if change_scope == "specific_post":\n            selected_label = st.selectbox(\n                "Post that needs changes",\n                [item["label"] for item in reviewable_posts],\n                key=f"senior_change_post_{link['id']}",\n            )\n            selected_post = next(\n                item for item in reviewable_posts if item["label"] == selected_label\n            )\n        selected_fields = st.multiselect(\n            "Which field(s) need changes?",\n            list(REVISION_FIELDS),\n            default=[],\n            key=f"senior_change_fields_{link['id']}",\n            help=(\n                "Only selected fields will be regenerated. Date, Platform, Pillar, "\n                "and Format remain unchanged."\n            ),\n        )\n''',
    '''        if change_scope == "specific_post":\n            selected_label = st.selectbox(\n                "Post that needs changes",\n                [item["label"] for item in reviewable_posts],\n                key=f"senior_change_post_{link['id']}",\n            )\n            selected_post = next(\n                item for item in reviewable_posts if item["label"] == selected_label\n            )\n            available_revision_fields = revision_fields_for_row(\n                calendar["headers"], selected_post["row"]\n            )\n        else:\n            available_revision_fields = revision_fields_for_rows(\n                calendar["headers"],\n                [item["row"] for item in reviewable_posts],\n            )\n        selected_fields = st.multiselect(\n            "Which field(s) need changes?",\n            list(available_revision_fields),\n            default=[],\n            key=f"senior_change_fields_{link['id']}",\n            help=(\n                "Only selected fields will be regenerated. Caption is available for the "\n                "current content-package format; Reel Script is available only when a "\n                "Reel or Video is in scope. Date, Platform, Pillar, Format, and Content "\n                "Status remain unchanged."\n            ),\n        )\n''',
)
replace_once(
    "app.py",
    '    submitted = st.form_submit_button("Generate Content Calendar", use_container_width=True)\n',
    '    submitted = st.form_submit_button("Generate Content Package", use_container_width=True)\n',
)
replace_once(
    "app.py",
    '''Return exactly {posts} content rows in the same order as the fixed posting sequence.\nReturn one Markdown table, with this exact header and no other columns:\n  Date | Platform | Pillar | Format | Content Idea | SEO Keyword Focus | CTA\n\nRules:\n- Do not invent offers, prices, statistics, testimonials, property details, or claims.\n- Put the matching date from the fixed posting sequence in every Date cell.\n- Write Content Idea, SEO Keyword Focus, and CTA in {language}.\n- For Hinglish, use natural Roman-script Hinglish.\n- Keep the table headers and requested Format and Pillar labels unchanged.\n- When a format or pillar mix is specified, use its labels exactly and satisfy\n  every requested count.\n- Give each row one concise, relevant SEO keyword focus phrase.\n- Keep the output concise.\n- Do not add week headings; the application creates the weekly display format.\n- Do not use the `|` character inside a table cell.\n- Do not add extra suggestions after the calendar.\n''',
    '''Return exactly {posts} content rows in the same order as the fixed posting sequence.\nReturn one Markdown table, with this exact header and no other columns:\n  Date | Platform | Pillar | Format | Content Idea | SEO Keyword Focus | CTA | Caption | Reel Script\n\nRules:\n- Do not invent offers, prices, statistics, testimonials, property details, or claims.\n- Put the matching date from the fixed posting sequence in every Date cell.\n- Write Content Idea, SEO Keyword Focus, CTA, Caption, and Reel Script in {language}.\n- For Hinglish, use natural Roman-script Hinglish.\n- Keep the table headers and requested Format and Pillar labels unchanged.\n- When a format or pillar mix is specified, use its labels exactly and satisfy\n  every requested count.\n- Give each row one concise, relevant SEO keyword focus phrase.\n- Every post requires a publish-ready Caption of roughly 20-45 words, aligned to\n  the approved idea, platform, audience, tone, and CTA.\n- For Reel or Video rows, Reel Script must be roughly 45-75 words and stay in one\n  table cell using a compact structure such as: Hook: ...; Scene 1: ...; Scene 2: ...; CTA: ...\n- For Image, Carousel, or Story rows, Reel Script must be exactly: Not applicable\n- Do not output Content Status. The application controls that field so the model\n  cannot mark its own content approved.\n- Keep the output concise.\n- Do not add week headings; the application creates the weekly display format.\n- Do not use the `|` character inside a table cell.\n- Do not add line breaks inside an individual table cell.\n- Do not add extra suggestions after the content package.\n''',
)
replace_once(
    "app.py",
    '''                    try:\n                        calendar_headers, model_rows = parse_markdown_table(\n                            generation_result.content\n                        )\n                        validate_generated_content_mix(\n                            model_rows, 3, format_mix, "Format"\n                        )\n                        validate_generated_content_mix(\n                            model_rows, 2, pillar_mix, "Pillar"\n                        )\n                        calendar_rows = build_canonical_calendar(model_rows, schedule)\n                        rendered_calendar = render_calendar_markdown(\n                            calendar_headers, calendar_rows\n                        )\n''',
    '''                    try:\n                        _generated_headers, model_rows = parse_markdown_table(\n                            generation_result.content,\n                            expected_headers=GENERATION_HEADERS,\n                        )\n                        validate_generated_content_mix(\n                            model_rows, 3, format_mix, "Format"\n                        )\n                        validate_generated_content_mix(\n                            model_rows, 2, pillar_mix, "Pillar"\n                        )\n                        calendar_rows = build_canonical_calendar(model_rows, schedule)\n                        calendar_headers = list(CONTENT_PACKAGE_HEADERS)\n                        rendered_calendar = render_calendar_markdown(\n                            calendar_headers,\n                            calendar_rows,\n                            content_status=CONTENT_STATUS_READY,\n                        )\n''',
)
replace_once(
    "app.py",
    '''                            st.success(\n                                "Content calendar generated and saved. It is now pending Senior approval."\n                            )\n''',
    '''                            st.success(\n                                "Content package generated and saved. It is now pending Senior approval."\n                            )\n''',
)
replace_once(
    "app.py",
    '    st.subheader("Generated Content Calendar")\n',
    '    st.subheader("Generated Content Package")\n',
)
replace_once(
    "app.py",
    '''    st.markdown(st.session_state["result"])\n\n    senior_approval = None\n''',
    '''    display_result = st.session_state["result"]\n    if latest_calendar is not None:\n        status_override = None\n        if campaign_record is not None:\n            status_override = {\n                "pending_senior_review": CONTENT_STATUS_READY,\n                "pending_review": CONTENT_STATUS_READY,\n                "revision_required": CONTENT_STATUS_NEEDS_CHANGES,\n                "rejected": CONTENT_STATUS_NEEDS_CHANGES,\n                "fully_approved": CONTENT_STATUS_APPROVED,\n                "pending_client_review": CONTENT_STATUS_APPROVED,\n                "approved": CONTENT_STATUS_APPROVED,\n            }.get(campaign_record.get("status"))\n        try:\n            display_result = render_calendar_markdown(\n                latest_calendar["headers"],\n                latest_calendar["rows"],\n                content_status=status_override,\n            )\n        except (TypeError, ValueError):\n            display_result = st.session_state["result"]\n    st.markdown(display_result)\n\n    senior_approval = None\n''',
)
replace_once(
    "app.py",
    '''                                    revised_headers, revised_rows = parse_markdown_table(\n                                        revision_result.content\n                                    )\n                                    if revised_headers != CONTENT_CALENDAR_HEADERS:\n                                        raise ValueError(\n                                            "The regenerated response returned an unexpected header."\n                                        )\n''',
    '''                                    revised_headers, revised_rows = parse_markdown_table(\n                                        revision_result.content,\n                                        expected_headers=latest_calendar["headers"],\n                                    )\n                                    if revised_headers != list(latest_calendar["headers"]):\n                                        raise ValueError(\n                                            "The regenerated response returned an unexpected header."\n                                        )\n''',
)
replace_once(
    "app.py",
    '''                                        revised_rows=revised_rows,\n                                        fields_to_change=fields_to_change,\n                                    )\n''',
    '''                                        revised_rows=revised_rows,\n                                        fields_to_change=fields_to_change,\n                                        headers=latest_calendar["headers"],\n                                    )\n''',
)
replace_once(
    "app.py",
    '        st.success("Calendar approved successfully. The campaign owner can now download Excel.")\n',
    '        st.success("Content package approved successfully. The campaign owner can now download Excel.")\n',
)
replace_once(
    "app.py",
    '                        "Download Approved Content Calendar Excel",\n',
    '                        "Download Approved Content Package Excel",\n',
)

print("Content-package transformation applied successfully.")
