---
name: restaurant-saver
description: Save restaurants dropped in a Discord channel, suggest free Thursdays with ✅ cards, brief on reservation day. Use to add a place, list places or plan a night.
compatibility: Requires Google Chrome driven by Playwright (requirements.txt) signed into Google Maps, the gog calendar CLI, and a Discord bot. The default Chrome path is macOS; set CHROME_PATH elsewhere.
metadata: {"openclaw": {"requires": {"bins": ["gog"]}}}
---

# Restaurant pipeline

## The shape of it

One database, three doors. `<data_root>/restaurant-saver/restaurants.db` is
the product; the scripts are ways in and out of it. `data_root()` is
`paths.data_root` in config.json, default `_data/` at the repo root
(gitignored).

| Door | Trigger | Entry point | Run it |
|------|---------|-------------|--------|
| Intake | Message in the restaurants channel | `save_restaurant.py` | on each message |
| Plan | Wednesday morning | `plan_thursday.py` → `post_suggestions.py` | weekly |
| Sweep | Hourly during waking hours | `sweep_reservation_reactions.py` | hourly |
| Brief | Each morning | `todays_reservation.py` | daily |

Nothing here schedules itself. Put the three commands on whatever scheduler
you run agents from; they are silent on success and exit non-zero on failure,
so route failures to `discord.channels.errors` (or wherever you look).

Channel: the Discord channel in `discord.channels.restaurants`. **No @mention
required** — every non-bot message in that channel is intake.

All commands below assume the repo root as cwd, one venv, and:

```bash
S=restaurant-assistant/restaurant-saver/scripts
```

## Intake

