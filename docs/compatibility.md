# Compatibility

What each skill needs and what it touches outside the repo. "Any OS" means
macOS and Linux were both exercised (CI runs the suite on both); Windows is
untested. Paid means the account behind the skill costs money to have; the
skills themselves are free to run. `doctor.py <package>` checks the binaries,
modules, config keys and secrets on the machine in front of you.

| Skill | OS | Accounts | Binaries and packages | Writes outside the data root | Dry run |
| --- | --- | --- | --- | --- | --- |
| morning-news | any | Discord bot | feedparser, PyYAML, python-dateutil (`requirements.txt`); an agent runtime to run `prompt.md` | Discord posts | `post_digest.py --dry-run` |
| reading-list | any | Readwise Reader (paid), Discord bot | `claude` CLI | Discord posts and reactions; the sweep archives, defers or deletes Reader documents | `reading_list_digest.py --dry-run`, `sweep_reading_list_reactions.py --dry-run` |
| import-reader-archive | any | Readwise Reader (paid) | `claude` CLI for documents Reader did not summarise | none | none needed: re-runnable, idempotent |
| import-readwise-highlights | any | Readwise (paid) | none | none | none needed: re-runnable, idempotent |
| import-apple-health | any; the shipped launchd templates are macOS | Health Auto Export iOS app (its REST automation is a paid feature), Discord bot for gap alerts | none | Discord gap alerts; `render_launchd.py --install` writes a LaunchAgent plist | `gap_check.py`, `backfill_from_folder.py`, `render_launchd.py`, `ingest_apple_health.py`, all `--dry-run` |
| import-hevy-workouts | any | Hevy (the API key needs Hevy Pro) | none | none | `ingest_hevy.py --dry-run` |
| import-oura-data | any | Oura ring with membership; a free API application for the client credentials | none | none (the OAuth token is stored under the data root) | `ingest_oura.py --dry-run` |
| structured-metrics | any | none | none | none | `ingest_metrics.py --dry-run` |
| workout-coach | any | Hevy (as above); Discord through the agent runtime | an agent runtime with a model | overwrites two rolling Hevy routines on your phone | `push_to_hevy_routine.py --dry-run` (needs the template cache, so an API key once) |
| restaurant-saver | any with `CHROME_PATH` set; the default Chrome path is macOS | a Google account signed into Maps; an account for the `gog` calendar CLI; Discord bot | Google Chrome, Playwright (`requirements.txt`), `gog` | Discord posts and reactions; a persistent Chrome profile under the data root; never books, never deletes a place | `post_suggestions.py --dry-run`, `sweep_reservation_reactions.py --dry-run` |
| token-tracker | any; quota sampling reads the macOS keychain | a Claude subscription signed into Claude Code; Discord bot | none (`security` on macOS for quota) | Discord report post | `report.py --no-post` |
| name-videos | macOS | none | ffmpeg, pyobjc Vision and Quartz (`requirements.txt`) | renames files in the drop folder, with an undo log | `rename.py` is a dry run by default; `undo.py --dry-run` |

Reads that are easy to miss:

- token-tracker reads every Claude Code transcript under `~/.claude/projects`
  and, when present, the agent gateway's state for attribution. Counts only
  leave the machine, and only if you export them.
- restaurant-saver drives a signed-in Google Maps session with automation
  fingerprinting disabled, which is against Google's terms of service. The
  package README says so; this table repeats it.
- reading-list's summariser sends article text to the `claude` CLI, which runs
  with tools, MCP servers and user settings disabled (`lib/claude_cli.py`).

Python 3.11 or newer everywhere; the stdlib covers all but three skills.
