from pathlib import Path


def replace_once(path, old, new):
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one match in {path}, found {count}: {old[:80]!r}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


# ---------------- campaign_store.py ----------------
replace_once(
    "campaign_store.py",
    '''from content_package import (
    REEL_SCRIPT_FORMATS,
    REVISION_FIELDS,
    require_supported_calendar_headers,
)


SCHEMA_VERSION = 6
''',
    '''from content_package import (
    REEL_SCRIPT_FORMATS,
    REVISION_FIELDS,
    require_supported_calendar_headers,
)
from design_brief import normalize_design_brief


SCHEMA_VERSION = 7
''',
)

campaign_methods = '''    def save_design_briefs(
        self,
        campaign_id: str,
        calendar_version_id: str,
        content_hash: str,
        briefs: Sequence[Mapping[str, Any]],
        *,
        generation_metadata: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Persist one complete design-brief set for a final Senior-approved version."""

        clean_campaign_id = _canonical_uuid(campaign_id, "campaign_id")
        clean_calendar_id = _canonical_uuid(
            calendar_version_id, "calendar_version_id"
        )
        clean_hash = _sha256_hash(content_hash, "content_hash")
        if isinstance(briefs, (str, bytes, bytearray)) or not isinstance(briefs, Sequence):
            raise TypeError("briefs must be a sequence of design brief objects.")
        brief_values = list(briefs)
        if not brief_values:
            raise ValueError("briefs must not be empty.")
        metadata = _require_mapping(generation_metadata, "generation_metadata")
        metadata_json = _serialize_json(metadata, "generation_metadata")
        now = _utc_now()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (clean_campaign_id,)
            ).fetchone()
            if campaign is None:
                raise RecordNotFound(f"Campaign {clean_campaign_id} was not found.")
            if campaign["status"] not in {"fully_approved", "approved"}:
                raise InvalidStatusTransition(
                    "Design briefs can be generated only after final Senior approval."
                )
            calendar = connection.execute(
                "SELECT * FROM calendar_versions WHERE id=? AND campaign_id=?",
                (clean_calendar_id, clean_campaign_id),
            ).fetchone()
            if calendar is None:
                raise RecordNotFound(
                    "That calendar version does not belong to this campaign."
                )
            latest = connection.execute(
                "SELECT id FROM calendar_versions WHERE campaign_id=? "
                "ORDER BY version DESC LIMIT 1",
                (clean_campaign_id,),
            ).fetchone()
            if latest is None or latest["id"] != clean_calendar_id:
                raise StoreConflict(
                    "Design briefs can be generated only for the latest content version."
                )
            calculated_hash = _calendar_content_hash(
                _deserialize_json(calendar["headers_json"]),
                _deserialize_json(calendar["rows_json"]),
                _deserialize_json(calendar["client_metadata_json"]),
                _deserialize_json(calendar["generation_metadata_json"]),
            )
            if calculated_hash != calendar["content_hash"]:
                raise StoreConflict(
                    "The approved content no longer matches its stored hash."
                )
            if calculated_hash != clean_hash:
                raise StoreConflict(
                    "The design brief request does not match the approved content hash."
                )
            senior_approval = connection.execute(
                "SELECT 1 FROM approvals WHERE campaign_id=? AND calendar_version_id=? "
                "AND role='senior' AND decision='approved' AND content_hash=?",
                (clean_campaign_id, clean_calendar_id, calculated_hash),
            ).fetchone()
            if senior_approval is None:
                raise InvalidStatusTransition(
                    "A hash-matched Senior approval is required before design briefs."
                )

            headers = list(
                require_supported_calendar_headers(
                    _deserialize_json(calendar["headers_json"])
                )
            )
            stored_rows = _deserialize_json(calendar["rows_json"])
            content_rows = [
                (row_index, row)
                for row_index, row in enumerate(stored_rows)
                if isinstance(row, list) and len(row) == len(headers)
            ]
            if len(brief_values) != len(content_rows):
                raise ValueError(
                    "Design brief count must match the approved post count."
                )
            duplicate = connection.execute(
                "SELECT 1 FROM design_briefs WHERE campaign_id=? AND calendar_version_id=? "
                "LIMIT 1",
                (clean_campaign_id, clean_calendar_id),
            ).fetchone()
            if duplicate is not None:
                raise StoreConflict(
                    "Design briefs already exist for this approved content version."
                )

            format_index = headers.index("Format")
            for post_number, (raw_brief, (row_index, row)) in enumerate(
                zip(brief_values, content_rows), start=1
            ):
                normalized_brief = normalize_design_brief(
                    raw_brief,
                    expected_post_number=post_number,
                    expected_format=str(row[format_index]),
                )
                connection.execute(
                    """
                    INSERT INTO design_briefs (
                        id,campaign_id,calendar_version_id,content_hash,
                        post_number,row_index,format,brief_json,
                        generation_metadata_json,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(uuid4()),
                        clean_campaign_id,
                        clean_calendar_id,
                        calculated_hash,
                        post_number,
                        row_index,
                        normalized_brief["format"],
                        _serialize_json(normalized_brief, "design_brief"),
                        metadata_json,
                        now,
                    ),
                )

            event_details = {
                "calendar_version_id": clean_calendar_id,
                "content_hash": calculated_hash,
                "brief_count": len(brief_values),
            }
            for key in ("request_id", "provider", "model"):
                if metadata.get(key) not in (None, ""):
                    event_details[key] = metadata[key]
            self._insert_event(
                connection,
                campaign_id=clean_campaign_id,
                event_type="design_briefs_generated",
                details=event_details,
                from_status=campaign["status"],
                to_status=campaign["status"],
                timestamp=now,
            )
            connection.commit()

        return self.list_design_briefs(clean_campaign_id, clean_calendar_id)

    def list_design_briefs(
        self, campaign_id: str, calendar_version_id: str
    ) -> list[dict[str, Any]]:
        """Return hash-verified design briefs for one exact content version."""

        clean_campaign_id = _canonical_uuid(campaign_id, "campaign_id")
        clean_calendar_id = _canonical_uuid(
            calendar_version_id, "calendar_version_id"
        )
        with self._connection() as connection:
            calendar = connection.execute(
                "SELECT * FROM calendar_versions WHERE id=? AND campaign_id=?",
                (clean_calendar_id, clean_campaign_id),
            ).fetchone()
            if calendar is None:
                raise RecordNotFound(
                    "That calendar version does not belong to this campaign."
                )
            calculated_hash = _calendar_content_hash(
                _deserialize_json(calendar["headers_json"]),
                _deserialize_json(calendar["rows_json"]),
                _deserialize_json(calendar["client_metadata_json"]),
                _deserialize_json(calendar["generation_metadata_json"]),
            )
            if calculated_hash != calendar["content_hash"]:
                raise StoreConflict(
                    "The content version no longer matches its stored hash."
                )
            rows = connection.execute(
                "SELECT * FROM design_briefs WHERE campaign_id=? AND calendar_version_id=? "
                "ORDER BY post_number ASC",
                (clean_campaign_id, clean_calendar_id),
            ).fetchall()
        results = [_design_brief_from_row(row) for row in rows]
        if any(item["content_hash"] != calculated_hash for item in results):
            raise StoreConflict(
                "Stored design briefs do not match the approved content hash."
            )
        return results

'''
replace_once(
    "campaign_store.py",
    '    # Manual client-share audit APIs appear before the automated review APIs.\n',
    campaign_methods
    + '    # Manual client-share audit APIs appear before the automated review APIs.\n',
)

