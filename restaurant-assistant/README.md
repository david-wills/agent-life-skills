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
Discord restaurants channel
  "Casa Invent — a friend's been asking"     a Maps link     an article URL
         │
         │  save_restaurant.py — resolve ▸ enrich ▸ upsert     (~40-60s)
         ▼
   restaurants.db
   name · cuisine · price · rating · drive time from home
   reservation platform + booking URL · your note · status
         │
         │  plan_thursday.py — Wednesday morning
         │  free Thursdays on the shared calendar × a scored shortlist
         ▼
   the agent picks 3-5 and writes the blurbs
         │
         │  post_suggestions.py — one card per place, each seeded with ✅
         ▼
   Discord restaurants channel ──── you react ✅ ────┐
                                                     │  sweep_reservation_reactions.py — hourly
                                                     ▼
                                              a `to_book` work list
                                              (this is where the package stops)
                                                     │
                            you book it and put "Reservation at X" on the calendar
                                                     │
                                                     ▼
   todays_reservation.py — each morning ── finds it on the calendar
                                                     ▼
                                          what to order, posted day-of
```

## The pieces

| Script | Door | Does |
| --- | --- | --- |
| `save_restaurant.py` | Intake | Name, Maps link or article URL → canonical place, enriched, stored. Returns `saved`, `candidates` (ask which), or a typed `error`. |
| `resolve_place.py` | Intake | Input → Google Maps place. Article URLs resolve by JSON-LD / `og:title` / `<h1>` only; no LLM. |
| `enrich_place.py` | Intake | Drive time from home, reservation platform + booking URL, cuisine / price / rating. |
| `plan_thursday.py` | Plan | Free Thursdays (nothing 5:30-10pm, no all-day event) plus a scored shortlist, capped at two per cuisine. Archives dropped pins on sight. |
| `post_suggestions.py` | Plan | The cards. Reactions are per message, so a batched digest would break the whole contract. |
| `sweep_reservation_reactions.py` | Sweep | ✅ / ❌ on open cards → `to_book` / `declined`. Conflicts (two ✅ on one date, ✅ and ❌ on one card) are reported, never guessed; unreadable cards land in `errors` and fail the run. |
| `todays_reservation.py` | Brief | Matches `Reservation at` / `Dinner at` / `Resy:` events on the calendar. |
| `calendar_lib.py` | | Read-only calendar access via `gog` and the free-Thursday logic. About 160 lines, half of them comments; swap it for your own client. |
| `browser.py`, `login.py` | | One persistent Chrome profile signed into Maps, and the one-time visible sign-in. |
| `restaurant_common.py` | | Schema, geo helpers, the upsert rules, `set_status`. |

## Ideas worth stealing

**The scorer is a shovel, not a critic.** The script ranks by bookability,
rating, drive time, whether you wrote a note, and against anything suggested in
the last 45 days. The agent makes the final pick, because a scorer cannot tell
that you had pasta twice this month.

**An empty blurb is correct.** The card already prints cuisine, price, rating
and drive time. A blurb restating them is filler. Say something new or say
nothing.

**A note is append-only.** A later bare mention of a place must not erase why
you saved it the first time. The upsert never blanks an enriched field with an
empty one either, and two places sharing a name at different addresses are
two rows.

**Never delete a place.** Archive it. Dropped pins that were never restaurants
(bare coordinates from an old saved-places list) are set to `archived`, not
removed.

**A failed read is not an empty read.** The sweep reports a card it could not
read and exits non-zero. Treating "Discord said no" as "nobody reacted" is how
a ✅ quietly goes missing.

**Why booking is not shipped.** Booking is external and hard to reverse, and
the reservation sites actively block driven browsers. A ✅ produces a work list;
what happens next is a human, or a booking step you write and own. The
calendar is the handshake: put `Reservation at X` on it and the brief picks it
up.

## Running it

- macOS with Google Chrome.app installed. The resolver drives the real Chrome,
  not Playwright's bundled Chromium (Google refuses to sign that one in). Set
  `CHROME_PATH` to point at a Chrome binary anywhere else.
- `pip install -r restaurant-assistant/restaurant-saver/requirements.txt`, then
  `python -m playwright install` for the driver.
- `python3 restaurant-assistant/restaurant-saver/scripts/login.py` once, with
  a visible window, signed in as `accounts.personal`. Cookies persist in
  `<data_root>/restaurant-saver/chrome-profile/`.
- The `gog` CLI — a Google Calendar command-line client — authenticated as
  `accounts.service` with the shared calendar subscribed. Any calendar CLI that
  prints events as JSON will do; `calendar_lib.py` is about 160 lines and
  `fetch_events` is the only function that knows about `gog`.
- `DISCORD_BOT_TOKEN` in the environment (or in one of the secret stores
  `lib/read_secret.py` reads), for a bot that can post and react in the
  restaurants channel.
- Config keys: `discord.channels.restaurants`, `discord.channels.errors`,
  `user.home` (the drive-time origin, address + lat/lng), `accounts.personal`
  (Maps), `accounts.service` (calendar), `calendars.shared`, and optionally
  `paths.data_root` (default `_data/` at the repo root).
- Run these three commands on a schedule, silent on success and non-zero on
  failure:
  - Wednesday morning: `plan_thursday.py`, then the agent posts via `post_suggestions.py`
  - hourly: `sweep_reservation_reactions.py`
  - every morning: `todays_reservation.py`

## Terms of service

The resolver drives a signed-in Google Maps session in a real Chrome and turns
off the usual automation fingerprints (no `--enable-automation`,
`navigator.webdriver` undefined, a UA matching the installed Chrome). That is
scraping. Google's terms of service may prohibit it, the account you sign in
with is the one on the line, and Maps can change its markup any week. Use at
your own risk, with an account you are prepared to lose.

## Honest limits

- **Google Maps has no API for "Want to go".** Resolution scrapes a signed-in
  Maps session and is as brittle as that sounds. A Maps failure never fails a
  save; the database is the source of truth.
- **Booking is not shipped.** See above. The sweep stops at `to_book`.
- **Article resolution is title-only.** JSON-LD, `og:title`, `<h1>`. A listicle
  with no single place in the title comes back `no_name_extracted`.
- **One browser script per profile at a time.** `ProcessSingleton ... already
  in use` means another script holds the profile.
- **The database ships as schema only.** No seed data; the schema is in
  `restaurant_common.py` and is created on first use.
