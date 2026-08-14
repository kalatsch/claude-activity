# claude-activity

A local, privacy-respecting **heatmap of your AI coding usage** — Claude Code
*and* Codex CLI — see when you actually worked, by hour of day × day of
month, with project / session breakdown on hover. Everything is computed on
your machine from `~/.claude/projects/**/*.jsonl` and
`~/.codex/sessions/**/*.jsonl`; no server, no telemetry, and nothing about
your usage is ever sent. Two incidental outbound requests exist: a once-a-day
fetch of the public Anthropic/OpenAI pricing pages (to keep API-cost estimates
current), and the web fonts the dashboard loads from Google Fonts when you open
it in a browser (system fonts are used as a fallback offline).

![screenshot](docs/screenshot.png)

## Features

- **Source toggle** at the top: **Both** (Union of activity intervals) ·
  **Claude** · **Codex**. Choice persists in localStorage.
- 24 × N-day heatmap for any month with activity
- Three cell types: **work hours** (configurable), **off-hours** (weekday
  outside the work window), **weekends** — color-coded green / yellow / red
- Three big KPIs at the top of the sidebar: total · work · off
- Per-hour-of-day and per-day-of-week bar charts
- Hover tooltip shows projects + session titles + per-project time for that
  exact hour
- Daily token totals (input + output + cache, weighted) per month — split by
  source via the toggle
- **Estimated API cost** — what your usage *would* have cost on pay-as-you-go
  API pricing, computed **per model** (Opus / Sonnet / Haiku / Codex) from the
  exact input / output / cache-write / cache-read token split. Shown as a
  sidebar KPI and per day in the totals tooltip (alongside that day's tokens)
- **Historical pricing** — each run records the current price table into
  `history.json` with an effective date; old snapshots are never deleted, so
  every day is costed at the rates in effect on that day (past months are not
  re-priced when rates change)
- **Auto price refresh** — the first `/activity` each day silently fetches the
  official Anthropic/OpenAI pricing pages and writes `prices.json`; out-of-range
  values are ignored as a safety net. This is the only network call the
  *generator* makes (the dashboard itself also loads web fonts from Google Fonts
  on open); disable it by removing Step 2.5 from `commands/activity.md`
- **Today's column is highlighted** when viewing the current month
- **Persistent history.json** — survives Claude/Codex pruning old session files,
  with a one-step `history.json.bak` copy of the freshly-merged result written
  on every run
- **Prompt-history backfill** — recovers active hours for sessions whose JSONL
  logs Claude Code already pruned (even from before the plugin was installed)
  from `~/.claude/history.jsonl`; time only, no tokens/cost
- Configurable: work days, work hours, gap threshold, first day of week,
  auto-open
- Single HTML dashboard, opens from `file://`; no server, no telemetry (the
  only network request it makes is loading web fonts from Google Fonts)

## Install

Inside Claude Code, add this GitHub repo as a plugin source and install from it
— no central publishing involved. These are two separate slash commands; run
them one at a time, in order (the install needs the source added first):

```bash
/plugin marketplace add kalatsch/claude-activity
/plugin install claude-activity@claude-activity
```

To update later, use the built-in `/plugin` menu → **Manage plugins →
claude-activity → Update** (then restart the window or run `/reload-plugins`
to load it into the current session).

> Sharing with teammates? Send them the install page —
> **<https://kalatsch.github.io/claude-activity/>** (served from
> [`docs/index.html`](docs/index.html) via GitHub Pages).

## Usage

Inside Claude Code:

```
/activity              # generate + open in browser
/activity --settings   # re-run the setup wizard (aliases: --setup, --config, --reconfigure)
/activity --compact    # maintenance: collapse duplicate session items, archive old backups
/activity --help       # show usage and all flags
/activity --no-open    # generate without opening the browser
```

First run walks you through a short setup wizard (work days, work hours, gap
threshold, first day of week) and saves the answers to
`~/.claude-activity/config.json`.

## Configuration

`~/.claude-activity/config.json` — edit any time and re-run `/activity`.

| Key | Default | Meaning |
|---|---|---|
| `gap_minutes` | `10` | Max gap between events that still counts as continuous activity. |
| `work_intervals` | `[[9, 18]]` | Work hours as an array of `[start, end)` pairs (0–23). Multiple pairs let you split around a lunch break, e.g. `[[9, 12], [13, 18]]`. |
| `work_days` | `[0,1,2,3,4]` | Weekday indices, `0` = Monday … `6` = Sunday. |
| `first_day_of_week` | `0` | Where the by-day-of-week chart starts. `0` = Mon, `6` = Sun. |
| `auto_open` | `true` | Open the HTML in the browser after generating. |
| `cache_read_weight` | `0.1` | Multiplier on `cache_read_input_tokens` for the "billable" tokens total. |
| `output_dir` | `"~/.claude-activity"` | Where to write `index.html` and `history.json`. |

## How it works

1. Walks every `.jsonl` under `~/.claude/projects/` (main sessions + sub-agent
   files) and every `.jsonl` under `~/.codex/sessions/`.
2. For each consecutive pair of events whose gap is ≤ `gap_minutes`, the
   duration is attributed to the later event's project / session and split
   across hour-of-day buckets. **A session's project is the enclosing git
   repo**, not the working directory's leaf folder — work in a subdirectory
   (`<repo>/docs/specs`) or in a git **worktree**
   (`<repo>/.claude-worktrees/<branch>`, including Claude Code's own isolated
   worktrees) rolls up to the repo instead of surfacing as its own project.
   The worktree/branch name is kept as the session's label. Names that older
   versions recorded as standalone projects are migrated on load and the
   rename map is persisted in `history.json`, so the fix holds even after the
   raw logs are pruned.
