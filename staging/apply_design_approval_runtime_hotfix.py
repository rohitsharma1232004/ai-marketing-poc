"""Fix runtime/UX issues in the already-applied Senior Design Approval UI.

Run from the repository root after the Senior Design Approval feature is present:
    python staging/apply_design_approval_runtime_hotfix.py

The patch is idempotent. It fixes a nested exception tuple that only fails at
runtime, and makes the creative uploader version-aware so a previously uploaded
file is not accidentally resubmitted after Streamlit reruns.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"


def replace_once_if_present(text: str, old: str, new: str, label: str) -> tuple[str, bool]:
    count = text.count(old)
    if count == 0:
        return text, False
    if count != 1:
        raise RuntimeError(f"{label}: expected at most one anchor, found {count}.")
    return text.replace(old, new, 1), True


def main() -> None:
    text = APP_PATH.read_text(encoding="utf-8")
    if "def render_creative_asset_controls(" not in text:
        raise RuntimeError(
            "app.py does not contain the Senior Design Approval feature. "
            "Apply that feature before running this hotfix."
        )

    changed = False

    # PERSISTENCE_EXCEPTIONS is already a tuple containing OSError. Nesting that
    # tuple inside another except tuple causes TypeError only when an exception is
    # actually raised, so py_compile cannot detect it.
    text, did_change = replace_once_if_present(
        text,
        "        except (OSError, PERSISTENCE_EXCEPTIONS) as error:\n",
        "        except PERSISTENCE_EXCEPTIONS as error:\n",
        "creative save exception handling",
    )
    changed = changed or did_change

    # Streamlit retains a file_uploader value across reruns when the widget key is
    # unchanged. Scope upload/save widgets to the latest creative version so a
    # successful save or replacement starts with a clean uploader automatically.
    text, did_change = replace_once_if_present(
        text,
        "        key=f\"creative_upload_{calendar['id']}_{post_number}\",\n",
        "        key=f\"creative_upload_{calendar['id']}_{post_number}_{latest_asset['asset_version'] if latest_asset else 0}\",\n",
        "version-aware creative uploader key",
    )
    changed = changed or did_change

    text, did_change = replace_once_if_present(
        text,
        "        key=f\"save_creative_{calendar['id']}_{post_number}\",\n",
        "        key=f\"save_creative_{calendar['id']}_{post_number}_{latest_asset['asset_version'] if latest_asset else 0}\",\n",
        "version-aware creative save button key",
    )
    changed = changed or did_change

    if "except (OSError, PERSISTENCE_EXCEPTIONS)" in text:
        raise RuntimeError("Unsafe nested persistence exception tuple still remains in app.py.")

    expected_upload_fragment = (
        "creative_upload_{calendar['id']}_{post_number}_"
        "{latest_asset['asset_version'] if latest_asset else 0}"
    )
    expected_save_fragment = (
        "save_creative_{calendar['id']}_{post_number}_"
        "{latest_asset['asset_version'] if latest_asset else 0}"
    )
    if expected_upload_fragment not in text or expected_save_fragment not in text:
        raise RuntimeError("Creative upload widgets are not safely version-scoped.")

    if changed:
        APP_PATH.write_text(text, encoding="utf-8")
        print("fixed app.py design approval runtime handling")
    else:
        print("app.py design approval runtime hotfix already applied")

    print("Design approval runtime hotfix complete.")


if __name__ == "__main__":
    main()