replace_once(
    "campaign_store.py",
    '''            self._ensure_v5_senior_share_schema(connection)
            self._ensure_v6_senior_change_schema(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
''',
    '''            self._ensure_v5_senior_share_schema(connection)
            self._ensure_v6_senior_change_schema(connection)
            self._ensure_v7_design_brief_schema(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
''',
)

schema_method = '''    @staticmethod
    def _ensure_v7_design_brief_schema(connection: sqlite3.Connection) -> None:
        """Store designer-ready briefs separately from immutable approved content."""

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS design_briefs (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                calendar_version_id TEXT NOT NULL,
                content_hash TEXT NOT NULL CHECK (
                    length(content_hash)=64
                    AND content_hash NOT GLOB '*[^0-9a-f]*'
                ),
                post_number INTEGER NOT NULL CHECK (post_number > 0),
                row_index INTEGER NOT NULL CHECK (row_index >= 0),
                format TEXT NOT NULL,
                brief_json TEXT NOT NULL,
                generation_metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (campaign_id, calendar_version_id, post_number),
                FOREIGN KEY (campaign_id, calendar_version_id)
                    REFERENCES calendar_versions(campaign_id, id) ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS design_briefs_version_idx "
            "ON design_briefs(campaign_id,calendar_version_id,post_number)"
        )

'''
replace_once(
    "campaign_store.py",
    '    @staticmethod\n    def _ensure_v3_indexes_and_triggers(connection: sqlite3.Connection) -> None:\n',
    schema_method
    + '    @staticmethod\n    def _ensure_v3_indexes_and_triggers(connection: sqlite3.Connection) -> None:\n',
)

