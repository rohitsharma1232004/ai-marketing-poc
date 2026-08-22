"""Harden runtime exception handling after Creative Studio UI is applied.

Run from repository root after apply_creative_studio_ui.py:
    python staging/apply_creative_studio_runtime_hotfix.py

The app-wide PERSISTENCE_EXCEPTIONS constant is already a tuple containing
OSError, TypeError, and ValueError. Nesting that tuple inside another except
tuple is invalid only when the exception path executes, so this patch prevents
a repeat of the earlier runtime-only TypeError.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"


def main() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    if "AI Creative Studio (Gemini)" not in text:
        raise RuntimeError(
            "Creative Studio UI is missing. Run staging/apply_creative_studio_ui.py first."
        )

    replacements = {
        "            except (OSError, PERSISTENCE_EXCEPTIONS) as error:\n":
            "            except PERSISTENCE_EXCEPTIONS as error:\n",
        "                    except (OSError, PERSISTENCE_EXCEPTIONS, TypeError, ValueError) as error:\n":
            "                    except PERSISTENCE_EXCEPTIONS as error:\n",
    }
    changed = False
    for old, new in replacements.items():
        count = text.count(old)
        if count > 1:
            raise RuntimeError(
                "Creative Studio runtime hotfix found an unexpected duplicate exception anchor."
            )
        if count == 1:
            text = text.replace(old, new, 1)
            changed = True

    unsafe_patterns = (
        "(OSError, PERSISTENCE_EXCEPTIONS)",
        "(OSError, PERSISTENCE_EXCEPTIONS, TypeError, ValueError)",
        "(PERSISTENCE_EXCEPTIONS,",
    )
    remaining = [pattern for pattern in unsafe_patterns if pattern in text]
    if remaining:
        raise RuntimeError(
            "Unsafe nested persistence exception tuple remains in app.py: "
            + ", ".join(remaining)
        )

    if changed:
        APP_PATH.write_text(text, encoding="utf-8")
        print("fixed Creative Studio runtime exception handling")
    else:
        print("Creative Studio runtime exception hotfix already applied")
    print("Creative Studio runtime hotfix complete.")


if __name__ == "__main__":
    main()
