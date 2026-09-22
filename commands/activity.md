---
description: Generate and open a local heatmap of your Claude Code activity
allowed-tools: Bash(python3 *), Bash(open *), Bash(xdg-open *), Bash(test *), Bash(echo *), Bash(cat *), WebFetch, Read, Write
argument-hint: "[--settings | --compact | --help | --no-open]"
---

# /activity

Generate (or update) the local Claude Code activity heatmap dashboard.

## Silence rule (READ FIRST)

Run everything below **silently**. The only chat output allowed is:

- The `AskUserQuestion` UI itself (when the wizard fires).
- The final one-line summary in Step 4.
- The help block in Step 0 (only when `--help` was passed).

**Do not** narrate steps, do not explain what you are about to do, do not
comment on which step you are on, do not announce that the wizard will run,
do not describe how many AskUserQuestion calls you are making, do not
acknowledge "config exists / config missing" — just do it. The user knows
what `/activity` does; they invoked it deliberately.

If something fails (Python missing, generate.py error), report the error in
one line — nothing else.

## Step 0 — Handle `--help` / `-h`

If the user passed `--help`, `-h`, or `help` as the argument, print the
block below verbatim **and stop**. No other text.

```
/activity                  Generate the heatmap and open it in the browser.
/activity --no-open        Generate but do not open the browser.
/activity --settings       Re-run the setup wizard (work days, hours, gap…).
                            Aliases: --setup, --config, --reconfigure
/activity --compact        One-off maintenance: rebuild history with duplicate
                            session items collapsed and old backups archived.
/activity --help           Show this help.
                            Aliases: -h, help

Config file:  ~/.claude-activity/config.json
                gap_minutes, prompt_gap_minutes, work_intervals, work_days,
                first_day_of_week, auto_open, cache_read_weight, output_dir
Output:       ~/.claude-activity/index.html (the dashboard)
History:      ~/.claude-activity/history.json (preserves data after Claude
                Code prunes old session JSONL files; a one-step
                history.json.bak copy of the merged result is written on
                each run)

History is refreshed only when you run /activity (there are no background
SessionStart/SessionEnd hooks), so nothing rewrites it behind your back.
```

## Step 1 — Locate `lib/generate.py`

`${CLAUDE_PLUGIN_ROOT}/lib/generate.py` when running inside Claude Code.
Otherwise fall back to `../lib/generate.py` relative to this command file.

## Step 2 — First-run setup

Run `test -f ~/.claude-activity/config.json` silently.

- If the file does **not** exist, OR the user passed any of `--settings`,
  `--setup`, `--config`, `--reconfigure` — run the wizard (below).
- Otherwise skip straight to Step 3 (no acknowledgement in chat).

### Setup wizard

Make **one** `AskUserQuestion` tool call with all four questions at once.
No preface, no narration, no "running the wizard now."

1. **Work days** — single-select:
   - `Mon–Fri` (recommended) → `[0,1,2,3,4]`
   - `Mon–Sat` → `[0,1,2,3,4,5]`
   - `All 7 days` → `[0,1,2,3,4,5,6]`
   - `Custom` → ask follow-up multiSelect of MO/TU/WE/TH/FR/SA/SU

2. **Work hours** — single-select. Each preset maps to `work_intervals`
   (array of `[start_inclusive, end_exclusive]` pairs):
   - `09:00–18:00` (default) → `[[9, 18]]`
   - `09:00–13:00, 14:00–18:00` (with 1h lunch) → `[[9, 13], [14, 18]]`
   - `10:00–19:00` → `[[10, 19]]`
   - `08:00–17:00` → `[[8, 17]]`
   - `Custom` → follow-up with **3 questions in one AskUserQuestion call**:
     - **Start hour** (0–23, e.g. 9)
     - **End hour** (0–23, exclusive — e.g. 18)
     - **Lunch break** — single-select: `None`, `12:00–13:00`,
       `13:00–14:00`, `14:00–15:00`, or any other hour-range the user
       wants to specify via the "Other" free-text field.
     Build `work_intervals` from the answers:
       - `None` → `[[start, end]]`
       - lunch `[L, L+k]` → `[[start, L], [L+k, end]]` (split). Drop the
         second interval if `end <= L+k`, or the first if `start >= L`.

3. **Gap threshold** — single-select:
   - `10 min` (recommended), `5 min` (strict), `15 min`, `30 min` (lenient)

4. **First day of week** — single-select:
   - `Monday (ISO)` → `0`, `Sunday (US)` → `6`

Write the answers to `~/.claude-activity/config.json`. `auto_open` is always
`true` in the file (use `/activity --no-open` for one-off silent runs).

```bash
python3 -c "import json,os,pathlib; \
  p=pathlib.Path(os.path.expanduser('~/.claude-activity/config.json')); \
  p.parent.mkdir(parents=True, exist_ok=True); \
  p.write_text(json.dumps($CONFIG_DICT, indent=2))"
```

Substitute `$CONFIG_DICT` with a Python dict literal built from the answers,
e.g.:

```python
{
  "gap_minutes": 10,
  "prompt_gap_minutes": 20,
  "work_intervals": [[9, 18]],
  "work_days": [0, 1, 2, 3, 4],
  "first_day_of_week": 0,
  "auto_open": True,
  "cache_read_weight": 0.1,
  "output_dir": "~/.claude-activity"
}
```

