import unittest

from revision_logic import (
    build_selected_post_revision_prompt,
    list_reviewable_posts,
    merge_revised_post,
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


class RevisionLogicTests(unittest.TestCase):
    def test_list_reviewable_posts_skips_week_headings_and_keeps_row_index(self):
        posts = list_reviewable_posts(ROWS, week_heading_prefix=PREFIX)
        self.assertEqual([item["post_number"] for item in posts], [1, 2])
        self.assertEqual([item["row_index"] for item in posts], [1, 2])
        self.assertIn("Old idea", posts[0]["label"])

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
        self.assertIn("Keep Date, Platform, Pillar, and Format unchanged", prompt)
        self.assertIn("exactly one content row", prompt)

        with self.assertRaises(ValueError):
            build_selected_post_revision_prompt(
                headers=HEADERS,
                current_row=ROWS[1],
                senior_feedback="",
            )


if __name__ == "__main__":
    unittest.main()
