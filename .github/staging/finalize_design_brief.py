from pathlib import Path


def require(path, marker):
    text = Path(path).read_text(encoding="utf-8")
    if marker not in text:
        raise RuntimeError(f"Required transformed marker missing in {path}: {marker!r}")


def replace_once(path, old, new):
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one match in {path}, found {count}: {old[:90]!r}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


for path, marker in (
    ("campaign_store.py", "def save_design_briefs("),
    ("campaign_store.py", "_ensure_v7_design_brief_schema"),
    ("app.py", "from design_brief import ("),
    ("app.py", "Design Brief Generator"),
    ("app.py", "design_status_by_post"),
):
    require(path, marker)

replace_once(
    "APPROVAL_WORKFLOW.md",
    '       -> Approve -> Excel download unlocked\n',
    '       -> Approve -> Excel download + Design Brief Generator unlocked\n',
)
replace_once(
    "APPROVAL_WORKFLOW.md",
    '- Excel export is allowed only when the latest version has a matching Senior `approved` decision. The exported content package displays `Senior Approved` status.\n',
    '- Excel export is allowed only when the latest version has a matching Senior `approved` decision. The exported content package displays `Senior Approved` status.\n'
    '- Design briefs can be generated only for the latest hash-matched, finally Senior-approved content version.\n'
    '- Design briefs are stored separately from approved content, so creative instructions cannot rewrite the approved package.\n'
    '- The dashboard derives `Design Status` as Locked, Not Generated, or Design Brief Ready without changing the approved content hash.\n',
)
replace_once(
    "DEVELOPMENT_ROADMAP.md",
    'Status: **Phase 1 complete; further content types planned**\n',
    'Status: **Phase 2 in validation — captions/reel scripts complete; Senior-approved Design Brief Generator implemented**\n',
)
replace_once(
    "DEVELOPMENT_ROADMAP.md",
    '''Next additions within this milestone:

- Carousel slide copy.
- Design briefs and platform-specific variants.
- Automatic brand, claim, duplication and platform QA.
''',
    '''Completed in Phase 2 / local validation:

- Added format-aware Design Brief generation for Image, Carousel, Reel, Video, and Story posts.
- Design Brief generation unlocks only after final Senior approval of the latest hash-matched content version.
- Design Briefs are stored separately from immutable approved content and shown in per-post expanders.
- Added derived `Design Status` without changing the approved content hash.

Next additions within this milestone:

- Carousel slide copy as a first-class content field.
- Platform-specific creative variants.
- Automatic brand, claim, duplication and platform QA.
''',
)
replace_once(
    "DEVELOPMENT_ROADMAP.md",
    '- Generate complete design briefs from the approved content package.\n- Start with Canva handoff rather than automatic publishing.\n',
    '- Use the approved Design Briefs as the creative-production contract.\n- Start with Canva/manual designer handoff rather than automatic publishing.\n',
)

print("Design Brief finalization checks and documentation updates applied successfully.")
