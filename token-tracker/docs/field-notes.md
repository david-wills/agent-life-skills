# Field notes: token-tracker

The operational history behind the package: what the half-hourly loop actually
does, what broke while it was running and what the code does about it now, and
the designs that were tried and thrown out. It lives here rather than in
`SKILL.md` so the instructions stay short. Everything below is checked against
the current scripts; deleted scripts are marked as such.

## How it runs

- `sample.py` is the only scheduled entry point, every 30 minutes under launchd or cron. It runs five steps in order, each in its own try/except, so a failing step is logged and the rest still run: ingest transcripts, attribute new sessions, ingest Codex rollouts, service the inbox, take a quota sample.
- Ingest (`collect.py`) walks `~/.claude/projects/**/*.jsonl` recursively (sub-agent transcripts sit a level deeper) and resumes each file from a stored byte offset. Only assistant records with a `usage` block and a real model name become rows; `INSERT OR IGNORE` on the message uuid makes re-runs free.
- Attribution runs per session, best evidence first, and stops at the first hit. A cron run whose window contains the whole session on the same model wins; then a live channel binding in the gateway's `sessions.json`; then the `chat_id` in the transcript's own opening context block, which survives rebinding; then `entrypoint: "cli"` means a person at a terminal. What is left is `unattributed`, and the report prints its dollar share instead of hiding it in another row.
- Codex is a separate pass (`collect_codex.py`) because its rollout files carry both halves in one place: per-turn counts and the plan's own 5-hour and weekly percentages. It gets its own report section; the two plans are different economies and are never summed.
- The quota sample (`quota.py`) hits the endpoint the CLI's `/usage` command reads, with the OAuth token from the keychain, and stores the raw response plus any per-model buckets. Without a subscription login it says so in one line and the rest of the heartbeat is unaffected.
- The second machine never sends transcripts. It runs the standalone `export_aggregates.py`, which emits per (day, model, project, entrypoint) token totals as JSON; the file is dropped into `token_tracker.inbox` (any folder both machines can reach) or carried over by hand, and `import_aggregates.py` turns it into rows tagged with that host. The heartbeat also keeps a current copy of the exporter in the inbox, so the other machine never runs a stale one.
- The report has a fixed shape: a headline total with its API-equivalent dollars, the live 5-hour and weekly percentages with the derived rate and its warnings, a by-machine section only once more than one host is feeding the database, then by model, by workflow, off-machine signals, the unpriced line, Codex, local models, and an honesty line giving the unattributed share.
- Two manual levers: `collect.py --reattribute` drops every label and rebuilds them after a rule change, and `report.py --no-collect` reports on stored data without touching the transcripts or the usage endpoint.
- `report.py --yesterday` runs once a day, early. At that hour "today" is minutes old, so the morning post has to look backwards to say anything.

## What broke, and what it taught

