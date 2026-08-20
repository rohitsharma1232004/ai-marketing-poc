"""Small server-rendered portal for senior and client calendar approvals.

The WhatsApp URL is a one-use bearer capability. A GET request only previews
the link; a deliberate POST exchanges it for a short-lived, HttpOnly browser
session. Decisions are always explicit POSTs and remain governed by
``ReviewService`` and the immutable calendar version stored in SQLite.
"""

from __future__ import annotations

import hmac
import html
import json
import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, quote

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from campaign_store import CampaignStore
from review_service import (
    PublicReviewBundle,
    ReviewDecisionError,
    ReviewDecisionResult,
    ReviewLinkUnavailable,
    ReviewService,
    ReviewServiceConfig,
    ReviewServiceConfigError,
    ReviewServiceError,
    ReviewSessionUnavailable,
)


SESSION_COOKIE = "__Host-calendar_review_session"
CSRF_COOKIE = "__Host-calendar_review_csrf"
DEV_SESSION_COOKIE = "calendar_review_session_dev"
DEV_CSRF_COOKIE = "calendar_review_csrf_dev"
MAX_FORM_BYTES = 16_384
WEEK_HEADING_PREFIX = "__WEEK_HEADING__:"
PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = PROJECT_DIR / "data" / "marketing_poc.sqlite3"

SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
}


class InvalidReviewForm(ValueError):
    """Raised for malformed or oversized browser form submissions."""


