"""Make Gemini the primary direct text provider and harden its text path.

Why this patch exists
---------------------
The local application has already been transformed with Brand Kit, Gemini
Creative Studio, Senior Design Approval, and direct Groq/Gemini routing. Those
local source files may contain uncommitted work, so this script applies small,
idempotent edits without replacing app.py or gemini_api.py wholesale.

What it changes
---------------
1. Makes Gemini the default text provider in app.py.
2. Sets the local Streamlit provider setting to Gemini without printing secrets.
3. Pins the stable GA text model to gemini-3.7-flash when no active model setting
   exists.
4. Uses low thinking for routine marketing generation to reduce latency/token
   usage while keeping the model configurable.
5. Adds safer request-size validation and clearer Gemini 400/404/429 errors.
6. Updates the Gemini unit tests for the new request contract.

Run from repository root:
    python staging/apply_gemini_primary_text_provider.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"
GEMINI_PATH = ROOT / "gemini_api.py"
TEST_PATH = ROOT / "tests" / "test_gemini_api.py"
SECRETS_PATH = ROOT / ".streamlit" / "secrets.toml"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text and old not in text:
        print(f"{label}: already applied")
        return text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    print(f"{label}: applied")
    return text.replace(old, new, 1)


def patch_app() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    old = 'DEFAULT_CALENDAR_GENERATION_PROVIDER = "groq"'
    new = 'DEFAULT_CALENDAR_GENERATION_PROVIDER = "gemini"'
    if new in text:
        print("app Gemini default: already applied")
    elif old in text:
        text = text.replace(old, new, 1)
        print("app Gemini default: applied")
    else:
        raise RuntimeError("app Gemini default: provider constant not found")
    APP_PATH.write_text(text, encoding="utf-8")


def patch_local_secrets() -> None:
    if not SECRETS_PATH.exists():
        print("local secrets: not found; source code was patched but provider setting was not changed")
        return

    text = SECRETS_PATH.read_text(encoding="utf-8")
    provider_pattern = re.compile(
        r'(?m)^\s*CALENDAR_GENERATION_PROVIDER\s*=\s*["\'][^"\']+["\']\s*$'
    )
    provider_line = 'CALENDAR_GENERATION_PROVIDER = "gemini"'
    if provider_pattern.search(text):
        text = provider_pattern.sub(provider_line, text, count=1)
    else:
        text = provider_line + "\n" + text

    active_model = re.compile(r'(?m)^\s*GEMINI_TEXT_MODEL\s*=')
    if not active_model.search(text):
        text = text.rstrip() + '\nGEMINI_TEXT_MODEL = "gemini-3.7-flash"\n'

    active_key = re.compile(r'(?m)^\s*GEMINI_API_KEY\s*=\s*["\'](.+?)["\']\s*$')
    key_match = active_key.search(text)
    if not key_match or not key_match.group(1).strip():
        print("local secrets warning: GEMINI_API_KEY is not configured")
    else:
        print("local secrets: Gemini provider selected; API key present (value not displayed)")

    SECRETS_PATH.write_text(text, encoding="utf-8")


def patch_gemini_api() -> None:
    text = GEMINI_PATH.read_text(encoding="utf-8")

    if "DEFAULT_GEMINI_TEXT_THINKING_LEVEL" not in text:
        text = replace_once(
            text,
            'DEFAULT_GEMINI_TEXT_MODEL = "gemini-3.7-flash"\n',
            'DEFAULT_GEMINI_TEXT_MODEL = "gemini-3.7-flash"\n'
            'DEFAULT_GEMINI_TEXT_THINKING_LEVEL = "low"\n',
            "Gemini low-thinking default",
        )
    else:
        print("Gemini low-thinking default: already applied")

    if "SUPPORTED_GEMINI_TEXT_THINKING_LEVELS" not in text:
        anchor = '''SUPPORTED_GEMINI_TEXT_MODELS = (\n    "gemini-3.7-flash",\n    "gemini-3.6-flash",\n    "gemini-3.5-flash-lite",\n)\n'''
        replacement = anchor + '''SUPPORTED_GEMINI_TEXT_THINKING_LEVELS = ("low", "medium", "high")\n'''
        text = replace_once(text, anchor, replacement, "Gemini thinking-level allow-list")
    else:
        print("Gemini thinking-level allow-list: already applied")

    if "MAX_GEMINI_PROMPT_CHARS" not in text:
        text = replace_once(
            text,
            "MAX_GEMINI_TEXT_CHARS = 500_000\n",
            "MAX_GEMINI_TEXT_CHARS = 500_000\nMAX_GEMINI_PROMPT_CHARS = 100_000\n",
            "Gemini prompt-size bound",
        )
    else:
        print("Gemini prompt-size bound: already applied")

    if "def _thinking_level(" not in text:
        model_helper = '''def _model(value: str, supported: tuple[str, ...], label: str) -> str:\n    model = str(value or "").strip()\n    if model not in supported:\n        raise ValueError(f"Unsupported {label}: {model}. Use one of: {', '.join(supported)}.")\n    return model\n\n\n'''
        thinking_helper = model_helper + '''def _thinking_level(value: str) -> str:\n    level = str(value or DEFAULT_GEMINI_TEXT_THINKING_LEVEL).strip().lower()\n    if level not in SUPPORTED_GEMINI_TEXT_THINKING_LEVELS:\n        raise ValueError(\n            "Unsupported Gemini thinking level: "\n            f"{level}. Use one of: {', '.join(SUPPORTED_GEMINI_TEXT_THINKING_LEVELS)}."\n        )\n    return level\n\n\n'''
        text = replace_once(text, model_helper, thinking_helper, "Gemini thinking validator")
    else:
        print("Gemini thinking validator: already applied")

    # Improve provider errors without exposing API keys or full upstream bodies.
    old_status = '''    if status_code == 429:\n        raise GeminiAPIError(\n            "Gemini API quota or rate limit reached. Check Google AI Studio billing/quota.",\n            request_id=request_id,\n            code="GEMINI_RATE_LIMIT",\n            retryable=True,\n        )\n    if status_code < 200 or status_code >= 300:\n        raise GeminiAPIError(\n            "Gemini returned an upstream error.",\n            request_id=request_id,\n            code="GEMINI_UPSTREAM_ERROR",\n            retryable=status_code >= 500,\n        )\n'''
    new_status = '''    if status_code == 400:\n        raise GeminiAPIError(\n            "Gemini rejected the request configuration. Check the selected model and request settings.",\n            request_id=request_id,\n            code="GEMINI_BAD_REQUEST",\n        )\n    if status_code == 404:\n        raise GeminiAPIError(\n            "The configured Gemini model or API endpoint was not found. Check the current stable model setting.",\n            request_id=request_id,\n            code="GEMINI_NOT_FOUND",\n        )\n    if status_code == 429:\n        retry_after = str(getattr(response, "headers", {}).get("Retry-After", "") or "").strip()\n        retry_note = f" Retry after {retry_after}." if retry_after else ""\n        raise GeminiAPIError(\n            "Gemini API quota or rate limit reached."\n            + retry_note\n            + " Limits are applied per Google AI project; check AI Studio Rate Limits.",\n            request_id=request_id,\n            code="GEMINI_RATE_LIMIT",\n            retryable=True,\n        )\n    if status_code < 200 or status_code >= 300:\n        raise GeminiAPIError(\n            "Gemini returned an upstream error.",\n            request_id=request_id,\n            code="GEMINI_UPSTREAM_ERROR",\n            retryable=status_code >= 500,\n        )\n'''
    if new_status in text:
        print("Gemini status diagnostics: already applied")
    elif old_status in text:
        text = text.replace(old_status, new_status, 1)
        print("Gemini status diagnostics: applied")
    else:
        raise RuntimeError("Gemini status diagnostics: expected status anchor not found")

    old_signature = '''def generate_text(\n    *,\n    system_prompt: str,\n    user_prompt: str,\n    api_key: str,\n    model: str = DEFAULT_GEMINI_TEXT_MODEL,\n    request_id: str | None = None,\n    api_url: str = DEFAULT_GEMINI_INTERACTIONS_URL,\n    http_client: Any = requests,\n) -> GeminiTextResult:\n    correlation_id = request_id or str(uuid4())\n    chosen_model = _model(model, SUPPORTED_GEMINI_TEXT_MODELS, "Gemini text model")\n    payload = {\n        "model": chosen_model,\n        "system_instruction": str(system_prompt),\n        "input": str(user_prompt),\n        "store": False,\n        "response_format": {"type": "text"},\n    }\n'''
    new_signature = '''def generate_text(\n    *,\n    system_prompt: str,\n    user_prompt: str,\n    api_key: str,\n    model: str = DEFAULT_GEMINI_TEXT_MODEL,\n    thinking_level: str = DEFAULT_GEMINI_TEXT_THINKING_LEVEL,\n    request_id: str | None = None,\n    api_url: str = DEFAULT_GEMINI_INTERACTIONS_URL,\n    http_client: Any = requests,\n) -> GeminiTextResult:\n    correlation_id = request_id or str(uuid4())\n    chosen_model = _model(model, SUPPORTED_GEMINI_TEXT_MODELS, "Gemini text model")\n    chosen_thinking_level = _thinking_level(thinking_level)\n    system_text = str(system_prompt or "").strip()\n    user_text = str(user_prompt or "").strip()\n    if not system_text or not user_text:\n        raise ValueError("Gemini text prompts must not be empty.")\n    if len(system_text) > MAX_GEMINI_PROMPT_CHARS or len(user_text) > MAX_GEMINI_PROMPT_CHARS:\n        raise ValueError(\n            f"Gemini text prompts must each be at most {MAX_GEMINI_PROMPT_CHARS:,} characters."\n        )\n    payload = {\n        "model": chosen_model,\n        "system_instruction": system_text,\n        "input": user_text,\n        "store": False,\n        "generation_config": {"thinking_level": chosen_thinking_level},\n        "response_format": {"type": "text"},\n    }\n'''
    if new_signature in text:
        print("Gemini text request hardening: already applied")
    elif old_signature in text:
        text = text.replace(old_signature, new_signature, 1)
        print("Gemini text request hardening: applied")
    else:
        raise RuntimeError("Gemini text request hardening: expected generate_text anchor not found")

    GEMINI_PATH.write_text(text, encoding="utf-8")


def patch_tests() -> None:
    text = TEST_PATH.read_text(encoding="utf-8")

    if 'assert payload["generation_config"] == {"thinking_level": "low"}' not in text:
        anchor = '    assert payload["system_instruction"] == "system"\n'
        insertion = anchor + '    assert payload["generation_config"] == {"thinking_level": "low"}\n'
        text = replace_once(text, anchor, insertion, "Gemini low-thinking test")
    else:
        print("Gemini low-thinking test: already applied")

    if "test_generate_text_allows_explicit_medium_thinking" not in text:
        extra = '''\n\ndef test_generate_text_allows_explicit_medium_thinking():\n    client = FakeHTTP(\n        FakeResponse(\n            200,\n            {\n                "steps": [\n                    {\n                        "type": "model_output",\n                        "content": [{"type": "text", "text": "ok"}],\n                    }\n                ]\n            },\n        )\n    )\n    generate_text(\n        system_prompt="system",\n        user_prompt="user",\n        api_key="secret",\n        thinking_level="medium",\n        http_client=client,\n    )\n    assert client.calls[0][1]["json"]["generation_config"] == {\n        "thinking_level": "medium"\n    }\n\n\ndef test_generate_text_rejects_oversized_prompt_before_network():\n    with pytest.raises(ValueError, match="at most"):\n        generate_text(\n            system_prompt="system",\n            user_prompt="x" * 100_001,\n            api_key="secret",\n        )\n'''
        text = text.rstrip() + extra + "\n"
        print("Gemini hardening tests: applied")
    else:
        print("Gemini hardening tests: already applied")

    TEST_PATH.write_text(text, encoding="utf-8")


def main() -> None:
    patch_app()
    patch_local_secrets()
    patch_gemini_api()
    patch_tests()
    print("Gemini is now the primary direct text provider with a stable model and low-thinking request profile.")


if __name__ == "__main__":
    main()