- **Every figure was inflated about 2.4x.** Claude Code writes one JSONL record per content block of a response, each repeating the same `usage` object, and the collector counted records. One sampled session had 415 records for 173 real calls. `collect.py` now dedupes on `requestId`, and when it resumes mid-file it seeds the seen set from the database so a call whose first block landed on the previous pass is not counted twice.
- **A half-written trailing line was skipped forever.** The original stored end-of-file as the resume offset while its comment claimed the offset stopped before the partial line; once the line completed, the next pass started after it. The file is now read in binary so offsets are exact, and the loop stops at the start of any line with no newline, so that line is read whole next time. A complete but corrupt line is a different case and is skipped.
- **One model prices cache reads at a flat rate, not 0.1x input.** Costing it the default way overstated machines heavy on that model by roughly 4x on their largest input bucket. `lib.CACHE_READ_FLAT` overrides the read rate per model; `cost_usd()` consults it before the multiplier.
- **The dollar-per-percent conversion is not one number.** In one observed week, two monotonic segments of the same window implied rates 2.4x apart, because an Opus-heavy stretch and a Haiku-heavy one were being averaged. `segment_rates()` derives the rate per segment, anchors the first segment at the window's 0% so a week sampled from mid-way is not read as a blip, ignores rises under 2 points as integer noise, and the report warns when segments disagree by more than 1.5x.
- **Reported utilization fell from double digits to near zero with `resets_at` unchanged.** An out-of-band credit or plan change, not a window roll. Dividing the full window's spend by the post-reset percentage would have overstated the rate by an order of magnitude, so calibration runs on the latest monotonic segment only and prints `n/a` when it has nothing to stand on. `quota.detect_reset()` posts a one-time alert for any drop over half a point inside an unchanged window, deduplicated through the `alerts` table.
- **Unknown models were silently costed at the Opus tier.** A comment said the report would name them; nothing did. `lib.unpriced()` now lists every model outside `PRICES`, and the report prints an `unpriced:` line pointing at `PRICES_AS_OF`, so a guess reads as a guess.
- **Days were cut in a hardcoded zone, five copies of it.** A machine in another zone, or a summer/winter change, would move usage across a day boundary and disagree with an export from elsewhere. `lib.day_tz()` reads `token_tracker.timezone` (IANA) and falls back to `None`, which the datetime library treats as the machine's zone correctly across DST for each instant. `export_aggregates.py --timezone` exists so the other machine cuts days the same way.
- **A host could collide with itself on import.** An export labelled with the local host's own name would replace the rows already read directly from disk. `import_aggregates.py` refuses a document whose `host` equals `lib.local_host()`, and `token_tracker.host` exists so the local label is explicit rather than whatever the hostname happens to be.
- **Positional inserts broke twice.** Adding `host` to `attribution` wrote `kind` into `label`; adding `provider` to `quota_samples` lost hours of samples with no error. Every INSERT in the package now names its columns.
- **`SCHEMA` and the migration list disagreed.** `provider` was added to `quota_samples` by migration only, so existing databases worked while a fresh one lacked the column. Invisible until a test built one; the two are kept in step and tests create from scratch.
- **Local models arrived looking like OpenAI.** Rollouts record a model id but no provider, and a locally-served model reaches Codex-style harnesses through an OpenAI-compatible endpoint. `lib.classify_provider()` positively identifies the two cloud vendors and treats anything else as `local`; the importer re-derives provider from the name rather than trusting the file, and because an import replaces its host entirely the reclassification took effect without a re-export.
- **Older transcripts carry only an aggregate cache-creation field.** Newer ones split it into 5-minute and 1-hour buckets under `cache_creation`. When the split is absent the collector and the exporter both read `cache_creation_input_tokens` as 5-minute writes rather than dropping it.
- **Codex `input_tokens` includes the cached portion.** Counting both double-counted the cache. The collector and the exporter both subtract `cached_input_tokens` first.
- **Local model volume cannot be reconstructed after the fact.** Ollama returns per-request counts and discards them, the trajectory files record a provider and model but no usage, and the gateway's own session totals were internally inconsistent. Local rows therefore carry tokens only where a harness wrote them; anything better means logging at the model server.
- **`short_project()` stripped the leading dash before the replacement that expected it**, so the gateway workspace showed under the wrong name. Found by the first test suite, not in production.
- **Transcripts are pruned after about 30 days.** The database is the archive; history before first collection is gone, and deleting it to "rebuild" loses everything older than the prune window.

## Things tried and dropped

- **Pull-only rsync over SSH (deleted).** The first multi-machine design rsynced the other machine's raw transcripts into a local `hosts/<name>/projects` tree with a dedicated unattended key, and `collect.py` carried a `synced_hosts()` shim to ingest that tree. Rejected: an `authorized_keys` entry grants a full shell as that user, the script would only *choose* to run a filtered rsync with nothing enforcing it, the key had to sit unencrypted, and a transcript sync copies code and conversations. The script and the shim are gone and the keypair was destroyed; if it is ever revived, say plainly what it grants.
- **A specific SMB share as the drop folder.** The private setup wrote the export to a share hosted on the other machine and auto-mounted here, with a one-shot installer (never shipped) scheduling a daily export there. That worked but was one arrangement of many; the shipped version reduces it to `token_tracker.inbox`, any folder both machines can reach, and a missing folder is a silent skip.
- **`migrate_dedupe.py` (deleted).** A one-off that fixed the record-versus-call inflation in rows already stored without `requestId`. A full re-ingest was impossible because many source transcripts had since been pruned, so it collapsed consecutive runs of identical usage within a session, which is safe because `cache_read` grows monotonically across real calls. Verified against `requestId` ground truth, run once, removed.
- **Fixed launchd labels and a fixed local report time in the docs.** Replaced by "schedule `sample.py` every 30 minutes and `report.py --yesterday` once a day, early" so the package does not assume one machine's scheduler or zone.
- **A default host label from the hostname alone.** Still the fallback, but `token_tracker.host` is documented as the thing to set, because a hostname is not a stable label across renames and the importer's collision check depends on it.

## Decisions that look odd

