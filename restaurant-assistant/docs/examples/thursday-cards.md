# Example: three Thursday cards

The Wednesday planning run, on invented restaurants. `plan_thursday.py` reads
the calendar and shortlists rows from the database; an agent writes the blurbs
and hands `post_suggestions.py` a payload. Everything below came out of the real
scripts against a throwaway data root, with the calendar step replaced by a
hand-written payload (the calendar CLI needs an account).

How it was made:

```bash
# three rows inserted with restaurant_common.upsert_restaurant() into <tmp>/restaurant-saver/restaurants.db
SKILLS_PATHS_DATA_ROOT=<tmp> python3 restaurant-assistant/restaurant-saver/scripts/post_suggestions.py --dry-run < payload.json
```

## The payload the agent writes

`id` is the database row; `blurb` is the agent's one or two sentences. The
cuisine, price, rating, drive time, note and links come from the row, not the
agent, so a blurb cannot invent a fact.

```json
{
  "date": "2025-09-11",
  "intro": "Thursday the 11th is open on the shared calendar. Three from the list, none suggested before:",
  "picks": [
    {
      "id": 1,
      "blurb": "Been on the list longest. Loud room, good for a weeknight; the branzino is the reason you saved it."
    },
    {
      "id": 2,
      "blurb": "Newest save. Wood-fired everything, walk-ins hard on weekends but Thursdays are fine."
    },
    {
      "id": 3,
      "blurb": "Furthest drive, cheapest bill. No reservations \u2014 get there by 6:45."
    }
  ]
}
```

## What the dry run prints

One card per restaurant. Posted for real, each card is its own Discord message
seeded with ✅, because reactions are per message and the sweep turns a ✅ into
a `to_book` row. A place with no reservation platform gets a Maps link only.

```text
[intro]
Thursday the 11th is open on the shared calendar. Three from the list, none suggested before:

[card 1]
**Casa Invent** — Italian · $$$ · 4.6★ · 18 min away
Been on the list longest. Loud room, good for a weeknight; the branzino is the reason you saved it.
_Your note: a friend's been asking about the branzino_
[Maps](<https://www.google.com/maps/place/Casa+Invent/>) · [Resy](<https://resy.com/cities/example/casa-invent>)
✅ to book Thu Sep 11, 7:00–7:30pm

[card 2]
**Ember & Ash** — Wood-fired · $$ · 4.7★ · 24 min away
Newest save. Wood-fired everything, walk-ins hard on weekends but Thursdays are fine.
[Maps](<https://www.google.com/maps/place/Ember+%26+Ash/>) · [Opentable](<https://www.opentable.com/r/ember-and-ash-example>)
✅ to book Thu Sep 11, 7:00–7:30pm

[card 3]
**Little Saigon Kitchen** — Vietnamese · $ · 4.5★ · 31 min away
Furthest drive, cheapest bill. No reservations — get there by 6:45.
_Your note: the article said get the crab noodles_
[Maps](<https://www.google.com/maps/place/Little+Saigon+Kitchen/>)
✅ to book Thu Sep 11, 7:00–7:30pm

{"posted": [], "date": "2025-09-11"}
```
