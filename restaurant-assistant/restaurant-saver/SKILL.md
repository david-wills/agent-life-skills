---
name: restaurant-saver
description: A restaurant pipeline, backed by one SQLite database. (1) INTAKE — anything dropped in Discord #restaurants (`discord.channels.restaurants` in config.json), no @mention needed: a name, a Maps link, an article URL, with or without a note. Resolved, enriched with drive time from home + reservation platform + booking URL, and stored. (2) PLAN — Wednesday cron finds free Thursdays on the shared household calendar and posts 3–5 suggestion cards with a ✅ affordance; an hourly sweep turns ✅ into a booking intent. (3) BRIEF — day-of cron spots a reservation on the calendar and posts what to order. Use when the user drops a restaurant, asks what's on their list, asks about free Thursdays, or when tuning any of the three crons. Does NOT book without the user's ✅. Does NOT delete places.
---

# Restaurant pipeline

## The shape of it

One database, three doors. `restaurant-saver/restaurants.db` is the product;
the skills are ways in and out of it.

| Door | Trigger | Entry point | Cron |
|------|---------|-------------|------|
| Intake | Message in `#restaurants` | `save_restaurant.py` | — |
| Plan | Wed 9:20 AM PT | `plan_thursday.py` → `post_suggestions.py` | `restaurant-thursday-planner` |
| Sweep | Hourly, 9 AM–10 PM PT | `sweep_reservation_reactions.py` | `restaurant-reservation-sweep` |
| Brief | Daily 9:40 AM PT | `todays_reservation.py` | `restaurant-day-of-brief` |

All three crons are isolated, silent on success, and alert to `#errors-alerts`
(`discord.channels.errors`). Times are staggered off the busy `:00`/`:03` minutes
to avoid isolated-agent boot contention.

Channel: Discord `#restaurants` (`discord.channels.restaurants`, AI Team guild).
**No @mention required** — every non-bot message in that channel is intake.

## Intake