- **Counts-only export instead of sync.** Adding a machine is a security decision before it is a config one. A JSON file of per-day totals carries no prompts, responses, code or session ids; `--redact-projects` even hashes the project names, which embed the transcript path and usually a username. Same output as a sync, no access in either direction.
- **Aggregate rows are synthetic `messages` at noon.** Storing an import as ordinary rows means every existing query and report works unchanged, with no second code path. The cost is granularity: the rows are timestamped at noon in the tracker's day zone, so a quota window that starts mid-day gets the whole day on one side, and `offmachine.py` excludes imported hosts from the 5-hour regression, where a noon lump would dump a day of spend into one 30-minute bucket. Ids are deterministic and an import replaces its host, so re-importing overwrites rather than double-counts.
- **OpenAI prices are config-only, with no defaults.** Guessing another vendor's list prices would put invented dollar figures in a cost report. Unset, Codex rows show tokens and plan percentages and a line saying they are not costed.
- **Local models get volume but no cost.** They have neither a dollar price nor a quota, so they cannot join either calculation; a fabricated place in the cost math would be worse than a token count.
- **The exporter is a standalone stdlib file.** It is meant to be copied to a machine with no clone of the repo and no packages, so it duplicates `classify_provider()` and the request dedupe on purpose. The heartbeat's refresh of the inbox copy is what keeps the duplicate honest.
- **Attribution records a `confidence` and a `method`.** `attribute()` re-examines any session not marked `high`, so an `unattributed` session gets another look each pass as cron logs and channel bindings catch up. `--reattribute` drops every label for rule changes, and `method` says which of the four passes matched, which is what you read when a label looks wrong.
- **Containment, not overlap, for cron matching.** A long-lived interactive session overlaps every cron that fires during it; requiring the session to start inside the run window and end shortly after it is what keeps those separate. Imported hosts skip the cron and channel passes entirely, since the gateway state only describes this machine.
- **Sub-agent turns count against the parent session.** They bill to the same account, so for cost that is the right answer even though it hides the fan-out.
- **The quota conversion is derived every run, not configured.** Anthropic publishes the weekly limit as a percentage with no token or dollar figure behind it, so the tracker divides measured spend by measured utilization each time. If the weighting changes, the next run re-derives it and the report states its confidence.
- **The headline dollar figure is a counterfactual.** Most of an agentic day's API-equivalent spend is cache reads and writes, i.e. re-reading the same context every turn, which a subscription bills as capacity and rate limits rather than per read. The number is what the traffic *would* cost at list prices, kept exact because it is the only stable unit the workflows can be compared in.
- **No agent scheduler.** A cost tracker that spends frontier-model tokens to report on token spend is self-defeating; both jobs are plain Python with no model calls.

## Numbers worth knowing

Per-model prices are deliberately not repeated here: they live in `PRICES` in
`token-tracker/token-tracker/scripts/lib.py`, dated by `PRICES_AS_OF`. Paths
below are relative to `token-tracker/token-tracker/` unless they start with `lib/`.

| Constant | Value | Where |
| --- | --- | --- |
| Heartbeat interval | 30 min (set in the scheduler, documented in the docstring) | `scripts/sample.py` |
| Cache write 5m / 1h multiplier | 1.25x / 2.0x input | `scripts/lib.py` `CACHE_WRITE_5M_MULT`, `CACHE_WRITE_1H_MULT` |
| Cache read multiplier | 0.1x input, unless the model is in `CACHE_READ_FLAT` | `scripts/lib.py` |
| Unknown-model price tier | Opus tier (`DEFAULT_PRICE`) | `scripts/lib.py` |
| Cron window slack | session start within -15 s / +30 s of the run window, end no later than +60 s; 40 candidate runs scanned | `scripts/collect.py` `attribute()` |
| `chat_id` scan depth | first 60 lines of a transcript | `scripts/collect.py` `scan_chat_id()` |
| Drift regression | at least 8 intervals; an interval over 2 h (sleep) is dropped | `scripts/offmachine.py` `MIN_INTERVALS`, `intervals()` |
| Segment warning | rates more than 1.5x apart; segments rising under 2 points ignored | `scripts/report.py` `segment_rates()`, `build()` |
| Reset detection | weekly percentage drops more than 0.5 points with `resets_at` unchanged | `scripts/quota.py` `detect_reset()` |
| Export schema | writes 2; importer accepts 1 and 2 | `scripts/export_aggregates.py` `SCHEMA`, `scripts/import_aggregates.py` |
| Redacted project name | first 12 hex of a SHA-256 | `scripts/export_aggregates.py` `--redact-projects` |
| Imported row timestamp | noon in `lib.day_tz()` | `scripts/import_aggregates.py` |
| Timeouts | keychain read 20 s, usage endpoint 30 s, SQLite busy 30 s, gateway DB open 20 s | `scripts/lib.py`, `scripts/quota.py`, `scripts/collect.py` |
| Retries | none anywhere; a failed step is logged and the next heartbeat catches up | `scripts/sample.py` |
| Discord post pause | 250 ms between chunks; chunks under 1900 characters | `lib/discord.py` `send_message()`, `split_for_discord()` |
| Transcript retention | about 30 days, a Claude Code behaviour, not a constant here | `README.md` |