helper = '''def _design_brief_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "campaign_id": row["campaign_id"],
        "calendar_version_id": row["calendar_version_id"],
        "content_hash": row["content_hash"],
        "post_number": row["post_number"],
        "row_index": row["row_index"],
        "format": row["format"],
        "brief": _deserialize_json(row["brief_json"]),
        "generation_metadata": _deserialize_json(row["generation_metadata_json"]),
        "created_at": row["created_at"],
    }


'''
replace_once(
    "campaign_store.py",
    'def _senior_change_request_from_row(row: sqlite3.Row) -> dict[str, Any]:\n',
    helper + 'def _senior_change_request_from_row(row: sqlite3.Row) -> dict[str, Any]:\n',
)


# ---------------- app.py ----------------
replace_once(
    "app.py",
    '''from generation_providers import (
    DEFAULT_GROQ_API_URL,
    GenerationProviderError,
    generate_calendar_content,
)
''',
    '''from design_brief import (
    DESIGN_BRIEF_SYSTEM_PROMPT,
    DESIGN_STATUS_BRIEF_READY,
    DESIGN_STATUS_LOCKED,
    DESIGN_STATUS_NOT_GENERATED,
    build_design_brief_prompt,
    display_design_brief_sections,
    parse_design_brief_response,
)
from generation_providers import (
    DEFAULT_GROQ_API_URL,
    GenerationProviderError,
    generate_calendar_content,
)
''',
)

replace_once(
    "app.py",
    '        f"Client details → {provider_label} → Content Package → Senior Approval → Excel Download"\n',
    '        f"Client details → {provider_label} → Content Package → Senior Approval → Design Briefs / Excel"\n',
)

old_render = '''def render_calendar_markdown(headers, rows, *, content_status=None):
    """Render the validated calendar in the weekly format shown in the UI."""
    display_rows = (
        apply_content_status(
            headers, rows, content_status, week_heading_prefix=WEEK_HEADING_PREFIX
        )
        if content_status
        else [list(map(str, row)) for row in rows]
    )
    header_line = "| " + " | ".join(headers) + " |"
    separator_line = "| " + " | ".join(["---"] * len(headers)) + " |"
    lines = []

    for row in display_rows:
        if is_week_heading(row):
            if lines:
                lines.append("")
            lines.append(f"## {row[0][len(WEEK_HEADING_PREFIX):]}")
            lines.append("")
            lines.extend([header_line, separator_line])
        else:
            lines.append("| " + " | ".join(row) + " |")

    return "\n".join(lines).strip()
'''
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

    return "\n".join(lines).strip()
'''
replace_once("app.py", old_render, new_render)

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


# ---------------- documentation ----------------
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

print("Design Brief integration transformation applied successfully.")
