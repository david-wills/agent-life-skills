# Field notes: restaurant-assistant

The operational history behind the package: what ran, what broke, what was
tried and thrown away, and why some shipped choices look odd. Kept out of
`SKILL.md` so the instructions stay short. Scripts named here live under
`restaurant-assistant/restaurant-saver/scripts/` unless marked "deleted".

## How it runs

- Four doors, one SQLite file at `<data_root>/restaurant-saver/restaurants.db`.
  Intake fires on every non-bot message in the restaurants channel, no @mention.
  Plan runs on Wednesday morning, Sweep hourly through waking hours, Brief every
  morning (not just Thursdays).
- The private deployment staggered the scheduled runs off the top of the hour
  (twenty past, forty past) because several isolated agents booting on the same
  minute contended for the machine. The package schedules nothing itself; the
  commands are silent on success and non-zero on failure.
- One save takes about 40-60 seconds. Resolve, place details and the directions
  card share one Playwright session (`resolve_in_context` in `resolve_place.py`,
  called from `save_restaurant.py`); the first version launched a browser per
  step and a save took closer to two minutes. The one-time import of the old
  list, fifty-odd places, went from an hour to minutes the same way.
- Message volume is small. A save produces one reply; Wednesday produces one
  intro plus three to five cards, each its own message with a ✅ seeded on it,
  posted 0.4 s apart. The sweep prints JSON and posts nothing; the brief posts
  once, on a day with a reservation.
- The agent, not the script, writes the copy. `plan_thursday.py` returns free
  Thursdays and a scored shortlist; the pick and the blurbs are the agent's.

## What broke, and what it taught

- **A name search does not always land on a place.** Maps sometimes leaves the
  URL on `/maps/search/` while rendering a single place panel, and sometimes
  returns a list. `maps_search` waits up to 4 s for the rewrite to
  `/maps/place/`; otherwise the save returns `kind: candidates` (up to `--top`,
  default 3) and the agent posts a numbered list and re-runs with the chosen
  Maps URL. A wrong guess is a wrong row forever, so nothing is picked
  automatically.
- **The price regex matched the wrong dollar sign.** The first pattern knew only
  tiers and matched any lone `$` before punctuation, so text like `US$` produced
  a one-dollar tier and per-person ranges (`$20–30`, `$100+`) were missed.
  `PRICE_RE` in `enrich_place.py` now matches tiers or ranges as whole tokens,
  stored exactly as Maps prints them.
- **A round hour parsed as no drive time.** `DURATION_RE` required `min`, so `2
  hr` returned `None` while `1 hr 5 min` worked. The first test suite found it;
  minutes are optional now.
- **Dropped pins are not restaurants.** The old saved-places list carried rows
  whose name was a bare coordinate pair with no cuisine. They were first
  filtered out of the shortlist on every run; `plan_thursday.py` now matches
  `COORD_NAME` and sets them to `archived` on sight, never deleted.
- **A multi-day trip did not block the Thursday inside it.** The first blocker
  logic skipped timed events whose start and end were both on other days.
  `blockers_for` in `calendar_lib.py` now uses plain interval overlap against
  the dinner window and labels the blocker `until <day> <time>`; all-day events
  are end-exclusive and an event with no end is assumed to last 30 minutes.
- **Timestamps were compared in the wrong zone.** `gog` prints RFC 3339 with an
  offset; the first code stripped it and compared wall-clock digits, which is
  only right when the calendar's zone and the machine's agree. `_local_naive`
  converts to the machine's local zone first, then drops tzinfo; a timestamp
  with no offset is assumed local. Tests pin `TZ` and check both.
- **Anyone's ✅ used to count.** Ownership was inferred from counts: the bot
  seeds one ✅, so `count > 1` meant "the owner reacted". On a shared server a
  stranger's click created a booking intent, and a second bot reaction broke the
  arithmetic.
- **Now ownership comes from the reactor list.** `fetch_message_reactions` in
  `lib/discord.py` takes `discord.user_id`, lists who reacted (paginated at 100,
  super reactions included) and sets `by_user` only when that id is present; the
  list call is skipped when the count proves only the bot reacted. Without a
  numeric `discord.user_id` the sweep exits 2 and touches nothing.