3. **Day categorisation** uses `work_days` + `work_intervals`:
   `work` (in-window weekday), `off` (out-of-window weekday), `wknd`
   (non-work day).
4. **Tokens**: every Claude `assistant` event's `usage` block (and every Codex
   `token_count` event's usage) is summed per day, broken down by model and by
   type (input / output / cache-write / cache-read).
5. **API cost**: each day's per-model tokens are multiplied by that model's
   published per-token rates (input / output / cache-write / cache-read) using
   the price snapshot effective on that date. Rates come from the `PRICES` table
   in `lib/generate.py`, optionally overridden by `output_dir/prices.json` (so
   you can correct or update prices without editing code). When rates change the
   next run **appends** a new dated snapshot to `history.json` and never
   overwrites the old ones, so past days stay costed at their original rates. If
   `prices.json` includes an `"effective": "YYYY-MM-DD"` (the real change date),
   the snapshot is stamped with it and backfills correctly even if the plugin
   only runs days later; otherwise it is stamped with the run date. A model with
   no rate is costed $0 and reported with a ⚠ warning.

   `prices.json` shape:
   ```json
   {
     "effective": "2026-06-09",
     "anthropic": { "fable-5": {"input": 10, "output": 50, "cache_write": 12.5, "cache_read": 1} },
     "openai":    { "gpt-5.5":  {"input": 5,  "output": 30, "cache_write": 0,    "cache_read": 0.5} }
   }
   ```
6. **Prompt-history backfill**: `~/.claude/history.jsonl` (the prompt log
   Claude Code keeps and does not prune together with the session JSONLs) is
   read as an extra stream of token-less activity markers carrying the real
   `sessionId`. Dense prompt runs recover active hours for periods whose
   session logs were already deleted — including periods **before the plugin
   was first installed**. Overlap with surviving logs is harmless: prompts are
   a sparse subset of the real event stream, hour merges take `max`, and
   session items dedup by `sessionId`. Backfilled time carries no tokens, so
   token/cost totals never inflate. Note this is a **lower bound**: only
   prompt-to-prompt gaps ≤ `gap_minutes` count, so sparse prompting during
   long autonomous turns is under-counted.
7. Output is merged with `~/.claude-activity/history.json` so months Claude
   Code later prunes from disk are preserved in the dashboard. Merge takes
   `max` per bucket — safe across pruning and re-runs. The union (`both`)
   source additionally **self-heals from the per-source history**: any
   day/hour present in `claude` or `codex` history but missing from `both`
   (the union schema postdates them, and a clobber would otherwise lose it
   for good) is folded back in — per-hour `max` is a safe lower bound for a
   union, and session items dedup by `sessionId`.
8. On each run the installed plugin **prunes its own stale cache versions** —
   sibling directories under `~/.claude/plugins/cache/.../claude-activity/` that
   belong to older releases (which may carry the legacy history writer or old
   hooks) are removed, so outdated code can't run and clobber data. This never
   touches `output_dir`, so saved history and token prices are unaffected.

## When history updates

History is refreshed **only when you run `/activity`** (or `generate.py`
directly). There are intentionally **no background `SessionStart` /
`SessionEnd` hooks**: an auto-run that ships out of sync with the on-disk
`history.json` schema can silently overwrite the file with whatever is still
in the logs and drop already-pruned months. Keeping the update explicit means
nothing rewrites your history behind your back — re-run `/activity` whenever
you want to fold in fresh sessions.

## Why a separate `history.json`

Claude Code periodically deletes older session JSONL files. Without history,
your heatmap would forget pruned months. `history.json` keeps only aggregates —
per-hour active seconds, per-day per-model token/cost breakdowns, per-project
token totals, the dated price snapshots, and the project-rename map (order of a
megabyte for months of dense usage) — far cheaper than backing up the raw
500 MB+ of JSONL.

**Self-healing.** Every run writes the freshly-merged result to a one-step
`history.json.bak` and a per-day `history-YYYY-MM-DD.bak.json` snapshot, and on
load it merges (union/max) across `history.json` **and every backup**. So even
if some outdated code overwrites `history.json` with fewer months, the next
proper run rebuilds the full history — and the token price book — from the
backups. Backups live in `output_dir`, never in the plugin cache, and only the
newest 14 per-day snapshots are kept.

Session activity is deduplicated by session id, so the per-project time filter
can't be inflated by a session's title changing between runs. `/activity
--compact` is a one-off cleanup that collapses any such legacy duplicates,
applies the persisted project-rename map (see step 2), and archives the
pre-compaction backups into `output_dir/pre-compact-<timestamp>/`.

## Comparison with `session-report` (Anthropic, official)

[`session-report`](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/session-report)
is a great companion — it focuses on **what** your tokens were spent on
(top prompts, cache efficiency, subagent breakdown) over a sliding window.

`claude-activity` is complementary: it focuses on **when** you worked,
visually, across calendar months, and preserves history across pruning.

## Privacy

- Reads from `~/.claude/projects/` and `~/.codex/sessions/` only.
- Writes only to `output_dir` (default `~/.claude-activity/`).
- Static HTML opened from `file://`; the only network request is the web fonts
  it loads from Google Fonts (falling back to system fonts offline). No usage
  data is ever transmitted.
- All token / session data stays on your disk.

## Requirements

- Python 3.9+ (no external Python packages required).
- A modern browser (Chrome, Safari, Firefox).

## License

MIT — see `LICENSE`.
