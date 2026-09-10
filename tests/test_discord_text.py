"""Pure text helpers in lib/discord.py."""

from __future__ import annotations

import unittest

import support  # noqa: F401  (puts lib/ on sys.path)

from discord import normalize_markdown, split_for_discord, strip_variation_selectors


class SplitForDiscord(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(split_for_discord("hello"), ["hello"])
        self.assertEqual(split_for_discord(""), [""])
        self.assertEqual(split_for_discord(None), [""])

    def test_splits_at_paragraphs_first(self):
        paras = ["a" * 700, "b" * 700, "c" * 700]
        chunks = split_for_discord("\n\n".join(paras), limit=1500)
        self.assertEqual(chunks, ["a" * 700 + "\n\n" + "b" * 700, "c" * 700])

    def test_splits_long_paragraph_at_lines(self):
        lines = ["x" * 400, "y" * 400, "z" * 400]
        chunks = split_for_discord("\n".join(lines), limit=900)
        self.assertEqual(chunks, ["x" * 400 + "\n" + "y" * 400, "z" * 400])

    def test_hard_splits_a_single_overlong_line(self):
        chunks = split_for_discord("q" * 2500, limit=1000)
        self.assertEqual([len(c) for c in chunks], [1000, 1000, 500])

    def test_never_truncates_and_respects_limit(self):
        text = "\n\n".join(
            ("para %d " % i) * (7 * (i + 1)) + "\nline\n" + "w" * (300 * i) for i in range(8)
        )
        chunks = split_for_discord(text, limit=500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("".join(c.replace("\n", "") for c in chunks), text.replace("\n", ""))


class NormalizeMarkdown(unittest.TestCase):
    def test_links_bold_and_emoji(self):
        self.assertEqual(normalize_markdown("see <https://x.io|the site>"), "see [the site](https://x.io)")
        self.assertEqual(normalize_markdown("<https://x.io>"), "https://x.io")
        self.assertEqual(normalize_markdown("a *bold* word"), "a **bold** word")
        self.assertEqual(normalize_markdown("**already** bold"), "**already** bold")
        self.assertEqual(normalize_markdown("ok :white_check_mark:"), "ok ✅")
        self.assertEqual(normalize_markdown("2 * 3 * 4"), "2 * 3 * 4")

    def test_empty_passthrough(self):
        self.assertEqual(normalize_markdown(""), "")
        self.assertIsNone(normalize_markdown(None))


class StripVariationSelectors(unittest.TestCase):
    def test_both_forms_compare_equal(self):
        self.assertEqual(strip_variation_selectors("🗑️"), strip_variation_selectors("🗑"))
        self.assertEqual(strip_variation_selectors("✅️"), "✅")
        self.assertEqual(strip_variation_selectors(None), "")


if __name__ == "__main__":
    unittest.main()
