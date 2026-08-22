"""Lower direct Groq completion budget to better fit the 8K TPM free-tier limit.

This patch changes the direct Groq request from 8192 max completion tokens to
3500 and updates the matching unit-test expectation.

Run from repository root:
    python staging/apply_groq_free_tier_token_cap.py
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_PATH = ROOT / "generation_providers.py"
TEST_PATH = ROOT / "tests" / "test_generation_providers.py"

OLD_PROVIDER = '        "max_completion_tokens": 8192,\n'
NEW_PROVIDER = '        "max_completion_tokens": 3500,\n'
OLD_TEST = '        self.assertEqual(call["json"]["max_completion_tokens"], 8192)\n'
NEW_TEST = '        self.assertEqual(call["json"]["max_completion_tokens"], 3500)\n'


def replace_once(path: Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if new in text and old not in text:
        print(f"{label} already applied")
        return
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one anchor, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"{label} applied")


def main() -> None:
    replace_once(
        PROVIDER_PATH,
        OLD_PROVIDER,
        NEW_PROVIDER,
        "Groq 3500-token completion cap",
    )
    replace_once(
        TEST_PATH,
        OLD_TEST,
        NEW_TEST,
        "Groq token-cap test update",
    )
    print("Direct Groq max_completion_tokens is now 3500.")


if __name__ == "__main__":
    main()
