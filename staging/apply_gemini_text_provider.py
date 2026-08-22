"""Enable Gemini as a first-class text generation provider in app.py.

This keeps Groq and n8n behavior unchanged while allowing:
    CALENDAR_GENERATION_PROVIDER = "gemini"
    GEMINI_API_KEY = "..."
    GEMINI_TEXT_MODEL = "gemini-3.7-flash"

Run from repository root:
    python staging/apply_gemini_text_provider.py
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
    if "from generation_router import generate_calendar_content" in text:
        print("app.py Gemini text provider already applied")
        return

    import_anchor = '''from generation_providers import (
    DEFAULT_GROQ_API_URL,
    GenerationProviderError,
    generate_calendar_content,
)
'''
    import_new = '''from generation_providers import DEFAULT_GROQ_API_URL, GenerationProviderError
from generation_router import generate_calendar_content
from gemini_api import DEFAULT_GEMINI_INTERACTIONS_URL, DEFAULT_GEMINI_TEXT_MODEL
'''
    text = replace_once(text, import_anchor, import_new, "Gemini text imports")

    # The variable names remain backward-compatible, but switch to a Gemini model
    # when Gemini is the selected provider.
    initial_model = '''    groq_model = get_app_setting("GROQ_MODEL", DEFAULT_GROQ_MODEL)
    n8n_webhook_url = get_app_setting("N8N_CALENDAR_WEBHOOK_URL")
'''
    initial_model_new = '''    groq_model = get_app_setting("GROQ_MODEL", DEFAULT_GROQ_MODEL)
    if generation_provider == "gemini":
        groq_model = get_app_setting("GEMINI_TEXT_MODEL", DEFAULT_GEMINI_TEXT_MODEL)
    n8n_webhook_url = get_app_setting("N8N_CALENDAR_WEBHOOK_URL")
'''
    text = replace_once(text, initial_model, initial_model_new, "initial Gemini model selection")

    brief_model = '''                brief_model = get_app_setting("GROQ_MODEL", DEFAULT_GROQ_MODEL)
                brief_n8n_url = get_app_setting("N8N_CALENDAR_WEBHOOK_URL")
'''
    brief_model_new = '''                brief_model = get_app_setting("GROQ_MODEL", DEFAULT_GROQ_MODEL)
                if brief_provider == "gemini":
                    brief_model = get_app_setting("GEMINI_TEXT_MODEL", DEFAULT_GEMINI_TEXT_MODEL)
                brief_n8n_url = get_app_setting("N8N_CALENDAR_WEBHOOK_URL")
'''
    text = replace_once(text, brief_model, brief_model_new, "design brief Gemini model selection")

    revision_model = '''                        revision_model = get_app_setting("GROQ_MODEL", DEFAULT_GROQ_MODEL)
                        revision_n8n_url = get_app_setting("N8N_CALENDAR_WEBHOOK_URL")
'''
    revision_model_new = '''                        revision_model = get_app_setting("GROQ_MODEL", DEFAULT_GROQ_MODEL)
                        if revision_provider == "gemini":
                            revision_model = get_app_setting("GEMINI_TEXT_MODEL", DEFAULT_GEMINI_TEXT_MODEL)
                        revision_n8n_url = get_app_setting("N8N_CALENDAR_WEBHOOK_URL")
'''
    text = replace_once(text, revision_model, revision_model_new, "revision Gemini model selection")

    provider_set_count = text.count('not in {"groq", "n8n"}')
    if provider_set_count != 3:
        raise RuntimeError(
            f"provider allow-list: expected 3 anchors, found {provider_set_count}."
        )
    text = text.replace(
        'not in {"groq", "n8n"}',
        'not in {"groq", "gemini", "n8n"}',
    )
    text = text.replace(
        "CALENDAR_GENERATION_PROVIDER must be either 'groq' or 'n8n'.",
        "CALENDAR_GENERATION_PROVIDER must be 'groq', 'gemini', or 'n8n'.",
    )

    initial_key_anchor = '''    elif generation_provider == "groq" and not groq_api_key:
        st.error(
            "GROQ_API_KEY is missing. Add it to the environment or "
            ".streamlit/secrets.toml and restart the app."
        )
    elif generation_provider == "n8n" and not n8n_webhook_url:
'''
    initial_key_new = '''    elif generation_provider == "groq" and not groq_api_key:
        st.error(
            "GROQ_API_KEY is missing. Add it to the environment or "
            ".streamlit/secrets.toml and restart the app."
        )
    elif generation_provider == "gemini" and not get_app_setting("GEMINI_API_KEY"):
        st.error(
            "GEMINI_API_KEY is missing. Add a Gemini Developer API key to the "
            "environment or .streamlit/secrets.toml and restart the app."
        )
    elif generation_provider == "n8n" and not n8n_webhook_url:
'''
    text = replace_once(text, initial_key_anchor, initial_key_new, "initial Gemini key check")

    brief_key_anchor = '''                elif brief_provider == "groq" and not brief_groq_key:
                    brief_config_error = "GROQ_API_KEY is missing."
                elif brief_provider == "n8n" and not brief_n8n_url:
'''
    brief_key_new = '''                elif brief_provider == "groq" and not brief_groq_key:
                    brief_config_error = "GROQ_API_KEY is missing."
                elif brief_provider == "gemini" and not get_app_setting("GEMINI_API_KEY"):
                    brief_config_error = "GEMINI_API_KEY is missing."
                elif brief_provider == "n8n" and not brief_n8n_url:
'''
    text = replace_once(text, brief_key_anchor, brief_key_new, "design brief Gemini key check")

    revision_key_anchor = '''                        elif revision_provider == "groq" and not revision_groq_key:
                            revision_config_error = "GROQ_API_KEY is missing."
                        elif revision_provider == "n8n" and not revision_n8n_url:
'''
    revision_key_new = '''                        elif revision_provider == "groq" and not revision_groq_key:
                            revision_config_error = "GROQ_API_KEY is missing."
                        elif revision_provider == "gemini" and not get_app_setting("GEMINI_API_KEY"):
                            revision_config_error = "GEMINI_API_KEY is missing."
                        elif revision_provider == "n8n" and not revision_n8n_url:
'''
    text = replace_once(text, revision_key_anchor, revision_key_new, "revision Gemini key check")

    text = replace_once(
        text,
        '        generation_label = "n8n" if generation_provider == "n8n" else "Groq"\n',
        '        generation_label = {"groq": "Groq", "gemini": "Gemini", "n8n": "n8n"}[generation_provider]\n',
        "initial provider label",
    )
    text = replace_once(
        text,
        '                    brief_label = "n8n" if brief_provider == "n8n" else "Groq"\n',
        '                    brief_label = {"groq": "Groq", "gemini": "Gemini", "n8n": "n8n"}[brief_provider]\n',
        "design brief provider label",
    )
    text = replace_once(
        text,
        '                            revision_label = "n8n" if revision_provider == "n8n" else "Groq"\n',
        '                            revision_label = {"groq": "Groq", "gemini": "Gemini", "n8n": "n8n"}[revision_provider]\n',
        "revision provider label",
    )

    # Every text generation call gets Gemini credentials; non-Gemini providers
    # simply ignore these keyword arguments inside generation_router.
    call_anchor = "                        groq_api_url=groq_api_url,\n"
    call_new = (
        call_anchor
        + '                        gemini_api_key=get_app_setting("GEMINI_API_KEY"),\n'
        + '                        gemini_api_url=get_app_setting("GEMINI_INTERACTIONS_URL", DEFAULT_GEMINI_INTERACTIONS_URL),\n'
    )
    text = replace_once(text, call_anchor, call_new, "initial Gemini call credentials")

    brief_call_anchor = "                                    groq_api_url=brief_groq_url,\n"
    brief_call_new = (
        brief_call_anchor
        + '                                    gemini_api_key=get_app_setting("GEMINI_API_KEY"),\n'
        + '                                    gemini_api_url=get_app_setting("GEMINI_INTERACTIONS_URL", DEFAULT_GEMINI_INTERACTIONS_URL),\n'
    )
    text = replace_once(text, brief_call_anchor, brief_call_new, "design brief Gemini call credentials")

    revision_call_anchor = "                                        groq_api_url=revision_groq_url,\n"
    revision_call_new = (
        revision_call_anchor
        + '                                        gemini_api_key=get_app_setting("GEMINI_API_KEY"),\n'
        + '                                        gemini_api_url=get_app_setting("GEMINI_INTERACTIONS_URL", DEFAULT_GEMINI_INTERACTIONS_URL),\n'
    )
    text = replace_once(text, revision_call_anchor, revision_call_new, "revision Gemini call credentials")

    text = text.replace(
        '                        "GROQ_TIMEOUT",\n                        "N8N_TIMEOUT",',
        '                        "GROQ_TIMEOUT",\n                        "GEMINI_TIMEOUT",\n                        "N8N_TIMEOUT",',
    )
    text = text.replace(
        "Only extracted text is sent to Groq; upload material you are allowed to share.",
        "Only extracted text is sent to the configured AI provider; upload material you are allowed to share.",
    )

    APP_PATH.write_text(text, encoding="utf-8")
    print("enabled Gemini text generation alongside Groq and n8n")


if __name__ == "__main__":
    main()