- **A failed read looked like an empty read.** A deleted card or an expired
  token fell through as "nobody reacted". The sweep now reports the card in
  `errors` and exits 1.
- **Chat text on a command line.** Intake used to pass the user's message as
  shell arguments; a quote, a backtick or a `$(` in a message is a shell
  injection waiting to happen. `save_restaurant.py --stdin` takes one JSON
  object and answers malformed input with typed codes (`bad_stdin_json`,
  `stdin_needs_query`, `bad_status`, `stdin_unknown_field:<name>`).
- **Google refuses to sign in Playwright's bundled Chromium** ("this browser may
  not be secure"). `browser.py` drives the installed Chrome (`channel="chrome"`,
  or `CHROME_PATH`).
- **Headless Chrome advertises itself.** The UA says `HeadlessChrome`, and a
  hardcoded UA version disagrees with the `Sec-CH-UA` client hints Chrome sends
  anyway, an easy automation tell. `chrome_ua()` reads the installed version
  live; `--enable-automation` is dropped, `AutomationControlled` is disabled and
  `navigator.webdriver` is undefined.
- **Be clear about what that is.** Scraping a signed-in Google Maps session with
  automation fingerprinting turned off is against Google's terms of service; the
  account you sign in with is the one at risk, and Maps can change its markup
  any week.
- **A silently expired Maps session** does not error. It shows up as
  `no_maps_results` or `maps_url_unresolved` on every save; that pattern is the
  cue to run `login.py`, which checks the signed-in address against
  `accounts.personal` and reports `not_logged_in` or `wrong_account`.

## Things tried and dropped

- **Booking automation, the design.** Two gates: the owner's ✅ plus a deliberate
  booking script, running as a second account in a second Chrome profile signed
  into the reservation sites. The profile (`booking_browser.py`,
  `login_booking.py`, both deleted) was built; the booking script never was.
- **Booking automation, why it stopped.** Two of the three platforms loaded fine
  headless. The third sits behind a bot-detection vendor that recognises a
  debug-pipe-driven Chrome and returns 403 regardless of a visible window or a
  correct UA; it blocks the automation, not the account. Reserve-with-Google was
  a dead end: that platform's links from Maps carry no booking token.
- **Booking automation, the workarounds not built.** A browser-extension agent
  inside the real Chrome (no debug pipe, but nothing a scheduled job can
  trigger) and OS-level keyboard automation clicking into real Chrome. Until one
  exists, the package stops at `to_book` and the calendar is the handshake.
- **"Prefer bookable."** The first live planning run offered three picks on the
  blocked platform out of four because the prompt said "prefer bookable" when it
  meant "prefer what the booking step can drive". With no booking step the rule
  is now simply to spread the platforms.
- **The v1 list scraper.** `list_places.py` (deleted) walked the Maps "Want to
  go" list card by card into a JSON cache the agent read at query time,
  refreshed when older than a week; `backfill_from_json.py` (deleted) migrated
  that cache into the database once. Replaced by the database as source of
  truth.
- **Writing to Maps lists.** `add_to_list.py` (deleted) saved a place into a
  named Maps list and could move it between lists, tracked by a `maps_saved`
  column. Dropped with the column: nothing writes back to Maps, and a Maps
  scrape failure must never fail a save.
- **Live hours per place.** `place_hours.py` (deleted) fetched open-now and
  today's hours for a shortlisted place. Nothing in the loop needed it once the
  cards carried cuisine, price, rating and drive time.
- **`find_free_thursdays.py`** (deleted) printed only the calendar half of the
  planner. Folded into `plan_thursday.py`.
- **An LLM fallback for article URLs.** When JSON-LD, `og:title` and `<h1>` all
  failed, the resolver shelled out to a hosted model with the stripped page text
  and asked for the restaurant name. Removed: a 45 s external call and an extra
  dependency on the save path for the rare listicle, and a guessed name still
  had to be confirmed. Resolution is title-only and returns `no_name_extracted`;
  the agent asks.