def create_app(service: ReviewService | None = None) -> FastAPI:
    """Create the portal, optionally injecting a service for isolated tests."""

    application = FastAPI(
        title="Content Calendar Review",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.review_service = service

    @application.middleware("http")
    async def add_security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

    @application.get("/healthz")
    async def health(request: Request) -> JSONResponse:
        try:
            _service_for(request)
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @application.get("/r/{token}")
    async def preview_review_link(request: Request, token: str) -> HTMLResponse:
        try:
            preview = _service_for(request).preview_link(token)
        except ReviewLinkUnavailable:
            return _unavailable_link_page()
        except Exception:
            return _temporary_problem_page()
        return _interstitial_page(preview.role, preview.expires_at)

    @application.post("/r/{token}/open")
    @application.post('/r/{token}', include_in_schema=False)
    async def exchange_review_link(request: Request, token: str) -> Response:
        try:
            form = await _read_urlencoded_form(
                request,
                required={"intent"},
                optional=set(),
                max_bytes=1_024,
            )
            if form["intent"] != "open_review":
                raise InvalidReviewForm("invalid intent")
        except InvalidReviewForm:
            return _status_page(
                "Invalid request",
                "Use the Continue button from the review link page.",
                status_code=400,
            )

        try:
            review_service = _service_for(request)
            opened = review_service.exchange_link(token)
        except ReviewLinkUnavailable:
            return _unavailable_link_page()
        except Exception:
            return _temporary_problem_page()

        location = f"/review/{quote(opened.review_request_id, safe='')}"
        response = RedirectResponse(location, status_code=303)
        session_cookie, csrf_cookie, secure = _cookie_policy(review_service)
        _set_review_cookie(
            response, session_cookie, opened.session_token, secure=secure
        )
        _set_review_cookie(response, csrf_cookie, opened.csrf_token, secure=secure)
        return response

    @application.get("/review/{review_request_id}")
    async def show_review(request: Request, review_request_id: str) -> HTMLResponse:
        try:
            review_service = _service_for(request)
        except Exception:
            return _temporary_problem_page()
        session_cookie, csrf_cookie, _secure = _cookie_policy(review_service)
        session_token = request.cookies.get(session_cookie, "")
        csrf_token = request.cookies.get(csrf_cookie, "")
        if not session_token or not csrf_token:
            return _unavailable_session_page()
        try:
            bundle = review_service.load_review(session_token)
        except ReviewSessionUnavailable:
            return _unavailable_session_page()
        except Exception:
            return _temporary_problem_page()
        if bundle.review_request_id != review_request_id:
            return _unavailable_session_page()
        return _review_page(bundle, csrf_token)

    @application.post("/review/{review_request_id}/decision")
    async def decide_review(
        request: Request,
        review_request_id: str,
        background_tasks: BackgroundTasks,
    ) -> Response:
        try:
            review_service = _service_for(request)
        except Exception:
            return _temporary_problem_page()
        session_cookie, csrf_cookie_name, _secure = _cookie_policy(review_service)
        session_token = request.cookies.get(session_cookie, "")
        csrf_cookie = request.cookies.get(csrf_cookie_name, "")
        if not session_token or not csrf_cookie:
            return _unavailable_session_page()

        try:
            bundle = review_service.load_review(session_token)
        except ReviewSessionUnavailable:
            return _unavailable_session_page()
        except Exception:
            return _temporary_problem_page()

        if bundle.review_request_id != review_request_id:
            return _unavailable_session_page()

        try:
            form = await _read_urlencoded_form(
                request,
                required={"review_request_id", "csrf_token", "decision"},
                optional={"feedback"},
                max_bytes=MAX_FORM_BYTES,
            )
        except InvalidReviewForm:
            return _review_page(
                bundle,
                csrf_cookie,
                error="The submitted review form is invalid. Please try again.",
                status_code=400,
            )

        if (
            form["review_request_id"] != review_request_id
            or not _constant_time_equal(form["csrf_token"], csrf_cookie)
        ):
            return _review_page(
                bundle,
                csrf_cookie,
                error="The review form expired. Refresh this page and try again.",
                feedback=form.get("feedback", ""),
                status_code=400,
            )

        try:
            result = review_service.decide(
                session_token,
                form["csrf_token"],
                form["decision"],
                form.get("feedback", ""),
            )
        except ReviewDecisionError as error:
            return _review_page(
                bundle,
                csrf_cookie,
                error=_safe_decision_error(error.code),
                feedback=form.get("feedback", ""),
                status_code=400,
            )
        except ReviewSessionUnavailable:
            return _unavailable_session_page()
        except ReviewServiceError:
            return _status_page(
                "Decision not saved",
                "We could not save this decision. Refresh the review page and try again.",
                status_code=409,
            )
        except Exception:
            return _temporary_problem_page()

        if result.next_notification_outbox_id:
            background_tasks.add_task(
                _process_notification_outbox_best_effort,
                review_service,
            )
        response = _decision_saved_page(result)
        _clear_review_cookies(response)
        return response

    return application


def _service_for(request: Request) -> ReviewService:
    service = request.app.state.review_service
    if service is None:
        service = _runtime_service()
    return service


@lru_cache(maxsize=1)
def _runtime_service() -> ReviewService:
    """Load server-only settings on first request, never at module import time."""

    secrets = _read_streamlit_secrets()
    signing_secret = _setting("APPROVAL_LINK_SIGNING_SECRET", secrets)
    public_base_url = _setting("APPROVAL_PUBLIC_BASE_URL", secrets)
    if not signing_secret or not public_base_url:
        raise ReviewServiceConfigError(
            "Approval portal configuration is incomplete.",
            code="CONFIG_INCOMPLETE",
        )
    _reject_placeholder_setting("APPROVAL_LINK_SIGNING_SECRET", signing_secret)

    database_value = _setting("CAMPAIGN_DB_PATH", secrets, str(DEFAULT_DB_PATH))
    database_path = Path(database_value)
    if not database_path.is_absolute():
        database_path = PROJECT_DIR / database_path

    webhook_secret = _setting("N8N_REVIEW_WEBHOOK_SECRET", secrets)
    if webhook_secret:
        _reject_placeholder_setting("N8N_REVIEW_WEBHOOK_SECRET", webhook_secret)
    config = ReviewServiceConfig(
        signing_secret=signing_secret,
        public_base_url=public_base_url,
        webhook_url=_setting("N8N_WHATSAPP_REVIEW_WEBHOOK_URL", secrets),
        webhook_secret=webhook_secret,
        review_ttl_hours=_integer_setting(
            "APPROVAL_LINK_TTL_HOURS", secrets, default=72
        ),
        session_ttl_minutes=_integer_setting(
            "APPROVAL_SESSION_TTL_MINUTES", secrets, default=30
        ),
        allow_insecure_localhost=_boolean_setting(
            "APPROVAL_ALLOW_INSECURE_LOCALHOST", secrets, default=False
        ),
    )
    return ReviewService(CampaignStore(database_path), config)


def _read_streamlit_secrets() -> Mapping[str, Any]:
    path = PROJECT_DIR / ".streamlit" / "secrets.toml"
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as secrets_file:
            values = tomllib.load(secrets_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReviewServiceConfigError(
            "Approval portal configuration is unavailable.",
            code="CONFIG_UNAVAILABLE",
        ) from error
    return values


def _setting(
    name: str,
    secrets: Mapping[str, Any],
    default: str = "",
) -> str:
    value = os.environ.get(name)
    if value is None:
        value = secrets.get(name, default)
    if value is None:
        return ""
    return str(value).strip()


def _reject_placeholder_setting(name: str, value: str) -> None:
    normalized = str(value or "").strip().casefold()
    markers = (
        "replace_with",
        "your_secret",
        "your_key",
        "change_me",
        "changeme",
        "placeholder",
        "example",
    )
    if any(marker in normalized for marker in markers):
        raise ReviewServiceConfigError(
            f"{name} must be replaced with a private random value.",
            code="CONFIG_PLACEHOLDER",
        )


def _integer_setting(
    name: str,
    secrets: Mapping[str, Any],
    *,
    default: int,
) -> int:
    value: Any = os.environ.get(name)
    if value is None:
        value = secrets.get(name, default)
    if isinstance(value, bool):
        raise ReviewServiceConfigError(
            "Approval portal configuration is invalid.",
            code="CONFIG_VALUE_INVALID",
        )
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ReviewServiceConfigError(
            "Approval portal configuration is invalid.",
            code="CONFIG_VALUE_INVALID",
        ) from error


def _boolean_setting(
    name: str,
    secrets: Mapping[str, Any],
    *,
    default: bool,
) -> bool:
    value: Any = os.environ.get(name)
    if value is None:
        value = secrets.get(name, default)
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ReviewServiceConfigError(
        "Approval portal configuration is invalid.",
        code="CONFIG_VALUE_INVALID",
    )


async def _read_urlencoded_form(
    request: Request,
    *,
    required: set[str],
    optional: set[str],
    max_bytes: int,
) -> dict[str, str]:
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().lower() != "application/x-www-form-urlencoded":
        raise InvalidReviewForm("unsupported content type")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise InvalidReviewForm("form is too large")
        chunks.append(chunk)
    try:
        encoded = b"".join(chunks).decode("utf-8", errors="strict")
        parsed = parse_qs(
            encoded,
            keep_blank_values=True,
            strict_parsing=False,
            max_num_fields=20,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise InvalidReviewForm("form encoding is invalid") from error

    allowed = required | optional
    if set(parsed) - allowed or required - set(parsed):
        raise InvalidReviewForm("unexpected or missing form fields")
    if any(len(values) != 1 for values in parsed.values()):
        raise InvalidReviewForm("duplicate form fields")
    return {name: values[0] for name, values in parsed.items()}


def _interstitial_page(role: str, expires_at: str) -> HTMLResponse:
    role_label = _role_label(role)
    body = (
        f'<p class="eyebrow">{_escape(role_label)} review</p>'
        "<h1>Content calendar ready for review</h1>"
        "<p>Continue to inspect the exact saved calendar before making a decision.</p>"
        f'<p class="meta">This link is available until {_escape(expires_at)}.</p>'
        '<form action="" method="post">'
        '<input type="hidden" name="intent" value="open_review">'
        '<button type="submit">Continue to calendar</button>'
        "</form>"
        '<p class="notice">Opening or previewing this link does not approve anything.</p>'
    )
    return _html_page("Calendar review", body)


def _review_page(
    bundle: PublicReviewBundle,
    csrf_token: str,
    *,
    error: str = "",
    feedback: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    role_label = _role_label(bundle.role)
    error_html = (
        f'<div class="error" role="alert">{_escape(error)}</div>' if error else ""
    )
    approvals_html = _approvals_table(bundle.approvals)
    body = (
        f'<p class="eyebrow">{_escape(role_label)} review</p>'
        f"<h1>{_escape(bundle.client_name)} content calendar</h1>"
        f'<p class="meta">Reviewer: {_escape(bundle.reviewer_name)} '
        f'({_escape(bundle.reviewer_phone_masked)}) &middot; '
        f'Calendar version {_escape(bundle.calendar_version)}</p>'
        f"{error_html}"
        f'<div class="table-wrap">{_calendar_table(bundle.headers, bundle.rows)}</div>'
        f"{approvals_html}"
        f'<form method="post" action="/review/{quote(bundle.review_request_id, safe="")}/decision">'
        f'<input type="hidden" name="review_request_id" value="{_escape(bundle.review_request_id)}">'
        f'<input type="hidden" name="csrf_token" value="{_escape(csrf_token)}">'
        '<label for="feedback">Feedback (required when requesting changes)</label>'
        f'<textarea id="feedback" name="feedback" maxlength="5000" rows="5">{_escape(feedback)}</textarea>'
        '<div class="actions">'
        '<button class="approve" type="submit" name="decision" value="approved">Approve calendar</button>'
        '<button class="reject" type="submit" name="decision" value="rejected">Request changes</button>'
        "</div></form>"
        '<p class="notice">Your decision applies only to the exact calendar version shown above.</p>'
    )
    return _html_page("Review content calendar", body, status_code=status_code)


def _calendar_table(headers: Sequence[str], rows: Sequence[Any]) -> str:
    header_cells = "".join(
        f'<th scope="col">{_escape(item)}</th>' for item in headers
    )
    rendered_rows: list[str] = []
    width = max(len(headers), 1)
    for row in rows:
        if (
            isinstance(row, Sequence)
            and not isinstance(row, (str, bytes, bytearray))
            and len(row) == 1
            and width > 1
        ):
            heading = row[0]
            if isinstance(heading, str) and heading.startswith(WEEK_HEADING_PREFIX):
                heading = heading[len(WEEK_HEADING_PREFIX) :]
            rendered_rows.append(
                f'<tr class="week"><th colspan="{width}">{_escape(heading)}</th></tr>'
            )
            continue
        cells = _regular_row_cells(row, headers)
        if cells is None:
            rendered_rows.append(
                f'<tr><td colspan="{width}">{_escape(_display_value(row))}</td></tr>'
            )
            continue
        rendered_rows.append(
            "<tr>"
            + "".join(f"<td>{_escape(cell)}</td>" for cell in cells)
            + "</tr>"
        )
    return (
        "<table><thead><tr>"
        + header_cells
        + "</tr></thead><tbody>"
        + "".join(rendered_rows)
        + "</tbody></table>"
    )


def _regular_row_cells(row: Any, headers: Sequence[str]) -> list[Any] | None:
    if isinstance(row, Mapping):
        if all(header in row for header in headers) and len(row) == len(headers):
            return [row[header] for header in headers]
        return None
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        if len(row) == len(headers):
            return list(row)
        return None
    return None


def _approvals_table(approvals: Sequence[Mapping[str, Any]]) -> str:
    if not approvals:
        return ""
    rows = []
    for approval in approvals:
        rows.append(
            "<tr>"
            f"<td>{_escape(_role_label(str(approval.get('role') or '')))}</td>"
            f"<td>{_escape(approval.get('decision') or '')}</td>"
            f"<td>{_escape(approval.get('approver_name') or '')}</td>"
            f"<td>{_escape(approval.get('feedback') or '')}</td>"
            "</tr>"
        )
    return (
        "<h2>Previous decision</h2>"
        '<div class="table-wrap"><table><thead><tr>'
        "<th>Role</th><th>Decision</th><th>Reviewer</th><th>Feedback</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></div>"
    )


def _decision_saved_page(result: ReviewDecisionResult) -> HTMLResponse:
    if result.decision == "rejected":
        title = "Changes requested"
        message = (
            "Your feedback was saved. The calendar must be revised before a new "
            "approval request can be sent."
        )
    elif result.role == "senior" and result.decision == "approved":
        title = "Senior approval saved"
        message = (
            "The client review is now queued for WhatsApp delivery. Your decision "
            "cannot be submitted again from this session."
        )
    elif result.role == "client" and result.decision == "approved":
        title = "Calendar fully approved"
        message = (
            "Both approval stages are complete. The final Excel download is now "
            "unlocked in the campaign dashboard."
        )
    else:
        title = "Decision saved"
        message = "Your calendar decision was saved successfully."
    return _status_page(title, message, status_code=200)


def _safe_decision_error(code: str) -> str:
    messages = {
        "CSRF_INVALID": "The review form expired. Refresh this page and try again.",
        "DECISION_INVALID": "Choose Approve calendar or Request changes.",
        "FEEDBACK_REQUIRED": "Add feedback explaining the requested changes.",
        "FEEDBACK_TOO_LONG": "Feedback must be 5,000 characters or fewer.",
        "CLIENT_REVIEWER_MISSING": (
            "A consented client WhatsApp reviewer must be configured before senior approval."
        ),
        "DECISION_NOT_SAVED": "The decision could not be saved. Please try again.",
    }
    return messages.get(code, "The decision could not be saved. Please try again.")


def _unavailable_link_page() -> HTMLResponse:
    return _status_page(
        "Review link unavailable",
        "This link is invalid, expired, already used, or has been replaced. Ask the sender for a new WhatsApp review link.",
        status_code=410,
    )


def _unavailable_session_page() -> HTMLResponse:
    response = _status_page(
        "Review session unavailable",
        "This review session has expired or was already used. Open the latest WhatsApp review link to continue.",
        status_code=410,
    )
    _clear_review_cookies(response)
    return response


def _temporary_problem_page() -> HTMLResponse:
    return _status_page(
        "Review temporarily unavailable",
        "The review page cannot be loaded right now. Please try again later.",
        status_code=503,
    )


def _status_page(title: str, message: str, *, status_code: int) -> HTMLResponse:
    body = f"<h1>{_escape(title)}</h1><p>{_escape(message)}</p>"
    return _html_page(title, body, status_code=status_code)


def _html_page(title: str, body: str, *, status_code: int = 200) -> HTMLResponse:
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(title)}</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, system-ui, sans-serif; }}
    body {{ margin: 0; background: #f5f7fb; color: #172033; }}
    main {{ width: min(1180px, calc(100% - 32px)); margin: 40px auto; background: #fff;
      border: 1px solid #dce2ec; border-radius: 14px; padding: 28px; box-sizing: border-box; }}
    h1 {{ margin: 0 0 12px; font-size: clamp(1.6rem, 4vw, 2.3rem); }}
    h2 {{ margin-top: 28px; }}
    .eyebrow {{ color: #405cf5; font-weight: 700; text-transform: uppercase;
      letter-spacing: .08em; font-size: .8rem; }}
    .meta, .notice {{ color: #566176; }}
    .notice {{ margin-top: 18px; font-size: .92rem; }}
    .error {{ margin: 18px 0; padding: 12px 14px; border-radius: 8px;
      color: #8b1a1a; background: #fff0f0; border: 1px solid #f3bcbc; }}
    .table-wrap {{ overflow-x: auto; margin: 22px 0; }}
    table {{ border-collapse: collapse; width: 100%; min-width: 760px; }}
    th, td {{ border: 1px solid #ccd3df; padding: 10px; text-align: left;
      vertical-align: top; white-space: pre-wrap; }}
    thead th, .week th {{ background: #edf1f8; }}
    label {{ display: block; font-weight: 700; margin: 18px 0 7px; }}
    textarea {{ width: 100%; box-sizing: border-box; padding: 10px;
      border: 1px solid #aab3c2; border-radius: 7px; font: inherit; }}
    .actions {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }}
    button {{ border: 0; border-radius: 7px; padding: 11px 17px; font: inherit;
      font-weight: 700; cursor: pointer; background: #405cf5; color: #fff; }}
    button.reject {{ background: #8b2733; }}
  </style>
</head>
<body><main>{body}</main></body>
</html>"""
    return HTMLResponse(document, status_code=status_code)


def _cookie_policy(service: ReviewService) -> tuple[str, str, bool]:
    config = getattr(service, "config", None)
    public_base_url = str(getattr(config, "public_base_url", "https://portal"))
    if public_base_url.casefold().startswith("http://"):
        return DEV_SESSION_COOKIE, DEV_CSRF_COOKIE, False
    return SESSION_COOKIE, CSRF_COOKIE, True


def _set_review_cookie(
    response: Response,
    name: str,
    value: str,
    *,
    secure: bool,
) -> None:
    response.set_cookie(
        name,
        value,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def _clear_review_cookies(response: Response) -> None:
    cookie_settings = (
        (SESSION_COOKIE, True),
        (CSRF_COOKIE, True),
        (DEV_SESSION_COOKIE, False),
        (DEV_CSRF_COOKIE, False),
    )
    for name, secure in cookie_settings:
        response.delete_cookie(
            name,
            path="/",
            secure=secure,
            httponly=True,
            samesite="lax",
        )


def _process_notification_outbox_best_effort(service: ReviewService) -> None:
    try:
        service.process_notification_outbox(limit=10)
    except Exception:
        # The database decision is authoritative; transport failure is retried
        # through the durable outbox and must never roll back an approval.
        return


def _role_label(role: str) -> str:
    return {"senior": "Senior", "client": "Client"}.get(
        str(role or "").strip().lower(), "Reviewer"
    )


def _display_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (Mapping, list, tuple, bool, int, float)):
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            pass
    return str(value)


def _escape(value: Any) -> str:
    return html.escape(_display_value(value), quote=True)


def _constant_time_equal(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (AttributeError, UnicodeEncodeError):
        return False


app = create_app()


__all__ = ["CSRF_COOKIE", "SESSION_COOKIE", "app", "create_app"]