The user drops one of: a restaurant name, a Google Maps link, an article URL, or
any of those plus a note ("Casa Invent — a friend's been asking", "went here, get
the branzino"). Parse intent, then run one command:

```bash
python3 $S/save_restaurant.py "<name|url>" \
    --note "<why they saved it>" --source-raw "<their raw message>"
```

Flags: `--note`, `--source-url`, `--source-raw`, `--status {want_to_go,been,favorite}`,
`--top N` (candidates for a name search, default 3), `--skip-drive`, `--watch`
(visible browser).

Intent → `--status`:
- default → `want_to_go`
- "I went to X" / "ate at X" / "had dinner at X" / "we tried X" → `been`
- explicit "favorite" → `favorite`

Output shapes:
- `{"kind":"saved","created":true,"restaurant":{...}}` — confirm in channel.
- `{"kind":"candidates","candidates":[...]}` — post a numbered list, wait for their pick, re-run with the chosen Maps URL.
- `{"kind":"error","error":"..."}` — see error table below.

Reply with name, cuisine, drive time, and the booking link when one was found:

> Saved **Casa Invent** — Italian · 4.6★ · 18 min away. Books on [Resy](<...>). Added to your list.

One save takes ~40–60s (resolve + enrich + drive time in a single browser
session). Say nothing while it runs; just answer when it lands.

Article URLs resolve by page title only: JSON-LD `Restaurant` markup, then
`og:title`, then the `<h1>`. There is no LLM fallback. If none of those name
the place you get `no_name_extracted`; ask the user for the name.

The two halves are also runnable alone:

```bash
python3 $S/resolve_place.py "<name|url>" [--top N] [--watch]     # -> single | candidates | error
python3 $S/enrich_place.py "<maps place url>" [--skip-drive] [--watch]
```

### What gets stored

Beyond name/address/coords: `cuisine`, `price`, `rating`, `review_count`,
`note` (why they saved it), `source_url`, `source_raw`, `drive_minutes` +
`drive_text` (from `user.home`), `reservation_platform`, `reservation_url`,
`status`, `added_at`, `visited_at`, `last_suggested_at`.

`price` is stored exactly as Maps prints it — `$$`, `$$$$`, `$20–30`, `$100+` —
not normalised to a dollar count.

`upsert_restaurant` never blanks an enriched field with an empty one, and
appends to `note` rather than clobbering it — a later bare mention of a place
must not erase why they saved it the first time. Two places with the same name
at different addresses are two rows; a name-only match is only trusted when one
side has no address.

## Plan (Wednesday)

```bash
python3 $S/plan_thursday.py --weeks 4 --limit 8
```

Returns `{thursdays:[{date,label,free,blockers,reservation}], candidates:[...]}`.
A Thursday is free when nothing overlaps 5:30–10:00pm and there's no all-day
event; a timed event that starts before the day and ends after it (a trip)
blocks it too. Candidates are `want_to_go` only — `been`, `favorite` and
`archived` never appear — scored for bookability, rating, drive time, whether
the user wrote a note, and *against* anything suggested in the last 45 days;
the shortlist is capped at 2 per cuisine so it isn't five Italian places.

**You pick the final 3–5 and write the blurbs.** The scorer is a shovel, not
a critic — it can't tell that they had pasta twice this month.

Two rules the scorer does not encode:
- **Spread the platforms.** A reservation link scores the same whatever site
  it points at. Don't offer four places on one platform when the list has
  others.
- **An empty blurb is correct.** The card already prints cuisine, price, rating
  and drive time. A blurb that restates them — "Italian, 4.6, about 18 minutes
  out" directly under a line saying exactly that — is filler. Say something new
  or say nothing.

Then:

```bash
echo '{"date":"2027-01-07","intro":"...","picks":[{"id":3,"blurb":"..."}]}' \
  | python3 $S/post_suggestions.py [--dry-run]
```

One card per restaurant, each seeded with ✅. Reactions are per-message, so a
batched digest would make the whole book-this contract impossible — never
consolidate the cards. `--dry-run` prints the cards instead of posting.

## Sweep (hourly)

```bash
python3 $S/sweep_reservation_reactions.py [--dry-run]
```

Reads ✅ / ❌ on open suggestion cards and returns
`{checked, to_book, declined, conflicts, errors}`. **The sweep never books.**
Booking is external and hard to reverse, so this package stops at the intent:
`to_book` is a work list for a human or for a booking step you write yourself.

Guards that are load-bearing:
- ✅ and ❌ on the same card → left `offered`, never guessed.
- Two ✅ for the same date → reported in `conflicts`, nothing accepted. Ask which.
- A card whose reactions cannot be read (deleted message, expired token) lands
  in `errors` and the exit code is 1. It is never counted as "no reaction".
- Only `discord.user_id`'s reactions count. The bot's seed and anyone else's
  ✅ are ignored, so the bot can sit in a shared server. Without a numeric
  `discord.user_id` the sweep exits 2 and touches nothing.

`--dry-run` reports without moving any suggestion to `accepted` / `declined`.

## Brief (day-of)

```bash
python3 $S/todays_reservation.py [--date YYYY-MM-DD]
```

Matches calendar events titled `Reservation at X` / `Dinner at X` (also
`Resy:` / `Reso at`). When `has_reservation` is true, research the place —
critics, regulars, the dishes people actually name — and post what to order
to the restaurants channel. Fires on any day, not just Thursdays.

## Calendar

Read-only via the `gog` CLI against the shared household calendar
(`calendars.shared`) as `accounts.service`. Nothing in this skill writes to it.
`gog` prints timestamps with an offset; `calendar_lib.py` converts them to the
machine's local zone before comparing against the dinner window.

If calendar reads start failing with a permission error, the share was
revoked — re-subscribe with `gog calendar subscribe <id> -a <account>`. If you
don't use `gog`, replace `fetch_events` in `calendar_lib.py`; it is the only
function that knows the CLI.

## Error paths

| Where | `error` | Meaning | What to do |
|-------|---------|---------|-----------|
| `login.py` (stderr) | `not_logged_in` / `wrong_account` | Sign-in didn't stick, or the wrong Google account | Re-run `login.py` |
| resolve / save | `empty_query` | Blank input | Ask again |
| resolve / save | `maps_url_unresolved` | Maps link didn't land on a place page | Ask for the name instead |
| resolve / save | `unsupported_url_host` | IG / TikTok / YouTube / X link | Ask for a name or a real article |
| resolve / save | `no_name_extracted` | Article had no title-shaped place name | Ask the user for the name |
| resolve / save | `no_maps_results` | Search found nothing | Ask for city/neighborhood |
| `enrich_place.py` | `not_a_place_page` | URL isn't a `/maps/place/` page | Resolve first, then enrich |
| `post_suggestions.py` | `no_picks` | Empty `picks` | Fix the payload |
| plan / brief | `gog_failed: ...` (RuntimeError) | `gog` missing, or the calendar read failed | Install gog / check the share |
| sweep | `errors[]` in output, exit 1 | A card's reactions couldn't be read | Check the message and the bot token |

A Maps session that has silently expired shows up as `no_maps_results` or
`maps_url_unresolved` on *every* save. That's the cue to run `login.py` again.

`ProcessSingleton ... already in use` means another script holds the Chrome
profile. Only one browser script per profile at a time — check with
`pgrep -fl restaurant-saver/scripts` before launching a second.

## Operational notes

- **Dropped pins are not restaurants.** A saved-places import can carry rows
  whose name is a bare coordinate pair with no cuisine. `plan_thursday.py`
  sets them to `status='archived'` when it sees them.
- **Never delete a place.** Archive it (`rc.set_status(conn, id, rc.STATUS_ARCHIVED)`
  or `UPDATE restaurants SET status='archived'`).
- **Google Maps "Want to go" is untouched.** The DB is the source of truth;
  nothing writes back to a Maps list.

## Quick reference

```bash
S=restaurant-assistant/restaurant-saver/scripts

python3 $S/login.py                                       # one-time visible sign-in
python3 $S/save_restaurant.py "Casa Invent" --note "a friend asked"   # intake
python3 $S/resolve_place.py "Casa Invent"                 # resolve only
python3 $S/enrich_place.py "<maps url>"                   # re-enrich one place
python3 $S/plan_thursday.py --weeks 4                     # free Thursdays + shortlist
python3 $S/post_suggestions.py --dry-run < payload.json   # preview cards
python3 $S/sweep_reservation_reactions.py --dry-run       # what would the sweep do
python3 $S/todays_reservation.py --date 2027-01-07        # day-of check

sqlite3 _data/restaurant-saver/restaurants.db \
  "SELECT name, cuisine, drive_text, reservation_platform FROM restaurants
   WHERE status='want_to_go' ORDER BY name;"
```

Layout:
```
restaurant-assistant/restaurant-saver/
  ├── SKILL.md
  ├── requirements.txt                (playwright; Google Chrome.app required)
  └── scripts/
      ├── restaurant_common.py        (schema, geo, upsert rules, config access)
      ├── calendar_lib.py             (gog reads, free-Thursday logic)
      ├── browser.py                  (Maps profile — accounts.personal)
      ├── login.py                    (one-time sign-in)
      ├── resolve_place.py            (input → canonical place)
      ├── enrich_place.py             (drive time, reservation link, details)
      ├── save_restaurant.py          (INTAKE orchestrator)
      ├── plan_thursday.py            (PLAN input)
      ├── post_suggestions.py         (PLAN cards + ✅)
      ├── sweep_reservation_reactions.py  (✅ → to_book work list)
      └── todays_reservation.py       (BRIEF detection)

<data_root>/restaurant-saver/         (default _data/restaurant-saver/, gitignored)
  ├── restaurants.db                  (source of truth)
  └── chrome-profile/                 (signed-in Maps session)
```
