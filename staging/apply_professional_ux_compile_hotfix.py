"""Fix the generated Technical details string from the professional UX transform."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"


def main() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    fixed = 'f"Error code: {error.code}\\nRequest ID: {error.request_id}",'
    if fixed in text:
        print("professional UX compile hotfix already applied")
        return

    broken = 'f"Error code: {error.code}\nRequest ID: {error.request_id}",'
    count = text.count(broken)
    if count != 1:
        raise RuntimeError(
            f"professional UX technical-details anchor: expected one match, found {count}"
        )
    APP_PATH.write_text(text.replace(broken, fixed, 1), encoding="utf-8")
    print("professional UX compile hotfix applied")


if __name__ == "__main__":
    main()