## Step 2.5 — Refresh prices once per day (SILENT — no chat output)

Keep API prices current automatically. Do everything here silently; never
narrate it. Skip entirely if `--help`, `--settings`, or `--compact` was passed.

1. Gate to once per day:

```bash
python3 -c "import json,os,datetime; p=os.path.expanduser('~/.claude-activity/prices.json'); d=json.load(open(p)) if os.path.exists(p) else {}; print('SKIP' if d.get('fetched')==datetime.date.today().isoformat() else 'FETCH')"
```

If it prints `SKIP`, go straight to Step 3. Only continue when it prints `FETCH`.

2. Read the file you are about to update, so the refresh MERGES instead of
   replacing (`cat ~/.claude-activity/prices.json`, may not exist yet).

3. Fetch current pricing (best-effort; on any failure, do NOT write — keep the
   existing file and continue to Step 3). Read **every model row in the table**,
   not a fixed list: a model released after this command was written must still
   end up priced.
   - `WebFetch` `https://platform.claude.com/docs/en/about-claude/pricing`
     (follow the redirect if returned). From the **Model pricing** table read,
     per model: *Base input tokens* → `input`, *5m cache writes* → `cache_write`,
     *1h cache writes* → `cache_write_1h`, *Cache hits and refreshes* →
     `cache_read`, *Output tokens* → `output`. Strip `$` and `/ MTok`; keep the
     USD-per-million number. Include retired rows too — old usage of them still
     has to be costed. Mind the footnotes: some families read cache at a
     different multiplier than the usual 0.1x.
   - Same page, **Fast mode pricing**: each model listed there gets a second
     entry keyed `<model>-fast` (e.g. `opus-5-fast`) with that table's input
     and output, and cache rates derived from its fast input using THAT
     model's own cache multipliers from the main table — `cache_write` =
     1.25x, `cache_write_1h` = 2x, and `cache_read` at the model's own ratio
     (0.1x usually, but e.g. 0.05x for Opus 5.5 — take cache_read / input from
     its standard row). The logs record the speed per request, so fast-mode
     turns are costed apart from standard ones.
   - `WebFetch` `https://platform.openai.com/docs/pricing` (follow redirect).
     Read every model row: standard `input`, cached input → `cache_read`,
     `output`; `cache_write` = 0. Use the **standard** (not long-context, batch,
     flex or priority) tier. Where a row has no cached price, use 0.

4. Key naming — the key must match what `normalize_model()` in generate.py
   produces from the model id in the logs:
   - Anthropic: drop the leading `Claude `, lowercase, and replace spaces and
     dots with `-`. "Claude Opus 4.8" → `opus-4-8`, "Claude Fable 5.1" →
     `fable-5-1`, "Claude Haiku 4.5" → `haiku-4-5`.
   - OpenAI: the model id exactly as the pricing page spells it — `gpt-5.5`,
     `gpt-5.5-pro`, `gpt-6-astra`, `gpt-5.6-sol`. OpenAI has no cache-write
     charge: `cache_write` and `cache_write_1h` are 0.

   Skip only rows you genuinely cannot read; never skip a row merely because
   it is unfamiliar — an unknown new model is exactly what this step is for.

5. `Write` `~/.claude-activity/prices.json` with the MERGED result: start from
   what step 2 read, overwrite the models you just read, ADD models that were
   not there before, and **keep models the pages no longer list**. A model
   dropping off a page is not a price change; deleting it here would silently
   re-cost every past day that used it.

```json
{"fetched":"<today YYYY-MM-DD>","anthropic":{"<key>":{"input":N,"output":N,"cache_write":N,"cache_write_1h":N,"cache_read":N}},"openai":{"<key>":{...}}}
```

   Add `"effective":"YYYY-MM-DD"` ONLY if the page states the new rates start on
   a specific date; otherwise omit it (generate.py stamps the snapshot with the
   run date).

6. Sanity-check before writing. generate.py ignores out-of-range values as a
   safety net, but a *plausible* wrong number gets through and is written into
   the dated price history, where it stays. Re-read any value where
   `cache_read` equals `input`, or where a rate moved by more than ~2x since
   the previous file — those are almost always a column misread, not a real
   price change.

## Step 3 — Generate

Silently run:

```bash
python3 <path-to-generate.py>
```

(Append ` --no-open` if the user passed `--no-open`, and ` --compact` if the
user passed `--compact`, on the command line.)
The script writes `~/.claude-activity/index.html` and opens it in the
browser when `auto_open` is true.

## Step 4 — Report

Output exactly one short line, e.g.:

```
85h 6m across 4 months · 1.25B tokens · ~/.claude-activity/index.html
```

If "merged with history" totals significantly exceed "this run" (Claude
Code pruned some logs), add a second line noting the history saved that
data. Otherwise no extra text.

## Notes (for the user — not for you to repeat in chat)

- Change settings later: `/activity --settings` (also `--setup`, `--config`,
  `--reconfigure`).
- Manual config edit: `~/.claude-activity/config.json`.
- Output dashboard: `~/.claude-activity/index.html`.
