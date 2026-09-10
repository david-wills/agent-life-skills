"""restaurant-assistant pure logic: price regex and calendar windows."""

from __future__ import annotations

import datetime as dt
import os
import time
import unittest
from unittest import mock

import support

support.add_path("restaurant-assistant", "restaurant-saver", "scripts")

import calendar_lib  # noqa: E402
from enrich_place import PRICE_RE, parse_duration_minutes  # noqa: E402


def price(text: str):
    m = PRICE_RE.search(text)
    return m.group(1) if m else None


class PriceRegex(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(price("Italian · $$ · 4.5"), "$$")
        self.assertEqual(price("Steakhouse\n$$$$\nOpen"), "$$$$")
        self.assertEqual(price("$"), "$")

    def test_ranges(self):
        self.assertEqual(price("Price: $20–30 per person"), "$20–30")
        self.assertEqual(price("about $30-50"), "$30-50")
        self.assertEqual(price("$100+ · Fine dining"), "$100+")
        self.assertEqual(price("$1,000+"), "$1,000+")
        self.assertEqual(price("$20 – $30"), "$20 – $30")

    def test_not_prices(self):
        self.assertIsNone(price("US$ prices vary"))
        self.assertIsNone(price("$$$$$ is not a tier"))
        self.assertIsNone(price("price$$ glued"))
        self.assertIsNone(price("no dollars here"))


class DurationParse(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_duration_minutes("1 hr 5 min"), 65)
        self.assertEqual(parse_duration_minutes("2 hr"), 120)
        self.assertEqual(parse_duration_minutes("25 min"), 25)
        self.assertIsNone(parse_duration_minutes("soon"))
        self.assertIsNone(parse_duration_minutes(None))


def ev(summary, start, end=None, **extra):
    node = lambda v: ({"date": v} if len(v) == 10 else {"dateTime": v})  # noqa: E731
    e = {"summary": summary, "start": node(start)}
    if end:
        e["end"] = node(end)
    e.update(extra)
    return e


@unittest.skipUnless(hasattr(time, "tzset"), "needs POSIX tzset")
class CalendarWindows(unittest.TestCase):
    DAY = dt.date(2025, 9, 11)  # a Thursday

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"TZ": "America/New_York"})
        patcher.start()
        self.addCleanup(patcher.stop)
        time.tzset()
        self.addCleanup(time.tzset)

    def blockers(self, *events):
        return calendar_lib.blockers_for(list(events), self.DAY)

    def test_offset_timestamps_are_compared_in_local_time(self):
        # 23:00Z is 19:00 in New York: inside the dinner window.
        self.assertEqual(self.blockers(ev("Call", "2025-09-11T23:00:00+00:00", "2025-09-12T00:00:00+00:00")),
                         ["Call (7:00pm)"])
        # 21:00Z is 17:00 local, ends 17:30: touches but does not overlap.
        self.assertEqual(self.blockers(ev("Early", "2025-09-11T21:00:00+00:00", "2025-09-11T21:30:00+00:00")), [])

    def test_naive_timestamps_are_local(self):
        self.assertEqual(self.blockers(ev("Dinner", "2025-09-11T18:00:00", "2025-09-11T20:00:00")),
                         ["Dinner (6:00pm)"])
        self.assertEqual(self.blockers(ev("Lunch", "2025-09-11T12:00:00", "2025-09-11T13:00:00")), [])

    def test_spanning_event_blocks_the_day(self):
        self.assertEqual(self.blockers(ev("Trip", "2025-09-10T08:00:00", "2025-09-12T09:00:00")),
                         ["Trip (until fri 9:00am)"])
        self.assertEqual(self.blockers(ev("Shift", "2025-09-10T22:00:00", "2025-09-11T17:00:00")), [])

    def test_all_day_events_are_end_exclusive(self):
        self.assertEqual(self.blockers(ev("Away", "2025-09-11", "2025-09-12")), ["Away (all day)"])
        self.assertEqual(self.blockers(ev("Away", "2025-09-09", "2025-09-12")), ["Away (all day)"])
        self.assertEqual(self.blockers(ev("Gone", "2025-09-09", "2025-09-11")), [])

    def test_cancelled_and_endless_events(self):
        self.assertEqual(self.blockers(ev("Nope", "2025-09-11T19:00:00", status="cancelled")), [])
        # No end: assumed 30 minutes.
        self.assertEqual(self.blockers(ev("Ping", "2025-09-11T17:15:00")), ["Ping (5:15pm)"])
        self.assertEqual(self.blockers(ev("Ping", "2025-09-11T16:45:00")), [])

    def test_reservation_detail(self):
        events = [ev("Reservation at Lucia", "2025-09-11T19:30:00", "2025-09-11T21:00:00"),
                  ev("Dinner at Nowhere", "2025-09-12T19:30:00")]
        found = calendar_lib.reservation_detail(events, self.DAY)
        self.assertEqual((found["name"], found["time"]), ("Lucia", "7:30pm"))
        self.assertEqual(calendar_lib.existing_reservation(events, dt.date(2025, 9, 13)), None)
        for title in ("Resy: Bar Alto", "reso @ Bar Alto", "dinner at Bar Alto "):
            self.assertEqual(calendar_lib.reservation_detail([ev(title, "2025-09-11T20:00:00")], self.DAY)["name"],
                             "Bar Alto")
        self.assertIsNone(calendar_lib.reservation_detail([ev("Bar Alto", "2025-09-11T20:00:00")], self.DAY))

    def test_upcoming_thursdays(self):
        days = calendar_lib.upcoming_thursdays(3, from_date=dt.date(2025, 9, 11))
        self.assertEqual([d.isoformat() for d in days], ["2025-09-18", "2025-09-25", "2025-10-02"])
        self.assertEqual(calendar_lib.upcoming_thursdays(1, from_date=dt.date(2025, 9, 10))[0], dt.date(2025, 9, 11))


if __name__ == "__main__":
    unittest.main()


class StdinIntake(unittest.TestCase):
    def read(self, text: str):
        import io
        import save_restaurant
        return save_restaurant.read_stdin_request(io.StringIO(text))

    def test_full_request(self):
        req, err = self.read('{"query": " Casa Invent ", "note": "a friend\u2019s tip", "source_raw": "$(rm -rf /) `x`", "status": "been"}')
        self.assertIsNone(err)
        self.assertEqual(req["query"], "Casa Invent")
        self.assertEqual(req["status"], "been")
        self.assertEqual(req["source_raw"], "$(rm -rf /) `x`")
        self.assertIsNone(req["source_url"])

    def test_errors_are_codes(self):
        self.assertEqual(self.read("{nope")[1], "bad_stdin_json")
        self.assertEqual(self.read('["Casa"]')[1], "stdin_needs_query")
        self.assertEqual(self.read('{"note": "x"}')[1], "stdin_needs_query")
        self.assertEqual(self.read('{"query": "x", "status": "maybe"}')[1], "bad_status")
        self.assertEqual(self.read('{"query": "x", "watch": true}')[1], "stdin_unknown_field:watch")
