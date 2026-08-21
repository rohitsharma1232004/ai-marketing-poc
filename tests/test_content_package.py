import unittest

from content_package import (
    CONTENT_PACKAGE_HEADERS,
    CONTENT_STATUS_APPROVED,
    CONTENT_STATUS_READY,
    GENERATION_HEADERS,
    REEL_SCRIPT_NOT_APPLICABLE,
    apply_content_status,
    normalize_generated_content_row,
    revision_fields_for_row,
    revision_fields_for_rows,
)

PREFIX = "__WEEK_HEADING__:"


class ContentPackageTests(unittest.TestCase):
    def test_generated_reel_row_gets_app_controlled_ready_status(self):
        row = [
            "wrong date",
            "Instagram",
            "Educational",
            "Reel",
            "Three home-buying mistakes",
            "faridabad first home buyer",
            "DM HOME",
            "Buying your first home? Avoid these common mistakes and save this post.",
            "Hook: Buying your first home?; Scene 1: Check budget; Scene 2: Verify location; CTA: DM HOME",
        ]
        normalized = normalize_generated_content_row(row, date_label="Mon, Aug 24")
        self.assertEqual(len(normalized), len(CONTENT_PACKAGE_HEADERS))
        self.assertEqual(normalized[0], "Mon, Aug 24")
        self.assertEqual(normalized[-1], CONTENT_STATUS_READY)

    def test_non_reel_script_is_forced_to_not_applicable(self):
        row = [
            "Mon, Aug 24", "Instagram", "Educational", "Image", "Idea", "keyword",
            "Learn more", "Useful caption", "Model tried to write a script",
        ]
        normalized = normalize_generated_content_row(row, date_label="Mon, Aug 24")
        self.assertEqual(normalized[8], REEL_SCRIPT_NOT_APPLICABLE)

    def test_reel_requires_real_script(self):
        row = [
            "Mon, Aug 24", "Instagram", "Educational", "Reel", "Idea", "keyword",
            "Learn more", "Useful caption", "Not applicable",
        ]
        with self.assertRaises(ValueError):
            normalize_generated_content_row(row, date_label="Mon, Aug 24")

    def test_revision_fields_are_format_aware(self):
        image = [
            "Mon", "Instagram", "Educational", "Image", "Idea", "kw", "CTA",
            "Caption", "Not applicable", CONTENT_STATUS_READY,
        ]
        reel = [
            "Tue", "Instagram", "Educational", "Reel", "Idea", "kw", "CTA",
            "Caption", "Script", CONTENT_STATUS_READY,
        ]
        self.assertNotIn("Reel Script", revision_fields_for_row(CONTENT_PACKAGE_HEADERS, image))
        self.assertIn("Reel Script", revision_fields_for_row(CONTENT_PACKAGE_HEADERS, reel))
        self.assertIn(
            "Reel Script",
            revision_fields_for_rows(CONTENT_PACKAGE_HEADERS, [image, reel]),
        )

    def test_status_override_does_not_mutate_source_rows(self):
        source = [
            [PREFIX + "Week 1"],
            [
                "Mon", "Instagram", "Educational", "Image", "Idea", "kw", "CTA",
                "Caption", "Not applicable", CONTENT_STATUS_READY,
            ],
        ]
        result = apply_content_status(
            CONTENT_PACKAGE_HEADERS,
            source,
            CONTENT_STATUS_APPROVED,
            week_heading_prefix=PREFIX,
        )
        self.assertEqual(result[1][-1], CONTENT_STATUS_APPROVED)
        self.assertEqual(source[1][-1], CONTENT_STATUS_READY)

    def test_generation_header_does_not_let_model_control_status(self):
        self.assertNotIn("Content Status", GENERATION_HEADERS)
        self.assertEqual(CONTENT_PACKAGE_HEADERS[-1], "Content Status")


if __name__ == "__main__":
    unittest.main()