The user drops one of: a restaurant name, a Google Maps link, an article URL, or
any of those plus a note ("Bestia — a friend's been asking", "went here, get the
branzino"). Parse intent, then run one command:

```bash
PY=skills/restaurant-saver/.venv/bin/python
$PY skills/restaurant-saver/scripts/save_restaurant.py "<name|url>" \
    --note "<why they saved it>" --source-raw "<their raw message>"
```

Intent → `--status`:
- default → `want_to_go`
- "I went to X" / "ate at X" / "had dinner at X" / "we tried X" → `been`
- explicit "favorite" → `favorite`

Output shapes:
- `{"kind":"saved","created":true,"restaurant":{...}}` — confirm in channel.
- `{"kind":"candidates","candidates":[...]}` — post a numbered list, wait for their pick, re-run with the chosen Maps URL.
- `{"kind":"error","error":"..."}` — see error table below.

Reply with name, cuisine, drive time, and the booking link when one was found:

> Saved **Bestia** — Italian · 4.6★ · 18 min away. Books on [Resy](<...>). Added to your list.

One save takes ~40–60s (resolve + enrich + drive time in a single browser
session). Say nothing while it runs; just answer when it lands.

### What gets stored

Beyond name/address/coords: `cuisine`, `price`, `rating`, `note` (why they saved
it), `source_url`, `drive_minutes` + `drive_text` (from `user.home`),
`reservation_platform`, `reservation_url`, `added_at`, `status`.

`upsert_restaurant` never blanks an enriched field with an empty one, and
appends to `note` rather than clobbering it — a later bare mention of a place
must not erase why they saved it the first time.

## Plan (Wednesday)

```bash
python3 skills/restaurant-saver/scripts/plan_thursday.py --weeks 4 --limit 8
```

Returns `{thursdays:[{date,label,free,blockers,reservation}], candidates:[...]}`.
A Thursday is free when nothing overlaps 5:30–10:00pm and there's no all-day
event. Candidates are scored for bookability, rating, drive time, whether
the user wrote a note, and *against* anything suggested in the last 45 days;
the shortlist is capped at 2 per cuisine so it isn't five Italian places.

**You pick the final 3–5 and write the blurbs.** The scorer is a shovel, not
a critic — it can't tell that they had pasta twice this month.

Two rules the scorer does not encode, learned from the first live run:
- **Platform priority beats score.** Resy and Tock are auto-bookable; OpenTable
  is not. At most one OpenTable pick per week, never the majority. The first
  run offered 3 OpenTable out of 4 because the prompt said "prefer bookable"
  when it meant "prefer *automatable*".
- **An empty blurb is correct.** The card already prints cuisine, price, rating
  and drive time. A blurb that restates them is filler — that run produced
  "Indian on Fairfax, 4.4 rating, $40-50 a head, about 19 minutes out" directly
  under a line saying exactly that. Say something new or say nothing.

Then:

```bash
echo '{"date":"2026-09-10","intro":"...","picks":[{"id":3,"blurb":"..."}]}' \
  | python3 skills/restaurant-saver/scripts/post_suggestions.py
```

One card per restaurant, each seeded with ✅. Reactions are per-message, so a
batched digest would make the whole book-this contract impossible — never
consolidate the cards.

## Sweep (hourly)

```bash
python3 skills/restaurant-saver/scripts/sweep_reservation_reactions.py
```

Reads ✅ / ❌ on open suggestion cards and returns a `to_book` work list.
**The sweep never books.** Booking is external and hard to reverse, so it
stays behind the user's ✅ *and* a deliberate booking step.

Guards that are load-bearing:
- ✅ and ❌ on the same card → left `offered`, never guessed.
- Two ✅ for the same date → reported in `conflicts`, nothing booked. Ask which.
- The bot's own seed reaction must stay at exactly one; never react twice from the bot, or `by_user` breaks.

Booking runs as `accounts.shopping` in a **separate** browser profile
(`booking-profile/`) from the Maps profile (`chrome-profile/`, which is
`accounts.personal`). Do not merge them. One-time sign-in:

```bash
$PY skills/restaurant-saver/scripts/login_booking.py
```

Default booking target: party of 2, 7:00–7:30pm.

### Platform coverage

| Platform | Automatable | Why |
|----------|-------------|-----|
| Resy | yes | 200 headless, venue pages load |
| Tock | yes | 200 headless |
| OpenTable | **no** | Akamai detects the CDP-driven session and 403s it |

OpenTable blocks the *automation*, not the account or the IP — the user is signed
in (there's a real `authCke` cookie in the profile) and Resy/Tock work from the
same machine. Playwright drives Chrome over a debug pipe, Akamai spots it and
poisons `_abck`. A visible window doesn't help; neither does a correct UA.

Two known ways around it, neither built:
- **Claude in Chrome** — an extension inside the user's real Chrome, so no debug
  pipe. Almost certainly defeats the block, but it's sidebar-driven; there's no
  supported way for a cron to trigger it.
- **Keyboard Maestro** — OS-level clicks into real Chrome, no CDP. The pattern
  the FB Reel publisher already uses. The only option that's both unattended
  and unblocked.

Reserve-with-Google is a dead end: the OpenTable links Maps returns carry no
`rwg_token`, while the Resy and Tock ones do.

Until one of those is built, OpenTable picks degrade to a prepared link.

## Brief (day-of)

```bash
python3 skills/restaurant-saver/scripts/todays_reservation.py
```

Matches calendar events titled `Reservation at X` / `Dinner at X` (also
`Resy:` / `Reso at`). When `has_reservation` is true, research the place —
critics, regulars, the dishes people actually name — and post what to order
to `#restaurants`. Fires on any day, not just Thursdays.

## Calendar

Read-only via `gog` against the shared household calendar
(`calendars.shared`) as `accounts.service`. Nothing in this skill writes to it.

If calendar reads start failing with a permission error, the share was
revoked — re-subscribe with `gog calendar subscribe <id> -a <account>`.

## Error paths

| `error` | Meaning | What to do |
|---------|---------|-----------|
| `not_logged_in` / `wrong_account` | Maps session dead | Run `login.py` (visible browser) |
| `maps_url_unresolved` | Link didn't resolve | Ask for the name instead |
| `unsupported_url_host` | IG / TikTok / YouTube link | Ask for a name or a real article |
| `no_name_extracted` | Article had no place name | Ask the user for the name |
| `no_maps_results` | Search found nothing | Ask for city/neighborhood |
| `not_a_place_page` | Landed on search results | Re-run; disambiguate first |
| `gog_failed` | Calendar read failed | Check the share is still live |

`ProcessSingleton ... already in use` means another script holds the Chrome
profile. Only one browser script per profile at a time — check with
`pgrep -fl backfill_from_json` before launching a second.

## Operational notes

- **Discord, not Slack.** v1 lived in Slack `#inbox-restaurants`; everything moved to `#restaurants` on 2026-09-03. No Slack path remains.
- **Google Maps "Want to go" is best-effort.** `add_to_list.py` still works and the DB tracks `maps_saved`, but a Maps scrape failure must never fail a save — the DB is the source of truth now.
- **Dropped pins are not restaurants.** The old list carried entries like `34.110890, -118.41541` with cuisine `Note`; they're filtered out of candidates and set to `status='archived'`.
- **Never delete a place.** Archive it (`status='archived'`).

## Quick reference

```bash
PY=skills/restaurant-saver/.venv/bin/python
S=skills/restaurant-saver/scripts

$PY $S/save_restaurant.py "Bestia" --note "a friend asked"   # intake
$PY $S/enrich_place.py "<maps url>"                        # re-enrich one place
python3 $S/plan_thursday.py --weeks 4                      # free Thursdays + shortlist
python3 $S/post_suggestions.py --dry-run < payload.json    # preview cards
python3 $S/sweep_reservation_reactions.py --dry-run        # what would the sweep do
python3 $S/todays_reservation.py --date 2026-09-10         # day-of check
$PY $S/backfill_from_json.py --no-enrich                   # re-import v1 cache

sqlite3 restaurant-saver/restaurants.db \
  "SELECT name, cuisine, drive_text, reservation_platform FROM restaurants
   WHERE status='want_to_go' ORDER BY name;"
```

Layout:
```
skills/restaurant-saver/
  ├── SKILL.md
  ├── .venv/                          (gitignored)
  └── scripts/
      ├── restaurant_common.py        (schema, geo, HOME, channel + calendar ids)
      ├── calendar_lib.py             (gog reads, free-Thursday logic)
      ├── browser.py                  (Maps profile — accounts.personal)
      ├── booking_browser.py          (booking profile — accounts.shopping)
      ├── login.py / login_booking.py (one-time sign-ins)
      ├── resolve_place.py            (input → canonical place)
      ├── enrich_place.py             (drive time, reservation link, details)
      ├── save_restaurant.py          (INTAKE orchestrator)
      ├── plan_thursday.py            (PLAN input)
      ├── post_suggestions.py         (PLAN cards + ✅)
      ├── sweep_reservation_reactions.py  (✅ → booking intent)
      ├── todays_reservation.py       (BRIEF detection)
      └── backfill_from_json.py       (one-time v1 migration)

restaurant-saver/
  ├── restaurants.db                  (source of truth)
  ├── chrome-profile/  booking-profile/   (gitignored)
  └── want-to-go.json                 (v1 cache, superseded)
```
