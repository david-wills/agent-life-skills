# Restaurant assistant

One skill, one SQLite database, three doors. Drop a restaurant into a Discord
channel and it is resolved, enriched and stored. On Wednesday an agent looks at
the shared calendar, finds the free Thursdays, and posts three to five
suggestions as cards with a ✅ on each. Reacting is the booking intent. On the
day, another run notices the reservation on the calendar and posts what to
order.

The database is the product. The scripts are ways in and out of it.

## The loop

```
Discord #restaurants
  "Bestia — a friend's been asking"     a Maps link     an article URL
         │
         │  save_restaurant.py — resolve ▸ enrich ▸ upsert     (~40-60s)
         ▼
   restaurants.db
   name · cuisine · price · rating · drive time from home
   reservation platform + booking URL · your note · status
         │
         │  plan_thursday.py — Wednesday 9:20
         │  free Thursdays on the shared calendar × a scored shortlist
         ▼
   the agent picks 3-5 and writes the blurbs
         │
         │  post_suggestions.py — one card per place, each seeded with ✅
         ▼
   Discord #restaurants ──── you react ✅ ────┐
                                              │  sweep_reservation_reactions.py — hourly
                                              ▼
                                       a `to_book` work list
                                       (the sweep never books)
                                              │
                                              ▼
   todays_reservation.py — daily 9:40 ── "Reservation at X" on the calendar
                                              ▼
                                   what to order, posted day-of
```

## The pieces

| Script | Door | Does |
| --- | --- | --- |
| `save_restaurant.py` | Intake | Name, Maps link or article URL → canonical place, enriched, stored. Returns `saved`, `candidates` (ask which), or a typed `error`. |
| `resolve_place.py`, `enrich_place.py` | Intake | Input → Google Maps place; then drive time, reservation platform, booking URL, details. |
| `plan_thursday.py` | Plan | Free Thursdays (nothing 5:30-10pm, no all-day event) plus a scored shortlist, capped at two per cuisine. |
| `post_suggestions.py` | Plan | The cards. Reactions are per message, so a batched digest would break the whole contract. |
| `sweep_reservation_reactions.py` | Sweep | ✅ / ❌ on open cards → booking intents. Conflicts (two ✅ on one date, ✅ and ❌ on one card) are reported, never guessed. |
| `todays_reservation.py` | Brief | Matches `Reservation at` / `Dinner at` / `Resy:` events on the calendar. |
| `calendar_lib.py` | | Read-only calendar access and the free-Thursday logic. |
| `browser.py`, `booking_browser.py` | | Two persistent Chrome profiles: one signed into Maps, one into the booking sites. Kept separate on purpose. |
| `restaurant_common.py` | | Schema, geo helpers, the upsert rules. |

## Ideas worth stealing

**The scorer is a shovel, not a critic.** The script ranks by bookability,
rating, drive time, whether you wrote a note, and against anything suggested in
the last 45 days. The agent makes the final pick, because a scorer cannot tell
that you had pasta twice this month.

**Prefer *automatable*, not *bookable*.** The first live run offered three
OpenTable places out of four because the prompt said "prefer bookable" when it
meant "prefer the platforms the booking step can drive". At most one OpenTable
pick a week now.

**An empty blurb is correct.** The card already prints cuisine, price, rating
and drive time. A blurb restating them is filler. Say something new or say
nothing.

**A note is append-only.** A later bare mention of a place must not erase why
you saved it the first time. The upsert never blanks an enriched field with an
empty one either.

**Never delete a place.** Archive it. Dropped pins that were never restaurants
(bare coordinates from an old Maps list) are archived, not removed.

**Booking stays behind two gates.** Your ✅ *and* a deliberate booking step.
Booking is external and hard to reverse; the hourly sweep only ever produces a
work list.

## Running it

- Python deps in `requirements.txt` (Playwright), then
  `python -m playwright install chromium`.
- One-time visible sign-ins: `login.py` for the Maps profile, `login_booking.py`
  for the booking profile. Two profiles because they are two accounts.
- Calendar reads go through the `gog` CLI against a shared calendar. Swap
  `calendar_lib.py` for whatever calendar client you use; nothing writes to it.
- Config keys: `discord.channels.restaurants`, `discord.channels.errors`,
  `user.home` (the drive-time origin), `accounts.personal` (Maps),
  `accounts.shopping` (booking), `accounts.service` and `calendars.shared`.
- Three agent crons: the Wednesday planner, the hourly sweep, the day-of brief.
  All silent on success; failures go to the errors channel.

## Honest limits

- **OpenTable blocks the automation.** Akamai detects the debug-pipe-driven
  Chrome and 403s it. Resy and Tock work from the same profile. OpenTable picks
  degrade to a prepared link you open yourself.
- **Google Maps has no API for "Want to go".** Resolution scrapes a signed-in
  Maps session and is as brittle as that sounds. A Maps failure never fails a
  save; the database is the source of truth.
- **One browser script per profile at a time.** `ProcessSingleton ... already
  in use` means another script holds the profile.
- **The database ships as schema only.** No seed data; the schema is in
  `restaurant_common.py` and is created on first save.
