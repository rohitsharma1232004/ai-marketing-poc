import unittest

from content_package import CONTENT_PACKAGE_HEADERS, CONTENT_STATUS_READY
from revision_logic import (
    REVISION_FIELDS,
    build_field_revision_prompt,
    build_selected_post_revision_prompt,
    list_reviewable_posts,
    merge_revised_fields,
    merge_revised_post,
    normalize_revision_fields,
)


PREFIX = "__WEEK_HEADING__:"
ROWS = [
    [PREFIX + "Week 1"],
    ["20 Aug 2026", "Instagram", "Educational", "Reel", "Old idea", "old keyword", "Old CTA"],
    ["22 Aug 2026", "Facebook", "Brand Awareness", "Image", "Second idea", "second keyword", "Second CTA"],
]
HEADERS = [
    "Date", "Platform", "Pillar", "Format", "Content Idea", "SEO Keyword Focus", "CTA"
]
PACKAGE_ROWS = [
    [PREFIX + "Week 1"],
    [
        "20 Aug 2026", "Instagram", "Educational", "Reel", "Old idea", "old keyword",
        "Old CTA", "Old caption", "Old reel script", CONTENT_STATUS_READY,
    ],
    [
        "22 Aug 2026", "Facebook", "Brand Awareness", "Image", "Second idea",
        "second keyword", "Second CTA", "Second caption", "Not applicable",
        CONTENT_STATUS_READY,
    ],
]