- **A `reservations` table** (party size, platform, confirmation) and a
  `booking_window` column. Dropped with the booking script; the default printed
  on the cards (party of two, 7:00-7:30 pm) is the only trace.
- **Slack.** v1 lived in a Slack channel. Everything moved to Discord and no
  Slack path remains.

## Decisions that look odd

- **One skill with three doors, not three skills.** The data layer was first
  written for three named skills (saver, planner, brief) over one database. The
  split bought nothing: every door needs the same schema, the same upsert rules
  and the same browser profile. The database is the product; the scripts are
  ways in and out of it.
- **One card per restaurant.** Reactions are per message, so a ✅ on a five-item
  digest says nothing about which item. Never consolidate the cards.
- **The sweep stops at intent.** Booking is external, hard to reverse, and the
  sites block driven browsers. `to_book` is a work list for a human or a booking
  step you write and own; `Reservation at X` on the calendar is how the loop
  closes.
- **Places are archived, never deleted.** `suggestions` rows reference
  `restaurants.id`, the 45-day "suggested recently" penalty needs the history,
  and the note is why you saved it. `set_status` to `archived` is the only exit.
- **Name-only matching is trusted only when one side has no address.** Two
  places with the same name at different addresses are two rows (chains, second
  locations). The day-of brief has nothing but a calendar title, so it gets a
  name-only match; a fully resolved save does not.
- **A note is append-only.** A later bare mention must not erase why a place was
  saved the first time, and the upsert never blanks an enriched field with an
  empty one.
- **A signed-in Chrome profile rather than an API.** There is no API for a
  personal Maps saved list, and the place panel, category chip, booking anchor
  and directions card all come from the same signed-in session. One profile
  covers all of it, at the cost described above; only one script may hold it at
  a time (`ProcessSingleton ... already in use` means another one has it).
- **Two rules the scorer does not encode.** Spread the platforms, and an empty
  blurb is correct: the card already prints cuisine, price, rating and drive
  time, so a blurb restating them is filler. Both are the agent's job.

## Numbers worth knowing

| Constant | Value | Where |
| --- | --- | --- |
| Free-evening window | 17:30-22:00 local, any overlap blocks | `calendar_lib.py` (`DINNER_START`, `DINNER_END`) |
| Event with no end | assumed 30 min | `calendar_lib.py` (`blockers_for`) |
| Thursdays looked ahead | 4 (`--weeks`) | `plan_thursday.py` |
| Shortlist size | 8 (`--limit`); the agent posts 3-5 | `plan_thursday.py` |
| Per-cuisine cap | 2 | `plan_thursday.py` (`candidates`) |
| "Suggested recently" penalty | last 45 days, -6.0 | `plan_thursday.py` (`score`) |
| Drive-time score | +2.0 at <=20 min, +0.5 at <=35, -1.5 beyond | `plan_thursday.py` (`score`) |
| Name-search candidates | 3 (`--top`) | `resolve_place.py`, `save_restaurant.py` |
| Booking window printed on cards | 7:00-7:30 pm | `post_suggestions.py` (`card_text`) |
| Delay between cards | 0.4 s | `post_suggestions.py` |
| Sweep considers cards dated | yesterday onward, state `offered` | `sweep_reservation_reactions.py` |
| Reactor list page size | 100 | `lib/discord.py` (`REACTION_PAGE`) |
| Discord request timeout | 30 s | `lib/discord.py` (`discord_request`) |
| Article fetch timeout | 15 s | `resolve_place.py` (`http_get`) |
| Maps search | 30 s navigation, 3 s settle, 4 s for the URL rewrite | `resolve_place.py` (`maps_search`) |
| Place page | 40 s navigation, 12 s for `h1` | `enrich_place.py` (`place_details`) |
| Directions card | 45 s navigation, 12 s for the trip card | `enrich_place.py` (`drive_from_home`) |
| `gog` subprocess timeout | 90 s | `calendar_lib.py` (`fetch_events`) |
| Chrome version fallback | 140 when `--version` fails | `restaurant_common.py` (`chrome_version`) |
