"""morning-news fetch_feeds: dedupe, recency window, undated items, feed list loading."""

from __future__ import annotations

import datetime as dt
import importlib.util
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import support

support.add_path("news-triage-assistant", "morning-news", "scripts")

HAVE_DEPS = all(importlib.util.find_spec(m) is not None for m in ("feedparser", "yaml", "dateutil"))
if HAVE_DEPS:
    import fetch_feeds as ff


def item(title, url, source="a", section="world", published=None, summary=""):
    return ff.Item(title=title, url=url, source=source, section=section,
                   published_utc=published, summary=summary)


def ago(hours: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()


RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>T</title>
<item><title>Fed &amp; markets: <b>rates</b> hold</title><link>https://x.io/1</link>
  <pubDate>Thu, 11 Sep 2025 12:00:00 GMT</pubDate><description>{summary}</description></item>
<item><title>No link here</title><description>skipped</description></item>
<item><title></title><link>https://x.io/3</link></item>
<item><title>Undated</title><link>https://x.io/4</link></item>
</channel></rss>"""


@unittest.skipUnless(HAVE_DEPS, "needs morning-news/requirements.txt (feedparser, PyYAML, python-dateutil)")
class TextAndDates(unittest.TestCase):
    def test_clean_text(self):
        self.assertEqual(ff.clean_text("<p>Hello &amp;  <b>world</b></p>\n"), "Hello & world")
        self.assertEqual(ff.clean_text(""), "")

    def test_normalize_title(self):
        self.assertEqual(ff.normalize_title("Fed Raises Rates!  (Again)"), "fed raises rates again")

    def test_parse_date(self):
        self.assertEqual(ff.parse_date({"published": "Thu, 11 Sep 2025 12:00:00 -0400"}),
                         dt.datetime(2025, 9, 11, 16, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(ff.parse_date({"updated": "2025-09-11T12:00:00"}).tzinfo, dt.timezone.utc)
        self.assertIsNone(ff.parse_date({"published": "not a date"}))
        self.assertIsNone(ff.parse_date({}))


@unittest.skipUnless(HAVE_DEPS, "needs morning-news/requirements.txt")
class Dedupe(unittest.TestCase):
    PRIORITY = {"wire": 0, "paper": 1}

    def test_same_url_keeps_higher_priority_source(self):
        out = ff.dedupe([item("Story", "https://x.io/1", source="paper"),
                         item("Story", "https://x.io/1", source="wire")], self.PRIORITY)
        self.assertEqual([(i.url, i.source) for i in out], [("https://x.io/1", "wire")])

    def test_near_duplicate_titles_collapse(self):
        out = ff.dedupe([item("Fed raises rates!", "https://a.io/1", source="paper"),
                         item("FED  Raises Rates", "https://b.io/2", source="wire"),
                         item("Other story", "https://c.io/3", source="blog")], self.PRIORITY)
        self.assertEqual([(i.source, i.url) for i in out],
                         [("wire", "https://b.io/2"), ("blog", "https://c.io/3")])

    def test_unranked_source_sorts_last_and_by_date_within_rank(self):
        out = ff.dedupe([item("Z", "u1", source="blog", published="2025-09-11T10:00:00+00:00"),
                         item("Y", "u2", source="wire", published="2025-09-11T12:00:00+00:00"),
                         item("X", "u3", source="wire", published="2025-09-11T09:00:00+00:00")], self.PRIORITY)
        self.assertEqual([i.title for i in out], ["X", "Y", "Z"])

    def test_blank_title_dropped(self):
        self.assertEqual(ff.dedupe([item("!!!", "u1")], {}), [])


@unittest.skipUnless(HAVE_DEPS, "needs morning-news/requirements.txt")
class RecencyWindow(unittest.TestCase):
    def test_window_keeps_recent_and_undated(self):
        items = [item("new", "u1", published=ago(2)),
                 item("old", "u2", published=ago(30)),
                 item("undated", "u3"),
                 item("garbage date", "u4", published="yesterday-ish")]
        kept, undated = ff.filter_recent(items, hours=24)
        self.assertEqual([i.title for i in kept], ["new", "undated", "garbage date"])
        self.assertEqual(undated, 2)

    def test_window_edge(self):
        kept, _ = ff.filter_recent([item("edge", "u", published=ago(23.9))], hours=24)
        self.assertEqual(len(kept), 1)


@unittest.skipUnless(HAVE_DEPS, "needs morning-news/requirements.txt")
class FeedList(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "feeds.yaml"

    def test_loads_feeds_and_priority(self):
        self.path.write_text("priority: [wire, paper]\nfeeds:\n"
                             "  - {source: wire, section: world, url: https://w.io/rss}\n"
                             "  - {source: paper, section: tech, url: https://p.io/rss}\n")
        feeds, priority = ff.load_feeds(self.path)
        self.assertEqual([f["source"] for f in feeds], ["wire", "paper"])
        self.assertEqual(priority, {"wire": 0, "paper": 1})

    def test_missing_file_names_the_example(self):
        with self.assertRaises(SystemExit) as cm:
            ff.load_feeds(self.path)
        self.assertIn("feed list not found", str(cm.exception))
        self.assertIn("feeds.example.yaml", str(cm.exception))

    def test_missing_key_and_empty_list(self):
        self.path.write_text("feeds:\n  - {source: wire, url: https://w.io/rss}\n")
        with self.assertRaises(SystemExit) as cm:
            ff.load_feeds(self.path)
        self.assertIn("missing 'section'", str(cm.exception))
        self.path.write_text("feeds: []\n")
        with self.assertRaises(SystemExit):
            ff.load_feeds(self.path)

    def test_example_feed_list_is_valid(self):
        feeds, _ = ff.load_feeds(ff.EXAMPLE_FEEDS_FILE)
        self.assertGreater(len(feeds), 0)


class _Parsed:
    """Minimal stand-in for feedparser's result object."""

    def __init__(self, entries=(), status=None, bozo=False, exc=None):
        self.entries = list(entries)
        self._d = {"status": status}
        self.bozo = bozo
        self.bozo_exception = exc

    def get(self, key, default=None):
        return self._d.get(key, default)


@unittest.skipUnless(HAVE_DEPS, "needs morning-news/requirements.txt")
class FetchOne(unittest.TestCase):
    CFG = {"source": "wire", "section": "world", "url": "https://w.io/rss"}

    def test_parses_entries_and_skips_incomplete_ones(self):
        long_summary = "word " * 200
        real_parse = ff.feedparser.parse
        with mock.patch.object(ff.feedparser, "parse", lambda url: real_parse(RSS.format(summary=long_summary))):
            items = ff.fetch_one(self.CFG)
        self.assertEqual([i.title for i in items], ["Fed & markets: rates hold", "Undated"])
        self.assertEqual(items[0].published_utc, "2025-09-11T12:00:00+00:00")
        self.assertIsNone(items[1].published_utc)
        self.assertLessEqual(len(items[0].summary), 601)
        self.assertTrue(items[0].summary.endswith("…"))
        self.assertEqual((items[0].source, items[0].section), ("wire", "world"))

    def test_http_error_and_bozo_are_errors(self):
        with mock.patch.object(ff.feedparser, "parse", lambda url: _Parsed(status=404)):
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                ff.fetch_one(self.CFG)
        with mock.patch.object(ff.feedparser, "parse",
                               lambda url: _Parsed(bozo=True, exc=ValueError("bad xml"))):
            with self.assertRaisesRegex(RuntimeError, "ValueError: bad xml"):
                ff.fetch_one(self.CFG)


@unittest.skipUnless(HAVE_DEPS, "needs morning-news/requirements.txt")
class FetchAll(unittest.TestCase):
    def test_hung_and_failing_feeds_become_errors(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(socket.setdefaulttimeout, None)

        def fake_fetch(cfg):
            if cfg["source"] == "ok":
                return [item("fine", "u1", source="ok")]
            if cfg["source"] == "bad":
                raise RuntimeError("HTTP 500")
            release.wait(10)
            return []

        feeds = [{"source": s, "section": "x", "url": f"https://{s}.io"} for s in ("ok", "bad", "hung")]
        with mock.patch.object(ff, "fetch_one", fake_fetch):
            items, errors = ff.fetch_all(feeds, timeout_s=1)
        self.assertEqual([i.title for i in items], ["fine"])
        self.assertEqual({e["source"]: e["error"] for e in errors},
                         {"bad": "RuntimeError: HTTP 500", "hung": "TimeoutError: no response within 1s"})

    def test_empty_feed_list(self):
        self.assertEqual(ff.fetch_all([]), ([], []))


if __name__ == "__main__":
    unittest.main()