class RevisionLogicTests(unittest.TestCase):
    def test_list_reviewable_posts_skips_week_headings_and_keeps_row_index(self):
        posts = list_reviewable_posts(ROWS, week_heading_prefix=PREFIX)
        self.assertEqual([item["post_number"] for item in posts], [1, 2])
        self.assertEqual([item["row_index"] for item in posts], [1, 2])
        self.assertIn("Old idea", posts[0]["label"])

        package_posts = list_reviewable_posts(PACKAGE_ROWS, week_heading_prefix=PREFIX)
        self.assertEqual([item["row_index"] for item in package_posts], [1, 2])

    def test_merge_revised_post_preserves_schedule_and_mix_columns(self):
        merged = merge_revised_post(
            ROWS,
            row_index=1,
            revised_row=[
                "wrong date", "Wrong platform", "Wrong pillar", "Wrong format",
                "Better idea", "better keyword", "Better CTA",
            ],
        )
        self.assertEqual(merged[1][:4], ROWS[1][:4])
        self.assertEqual(
            merged[1][4:], ["Better idea", "better keyword", "Better CTA"]
        )
        self.assertEqual(merged[2], ROWS[2])

    def test_revision_prompt_requires_feedback_and_requests_one_row(self):
        prompt = build_selected_post_revision_prompt(
            headers=HEADERS,
            current_row=ROWS[1],
            senior_feedback="Make the CTA stronger.",
            client_metadata={"client_name": "ABC Realty", "tone": "Professional"},
            campaign_intake={"goal": "Lead generation", "language": "English"},
        )
        self.assertIn("Make the CTA stronger.", prompt)
        self.assertIn("exactly 1 content row(s)", prompt)

        with self.assertRaises(ValueError):
            build_selected_post_revision_prompt(
                headers=HEADERS,
                current_row=ROWS[1],
                senior_feedback="",
            )

    def test_seo_only_merge_changes_only_seo_field(self):
        revised = [[
            "wrong date", "wrong platform", "wrong pillar", "wrong format",
            "wrong idea", "faridabad first home buyer", "wrong cta",
        ]]
        merged = merge_revised_fields(
            ROWS,
            target_row_indices=[1],
            revised_rows=revised,
            fields_to_change=["SEO Keyword Focus"],
        )
        self.assertEqual(merged[1][:5], ROWS[1][:5])
        self.assertEqual(merged[1][5], "faridabad first home buyer")
        self.assertEqual(merged[1][6], ROWS[1][6])
        self.assertEqual(merged[2], ROWS[2])

    def test_whole_calendar_cta_merge_preserves_all_other_fields(self):
        revised = [
            [*ROWS[1][:6], "Lead CTA 1"],
            [*ROWS[2][:6], "Lead CTA 2"],
        ]
        merged = merge_revised_fields(
            ROWS,
            target_row_indices=[1, 2],
            revised_rows=revised,
            fields_to_change=["CTA"],
        )
        self.assertEqual(merged[1][:6], ROWS[1][:6])
        self.assertEqual(merged[2][:6], ROWS[2][:6])
        self.assertEqual(merged[1][6], "Lead CTA 1")
        self.assertEqual(merged[2][6], "Lead CTA 2")

    def test_field_prompt_includes_senior_and_team_instructions(self):
        prompt = build_field_revision_prompt(
            headers=HEADERS,
            current_rows=[ROWS[1]],
            fields_to_change=["SEO Keyword Focus"],
            senior_feedback="Use buyer-intent keywords only.",
            user_instructions="Include Sector 88 where relevant.",
        )
        self.assertIn("SEO Keyword Focus", prompt)
        self.assertIn("Use buyer-intent keywords only.", prompt)
        self.assertIn("Include Sector 88 where relevant.", prompt)
        self.assertIn("Change ONLY these field(s): SEO Keyword Focus", prompt)

    def test_package_caption_only_revision_preserves_script_and_status(self):
        revised = [[
            "wrong date", "wrong platform", "wrong pillar", "wrong format", "wrong idea",
            "wrong keyword", "wrong cta", "Better caption", "Wrong script", "Wrong status",
        ]]
        merged = merge_revised_fields(
            PACKAGE_ROWS,
            target_row_indices=[1],
            revised_rows=revised,
            fields_to_change=["Caption"],
            headers=CONTENT_PACKAGE_HEADERS,
        )
        self.assertEqual(merged[1][7], "Better caption")
        self.assertEqual(merged[1][8], PACKAGE_ROWS[1][8])
        self.assertEqual(merged[1][9], CONTENT_STATUS_READY)
        self.assertEqual(merged[1][:7], PACKAGE_ROWS[1][:7])

    def test_non_reel_script_is_preserved_even_for_whole_calendar_script_revision(self):
        revised = [
            [*PACKAGE_ROWS[1][:8], "Better reel script", "Wrong status"],
            [*PACKAGE_ROWS[2][:8], "Model invented image script", "Wrong status"],
        ]
        merged = merge_revised_fields(
            PACKAGE_ROWS,
            target_row_indices=[1, 2],
            revised_rows=revised,
            fields_to_change=["Reel Script"],
            headers=CONTENT_PACKAGE_HEADERS,
        )
        self.assertEqual(merged[1][8], "Better reel script")
        self.assertEqual(merged[2][8], "Not applicable")
        self.assertEqual(merged[1][9], CONTENT_STATUS_READY)
        self.assertEqual(merged[2][9], CONTENT_STATUS_READY)

    def test_package_prompt_keeps_status_immutable(self):
        prompt = build_field_revision_prompt(
            headers=CONTENT_PACKAGE_HEADERS,
            current_rows=[PACKAGE_ROWS[1]],
            fields_to_change=["Caption", "Reel Script"],
            senior_feedback="Make the hook sharper.",
        )
        self.assertIn("Content Status", prompt)
        self.assertIn("Change Reel Script only for rows whose Format is Reel or Video", prompt)

    def test_legacy_calendar_rejects_new_field_revision(self):
        with self.assertRaises(ValueError):
            build_field_revision_prompt(
                headers=HEADERS,
                current_rows=[ROWS[1]],
                fields_to_change=["Caption"],
                senior_feedback="Rewrite it.",
            )

    def test_revision_fields_are_restricted(self):
        self.assertEqual(normalize_revision_fields(list(REVISION_FIELDS)), REVISION_FIELDS)
        self.assertIn("Caption", REVISION_FIELDS)
        self.assertIn("Reel Script", REVISION_FIELDS)
        with self.assertRaises(ValueError):
            normalize_revision_fields(["Platform"])
        with self.assertRaises(ValueError):
            normalize_revision_fields([])


if __name__ == "__main__":
    unittest.main()
