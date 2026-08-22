"""Show safe Groq rate-limit details in the app instead of a generic 429.

This patch only changes the direct Groq 429 error message. It extracts safe
quota details such as TPM/RPM, Limit, Used, Requested, and Retry-After when
Groq provides them. API keys and authorization headers are never displayed.

Run from repository root:
    python staging/apply_groq_rate_limit_diagnostics.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_PATH = ROOT / "generation_providers.py"

OLD_BLOCK = '''    if status_code == 429:\n        raise GenerationProviderError(\n            "Groq rate limit reached. Please try again later.",\n            request_id=request_id,\n            code="GROQ_RATE_LIMIT",\n            retryable=True,\n        )\n'''

NEW_BLOCK = '''    if status_code == 429:\n        import re\n\n        safe_parts: list[str] = []\n        try:\n            rate_data = response.json()\n            if isinstance(rate_data, Mapping):\n                error_data = rate_data.get("error")\n                if isinstance(error_data, Mapping):\n                    raw_message = error_data.get("message")\n                    if isinstance(raw_message, str):\n                        dimension_match = re.search(r"\\b(TPM|TPD|RPM|RPD)\\b", raw_message)\n                        if dimension_match:\n                            safe_parts.append(dimension_match.group(1))\n                        for label in ("Limit", "Used", "Requested"):\n                            match = re.search(rf"{label}\\s*[:=]?\\s*([0-9,]+)", raw_message, re.IGNORECASE)\n                            if match:\n                                safe_parts.append(f"{label}: {match.group(1)}")\n        except (ValueError, TypeError, AttributeError):\n            pass\n\n        response_headers = getattr(response, "headers", {}) or {}\n        retry_after = response_headers.get("retry-after") or response_headers.get("Retry-After")\n        token_reset = response_headers.get("x-ratelimit-reset-tokens")\n        if retry_after:\n            safe_parts.append(f"Retry after: {retry_after}")\n        elif token_reset:\n            safe_parts.append(f"Token reset: {token_reset}")\n\n        details = f" ({'; '.join(safe_parts)})" if safe_parts else ""\n        raise GenerationProviderError(\n            f"Groq rate limit reached{details}. Please try again later.",\n            request_id=request_id,\n            code="GROQ_RATE_LIMIT",\n            retryable=True,\n        )\n'''


def main() -> None:
    text = PROVIDER_PATH.read_text(encoding="utf-8")
    if NEW_BLOCK in text and OLD_BLOCK not in text:
        print("Groq rate-limit diagnostics already applied")
        return
    count = text.count(OLD_BLOCK)
    if count != 1:
        raise RuntimeError(f"Expected one Groq 429 block, found {count}")
    PROVIDER_PATH.write_text(text.replace(OLD_BLOCK, NEW_BLOCK, 1), encoding="utf-8")
    print("Groq rate-limit diagnostics applied.")


if __name__ == "__main__":
    main()
