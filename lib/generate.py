#!/usr/bin/env python3
"""
claude-activity — local heatmap of Claude Code usage time.

Reads ~/.claude/projects/**/*.jsonl, sums inter-event gaps below the gap
threshold, attributes time to projects & sessions, embeds the result into a
self-contained HTML dashboard.

Config: ~/.claude-activity/config.json (created with defaults on first run).
Output: <output_dir>/index.html and <output_dir>/history.json
        (default <output_dir> = ~/.claude-activity).

A merged history file is maintained so months that Claude Code later prunes
from disk are preserved in the dashboard.
"""

import json
import os
import re
import shutil
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

# ---------- Defaults & config ----------
DEFAULTS = {
    "gap_minutes": 10,
    # Each interval is [start_hour_inclusive, end_hour_exclusive], 0..23.
    # Multiple intervals are useful for splitting around a lunch break,
    # e.g. [[9, 12], [13, 18]] (no work between 12 and 13).
    "work_intervals": [[9, 18]],
    # Weekday indices where 0=Mon, 6=Sun
    "work_days": [0, 1, 2, 3, 4],
    # 0=Mon (ISO), 6=Sun (US-style) — affects By-day-of-week chart ordering
    "first_day_of_week": 0,
    "auto_open": True,
    "cache_read_weight": 0.1,
    "output_dir": "~/.claude-activity",
}


def normalize_config(cfg):
    """Migrate the legacy work_hour_start/end pair to work_intervals."""
    if "work_intervals" not in cfg:
        if "work_hour_start" in cfg and "work_hour_end" in cfg:
            cfg["work_intervals"] = [[int(cfg["work_hour_start"]),
                                      int(cfg["work_hour_end"])]]
    # Always strip the legacy keys to keep the canonical schema clean.
    cfg.pop("work_hour_start", None)
    cfg.pop("work_hour_end", None)
    return cfg

PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE_FILE = SCRIPT_DIR / "index.html"
CONFIG_FILE = Path.home() / ".claude-activity" / "config.json"
LOGO_FILES = {
    "claude": SCRIPT_DIR.parent / "docs" / "claude.svg",
    "codex":  SCRIPT_DIR.parent / "docs" / "openai.svg",
}

SOURCES = ("both", "claude", "codex")

# Max distinct (project, title, source) entries kept per hour. The tooltip shows
# only the top few, but the client also aggregates this list by project for the
# project filter, so keep enough that a busy hour isn't truncated.
MAX_SESSIONS_PER_HOUR = 30


# ---------- Token pricing (USD per million tokens) ----------
# Each run records the current snapshot into history.json tagged with an
# effective date; old snapshots are never deleted, so every day is costed at
# the rates in effect on that day (historicity). When a published price
# changes, edit PRICES below — the next run appends a new dated snapshot.
PRICE_EFFECTIVE = "2026-01-01"   # effective date for the baseline snapshot
PRICES = {
    "anthropic": {
        #              input  output  cache_write(5m)  cache_read
        "opus-4-8":   {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
        "opus-4-7":   {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
        "opus-4-6":   {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
        "sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
        "sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.30},
        "haiku-4-5":  {"input": 1.0, "output": 5.0,  "cache_write": 1.25, "cache_read": 0.10},
    },
    "openai": {
        # Codex. No cache-creation counter; cached input maps to cache_read.
        "gpt-5.5":     {"input": 5.0,  "output": 30.0,  "cache_write": 0.0, "cache_read": 0.50},
        "gpt-5.5-pro": {"input": 30.0, "output": 180.0, "cache_write": 0.0, "cache_read": 0.0},
    },
}


def normalize_model(name):
    """Map a raw model id to a price-table key.
    claude-opus-4-7 → opus-4-7 ; claude-haiku-4-5-20251001 → haiku-4-5 ;
    claude-opus-4-8[1m] → opus-4-8 (1M context is standard-priced) ;
    bare 'opus' → opus-4-7 ; '<synthetic>'/empty → None (no cost)."""
    if not name:
        return None
    n = name.strip().split("[")[0]            # drop context-window suffix like [1m]
    if not n or n == "<synthetic>":
        return None
    if n.startswith("claude-"):
        parts = n[len("claude-"):].split("-")
        return "-".join(parts[:3]) if len(parts) >= 3 else "-".join(parts)
    if n == "opus":
        return "opus-4-7"
    return n                                  # gpt-5.5 etc. pass through


def _provider_for_model(model):
    if not model:
        return None
    return "openai" if model.startswith(("gpt", "o1", "o3", "o4")) else "anthropic"


def _rate_for(snapshot, model):
    """Find a rate dict for `model` in a price snapshot, with a same-family
    fallback (an unseen opus-x.y falls back to the newest opus entry)."""
    prov = _provider_for_model(model)
    table = (snapshot or {}).get(prov, {}) if prov else {}
    if model in table:
        return table[model]
    fam = model.split("-")[0] if model else ""
    fam_keys = sorted(k for k in table if k.split("-")[0] == fam)
    return table[fam_keys[-1]] if fam_keys else None


def _snapshot_for_date(prices, date_str):
    """Pick the price snapshot effective on `date_str` (latest effective <= date,
    else the earliest available)."""
    if not prices:
        return None
    ordered = sorted(prices, key=lambda s: s.get("effective", ""))
    chosen = ordered[0]
    for snap in ordered:
        if snap.get("effective", "") <= date_str:
            chosen = snap
    return chosen


def cost_of_models(models_dict, snapshot):
    """USD cost of one day's per-model token breakdown under a price snapshot."""
    if not models_dict or not snapshot:
        return 0.0
    total = 0.0
    for model, tk in models_dict.items():
        r = _rate_for(snapshot, model)
        if not r:
            continue
        total += (
            tk.get("input", 0)        * r["input"]
            + tk.get("output", 0)       * r["output"]
            + tk.get("cache_read", 0)   * r["cache_read"]
            + tk.get("cache_create", 0) * r["cache_write"]
        ) / 1_000_000.0
    return total


def _current_price_snapshot():
    return {"anthropic": dict(PRICES["anthropic"]), "openai": dict(PRICES["openai"])}


def update_price_book(history_prices):
    """Reflect the current PRICES while preserving history. Seeds a baseline
    snapshot if empty; appends a today-dated snapshot when current rates differ
    from the latest stored ones."""
    cur = _current_price_snapshot()
    rates = lambda s: {k: v for k, v in s.items() if k != "effective"}
    if not history_prices:
        return [{"effective": PRICE_EFFECTIVE, **cur}]
    ordered = sorted(history_prices, key=lambda s: s.get("effective", ""))
    if rates(ordered[-1]) == rates(cur):
        return ordered
    today = datetime.now().astimezone().date().isoformat()
    ordered = [s for s in ordered if s.get("effective") != today]
    ordered.append({"effective": today, **cur})
    return sorted(ordered, key=lambda s: s.get("effective", ""))


def apply_costs(merged_months, prices):
    """Attach day['cost'] and month['cost_total'] from per-model tokens and the
    date-effective price snapshot. Returns the grand total across months."""
    grand = 0.0
    for mkey, mval in merged_months.items():
        mtotal = 0.0
        for dkey, day in mval.get("days", {}).items():
            snap = _snapshot_for_date(prices, f"{mkey}-{dkey}")
            c = cost_of_models(day.get("models"), snap)
            day["cost"] = round(c, 4)
            mtotal += c
            pm = day.get("proj_models")
            if pm:
                day["proj_cost"] = {
                    proj: round(cost_of_models(models, snap), 4)
                    for proj, models in pm.items()
                }
        mval["cost_total"] = round(mtotal, 2)
        grand += mtotal
    return round(grand, 2)


def _sanitize_svg(svg_text):
    """Inline-friendly the SVG: drop XML PI / comments / <title>, force
    currentColor fill, strip width/height so CSS controls size."""
    svg_text = re.sub(r"<\?xml[^>]*\?>", "", svg_text)
    svg_text = re.sub(r"<!--.*?-->", "", svg_text, flags=re.DOTALL)
    # Drop <title>…</title> — otherwise their text bleeds into our badge label
    svg_text = re.sub(r"<title>.*?</title>", "", svg_text, flags=re.DOTALL).strip()
    m = re.match(r"<svg([^>]*)>", svg_text)
    if not m:
        return svg_text
    attrs = m.group(1)
    attrs = re.sub(r'\s+(?:width|height)\s*=\s*"[^"]*"', "", attrs)
    if re.search(r'\bfill\s*=', attrs):
        attrs = re.sub(r'\bfill\s*=\s*"[^"]*"', 'fill="currentColor"', attrs)
    else:
        attrs = ' fill="currentColor"' + attrs
    return "<svg" + attrs + ">" + svg_text[m.end():]


def load_brand_logos():
    """Return {source: sanitized SVG markup} for any logo files found.
    Sources without a file are omitted; the HTML keeps its built-in fallback."""
    out = {}
    for src, path in LOGO_FILES.items():
        if not path.exists():
            continue
        try:
            out[src] = _sanitize_svg(path.read_text())
        except OSError:
            continue
    return out


def load_config():
    """Read config from ~/.claude-activity/config.json.

    If the file is missing, fall back to in-memory defaults *without writing
    anything to disk*. The config file is only created by the /activity setup
    wizard so that its presence is a reliable signal of "user has configured."
    """
    if CONFIG_FILE.exists():
        try:
            user_cfg = json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ! config error ({e}), using defaults")
            user_cfg = {}
    else:
        print(f"  no config yet — using defaults "
              f"(run /activity --settings in Claude Code to customise)")
        user_cfg = {}
    # Migrate legacy keys on the user config BEFORE merging with DEFAULTS, so
    # that the merge doesn't mask the legacy fields with the default array.
    user_cfg = normalize_config(user_cfg)
    return {**DEFAULTS, **user_cfg}


# ---------- JSONL parsing ----------
def project_name_from_cwd(cwd):
    if not cwd:
        return "unknown"
    p = Path(cwd)
    # Attribute to the enclosing git repo's folder name, so work in a nested
    # subdirectory (e.g. <repo>/docs/specs or
    # <repo>/docs/product-team/<ticket>) rolls up to the repo instead of
    # surfacing the leaf folder as its own "project". Falls back to the leaf
    # segment when the path is not inside a git repo (or no longer exists).
    for anc in (p, *p.parents):
        try:
            if (anc / ".git").exists():
                return anc.name or cwd
        except OSError:
            break
    return p.name or cwd


def cwd_sublabel(cwd):
    """Leaf subfolder name when cwd is *inside* a git repo (below its root) —
    used as a human-label fallback so sessions rolled up to the repo aren't all
    'no title'. Empty when cwd is the repo root or not inside a repo."""
    if not cwd:
        return ""
    p = Path(cwd)
    for anc in (p, *p.parents):
        try:
            if (anc / ".git").exists():
                return "" if anc == p else p.name
        except OSError:
            break
    return ""


def extract_tokens(obj):
    if obj.get("type") != "assistant":
        return None
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "cache_create": int(usage.get("cache_creation_input_tokens") or 0),
        "model": normalize_model(msg.get("model")),
    }


def collect_claude():
    """Read ~/.claude/projects JSONL → list of events tagged source='claude'.

    Event tuple: (ts, tokens_dict_or_None, session_id, project, source).
    """
    events = []
    session_meta = {}
    files = list(PROJECTS_DIR.glob("*/*.jsonl")) + list(
        PROJECTS_DIR.glob("*/*/subagents/*.jsonl")
    )
    for jp in files:
        try:
            with open(jp) as f:
                file_sid = file_proj = file_title = file_last = None
                file_cwd = file_branch = None
                lines = f.readlines()

                for line in lines:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    sid = obj.get("sessionId")
                    if sid and not file_sid:
                        file_sid = sid
                    cwd = obj.get("cwd")
                    if cwd and not file_proj:
                        file_proj = project_name_from_cwd(cwd)
                        file_cwd = cwd
                    br = obj.get("gitBranch")
                    if isinstance(br, str) and br.strip():
                        file_branch = br.strip()
                    if obj.get("type") == "ai-title":
                        t = obj.get("aiTitle")
                        if isinstance(t, str) and t.strip():
                            file_title = t.strip()
                    elif obj.get("type") == "last-prompt":
                        lp = obj.get("lastPrompt")
                        if isinstance(lp, str) and lp.strip():
                            file_last = lp.strip()

                if file_sid:
                    # Fallback chain so rolled-up sessions still read sensibly:
                    # AI title → last prompt → git branch → repo subfolder.
                    sub = cwd_sublabel(file_cwd)
                    title = file_title or file_last or file_branch or sub or ""
                    if len(title) > 80:
                        title = title[:77] + "…"
                    rank = (4 if file_title else 3 if file_last
                            else 2 if file_branch else 1 if sub else 0)
                    # Subagent files share the parent's sessionId and carry no
                    # title — keep the richest entry instead of letting an empty
                    # one clobber it.
                    prev = session_meta.get(file_sid)
                    if prev is None or rank > prev.get("_rank", -1):
                        session_meta[file_sid] = {
                            "project": file_proj or "unknown",
                            "title": title,
                            "source": "claude",
                            "_rank": rank,
                        }

                for line in lines:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    ts_str = obj.get("timestamp")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone()
                    except ValueError:
                        continue
                    sid = obj.get("sessionId") or file_sid or ""
                    proj = (
                        project_name_from_cwd(obj.get("cwd"))
                        if obj.get("cwd")
                        else file_proj or "unknown"
                    )
                    events.append((ts, extract_tokens(obj), sid, proj, "claude"))
        except OSError:
            continue
    events.sort(key=lambda e: e[0])
    return events, session_meta


def _codex_tokens_from_event(payload, model=None):
    """Codex `event_msg.payload.type=token_count` carries per-turn usage in
    `last_token_usage`. Map onto the same shape as Claude's extract_tokens().
    `model` is captured from the file's turn_context records (not the event)."""
    if not isinstance(payload, dict):
        return None
    if payload.get("type") != "token_count":
        return None
    info = payload.get("info") or {}
    usage = info.get("last_token_usage") or {}
    if not isinstance(usage, dict):
        return None
    return {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0)
              + int(usage.get("reasoning_output_tokens") or 0),
        "cache_read": int(usage.get("cached_input_tokens") or 0),
        "cache_create": 0,  # Codex has no cache-creation counter
        "model": model,
    }


def collect_codex():
    """Read ~/.codex/sessions/**/*.jsonl → list of events tagged source='codex'.

    Codex format: each file is a rollout. Important record types:
      - `session_meta` (one per file, first line): payload.id, payload.cwd
      - `event_msg` with payload.type in {user_message, agent_message,
        token_count, task_started, ...}: timestamped activity.
    First user_message text becomes the session title fallback.
    """
    if not CODEX_SESSIONS_DIR.exists():
        return [], {}

    events = []
    session_meta = {}
    files = list(CODEX_SESSIONS_DIR.rglob("*.jsonl"))

    for jp in files:
        try:
            with open(jp) as f:
                lines = f.readlines()
        except OSError:
            continue

        file_sid = None
        file_proj = "unknown"
        file_title = ""
        file_model = None
        file_cwd = None

        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            t = obj.get("type")
            payload = obj.get("payload") or {}
            if t == "session_meta":
                file_sid = payload.get("id") or file_sid
                cwd = payload.get("cwd")
                if cwd:
                    file_proj = project_name_from_cwd(cwd)
                    file_cwd = cwd
            elif t == "turn_context":
                mdl = payload.get("model") or obj.get("model")
                if mdl:
                    file_model = mdl
            elif t == "event_msg" and payload.get("type") == "user_message" and not file_title:
                msg = payload.get("message")
                if isinstance(msg, str) and msg.strip():
                    file_title = msg.strip().splitlines()[0]
                    if len(file_title) > 80:
                        file_title = file_title[:77] + "…"

        if file_sid:
            session_meta[file_sid] = {
                "project": file_proj,
                "title": file_title or cwd_sublabel(file_cwd),
                "source": "codex",
            }

        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") != "event_msg":
                continue
            payload = obj.get("payload") or {}
            ptype = payload.get("type")
            if ptype not in ("user_message", "agent_message", "token_count"):
                continue
            ts_str = obj.get("timestamp")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone()
            except ValueError:
                continue
            tokens = (_codex_tokens_from_event(payload, normalize_model(file_model))
                      if ptype == "token_count" else None)
            events.append((ts, tokens, file_sid or "", file_proj, "codex"))

    events.sort(key=lambda e: e[0])
    return events, session_meta


def collect():
    """Collect events from all sources. Returns:
        events_by_source = {
          "claude": [...],
          "codex":  [...],
          "both":   [...]   # sorted union of the two — for Union-mode active time
        }
        session_meta = {sid: {project, title}}   # merged
    """
    claude_events, claude_meta = collect_claude()
    codex_events, codex_meta = collect_codex()
    both_events = sorted(claude_events + codex_events, key=lambda e: e[0])
    session_meta = {**claude_meta, **codex_meta}
    return {
        "claude": claude_events,
        "codex":  codex_events,
        "both":   both_events,
    }, session_meta


# ---------- Bucketing ----------
def distribute_interval(start, end, sid, hours, sessions):
    cur = start
    while cur < end:
        nxt = (cur.replace(minute=0, second=0, microsecond=0)
               + timedelta(hours=1))
        sl = min(end, nxt)
        sec = (sl - cur).total_seconds()
        key = (cur.year, cur.month, cur.day, cur.hour)
        hours[key] += sec
        sessions[key][sid] += sec
        cur = sl


def build_buckets(events, gap_limit, cache_read_weight):
    hour_b = defaultdict(float)
    session_b = defaultdict(lambda: defaultdict(float))
    daily_tokens = defaultdict(lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "all": 0,
    })
    # Per-model breakdown per day, for accurate API-cost pricing.
    daily_models = defaultdict(lambda: defaultdict(lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_create": 0,
    }))
    # Per-project: billable token total per day, and per-model split per day
    # (so tokens/cost can be filtered by project too).
    daily_proj_tokens = defaultdict(lambda: defaultdict(int))
    daily_proj_models = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {
        "input": 0, "output": 0, "cache_read": 0, "cache_create": 0,
    })))

    for i in range(1, len(events)):
        ts_prev = events[i - 1][0]
        ts_cur, _, sid, *_ = events[i]
        if ts_cur - ts_prev <= gap_limit:
            distribute_interval(ts_prev, ts_cur, sid, hour_b, session_b)

    for ev in events:
        ts, tok = ev[0], ev[1]
        if not tok:
            continue
        proj = ev[3] if len(ev) > 3 and ev[3] else "unknown"
        k = (ts.year, ts.month, ts.day)
        for f in ("input", "output", "cache_read", "cache_create"):
            daily_tokens[k][f] += tok[f]
        billable = (
            tok["input"] + tok["output"] + tok["cache_create"]
            + int(tok["cache_read"] * cache_read_weight)
        )
        daily_tokens[k]["all"] += billable
        daily_proj_tokens[k][proj] += billable
        model = tok.get("model")
        if model:
            mb = daily_models[k][model]
            pmb = daily_proj_models[k][proj][model]
            for f in ("input", "output", "cache_read", "cache_create"):
                mb[f] += tok[f]
                pmb[f] += tok[f]
    return hour_b, session_b, daily_tokens, daily_models, daily_proj_tokens, daily_proj_models


def shape_output(hour_b, session_b, daily_tokens, daily_models, session_meta,
                 daily_proj_tokens=None, daily_proj_models=None):
    daily_proj_tokens = daily_proj_tokens or {}
    daily_proj_models = daily_proj_models or {}
    months = defaultdict(lambda: defaultdict(lambda: {
        "hours": {}, "sessions": {}, "total": 0, "tokens": None, "models": None,
        "proj_tokens": None, "proj_models": None,
    }))
    for (y, m, d, h), sec in hour_b.items():
        if sec <= 0:
            continue
        months[f"{y:04d}-{m:02d}"][f"{d:02d}"]["hours"][str(h)] = round(sec)

    for (y, m, d, h), sid_map in session_b.items():
        merged = {}
        for sid, sec in sid_map.items():
            if sec < 1:
                continue
            meta = session_meta.get(sid, {})
            key = (
                meta.get("project", "unknown"),
                meta.get("title", ""),
                meta.get("source", ""),
            )
            merged[key] = merged.get(key, 0) + sec
        items = [
            {"project": p, "title": t, "source": s, "sec": round(sec)}
            for (p, t, s), sec in merged.items()
        ]
        items.sort(key=lambda x: -x["sec"])
        if items:
            months[f"{y:04d}-{m:02d}"][f"{d:02d}"]["sessions"][str(h)] = items[:MAX_SESSIONS_PER_HOUR]

    for (y, m, d), tk in daily_tokens.items():
        months[f"{y:04d}-{m:02d}"][f"{d:02d}"]["tokens"] = tk

    for (y, m, d), mm in daily_models.items():
        months[f"{y:04d}-{m:02d}"][f"{d:02d}"]["models"] = {
            mdl: dict(v) for mdl, v in mm.items()
        }

    for (y, m, d), pt in daily_proj_tokens.items():
        months[f"{y:04d}-{m:02d}"][f"{d:02d}"]["proj_tokens"] = dict(pt)

    for (y, m, d), pm in daily_proj_models.items():
        months[f"{y:04d}-{m:02d}"][f"{d:02d}"]["proj_models"] = {
            proj: {mdl: dict(v) for mdl, v in models.items()}
            for proj, models in pm.items()
        }

    out = {}
    for mkey, days in months.items():
        out_days = {}
        for dkey, day in days.items():
            day["total"] = sum(day["hours"].values())
            out_days[dkey] = day
        out[mkey] = {
            "days": out_days,
            "total": sum(d["total"] for d in out_days.values()),
            "tokens_total": sum(
                d["tokens"]["all"] for d in out_days.values() if d.get("tokens")
            ),
        }
    return out


# ---------- History merge ----------
def merge_hour_dicts(a, b):
    return {k: max(a.get(k, 0), b.get(k, 0)) for k in set(a) | set(b)}


def merge_sessions(a, b):
    out = {}
    for hkey in set(a) | set(b):
        by_key = {}
        for item in (a.get(hkey) or []) + (b.get(hkey) or []):
            # Legacy history items (pre-Codex schema) had no `source` field;
            # back then we tracked only Claude, so default to that.
            k = (
                item.get("project", "unknown"),
                item.get("title", ""),
                item.get("source") or "claude",
            )
            by_key[k] = max(by_key.get(k, 0), int(item.get("sec", 0)))
        merged = sorted(
            [{"project": p, "title": t, "source": s, "sec": sec}
             for (p, t, s), sec in by_key.items()],
            key=lambda x: -x["sec"],
        )[:MAX_SESSIONS_PER_HOUR]
        out[hkey] = merged
    return out


def merge_tokens(a, b):
    if not a and not b:
        return None
    if not a:
        return dict(b)
    if not b:
        return dict(a)
    return {k: max(a.get(k, 0), b.get(k, 0)) for k in set(a) | set(b)}


def merge_models(a, b):
    """Per-model, per-token-type max — same pruning-safe semantics as tokens."""
    if not a and not b:
        return None
    out = {}
    for mdl in set(a or {}) | set(b or {}):
        av = (a or {}).get(mdl, {})
        bv = (b or {}).get(mdl, {})
        out[mdl] = {
            f: max(av.get(f, 0), bv.get(f, 0))
            for f in ("input", "output", "cache_read", "cache_create")
        }
    return out


def merge_proj_tokens(a, b):
    """Per-project billable-token total — pruning-safe max, like merge_tokens."""
    if not a and not b:
        return None
    return {p: max((a or {}).get(p, 0), (b or {}).get(p, 0))
            for p in set(a or {}) | set(b or {})}


def merge_proj_models(a, b):
    """Per-project, per-model token split — merge_models per project."""
    if not a and not b:
        return None
    return {p: merge_models((a or {}).get(p), (b or {}).get(p))
            for p in set(a or {}) | set(b or {})}


def merge_months(current, history):
    merged = {}
    for mkey in set(current) | set(history):
        c_days = current.get(mkey, {}).get("days", {})
        h_days = history.get(mkey, {}).get("days", {})
        days = {}
        for dkey in set(c_days) | set(h_days):
            cd = c_days.get(dkey, {})
            hd = h_days.get(dkey, {})
            hours = merge_hour_dicts(cd.get("hours", {}), hd.get("hours", {}))
            day = {
                "hours": hours,
                "sessions": merge_sessions(cd.get("sessions", {}), hd.get("sessions", {})),
                "tokens": merge_tokens(cd.get("tokens"), hd.get("tokens")),
                "models": merge_models(cd.get("models"), hd.get("models")),
                "proj_tokens": merge_proj_tokens(cd.get("proj_tokens"), hd.get("proj_tokens")),
                "proj_models": merge_proj_models(cd.get("proj_models"), hd.get("proj_models")),
                "total": sum(hours.values()),
            }
            days[dkey] = day
        merged[mkey] = {
            "days": days,
            "total": sum(d["total"] for d in days.values()),
            "tokens_total": sum(
                d["tokens"]["all"] for d in days.values()
                if d.get("tokens") and "all" in d["tokens"]
            ),
        }
    return merged


# ---------- Output ----------
DATA_BLOCK_RE = re.compile(
    r"(/\* DATA_START \*/)(.*?)(/\* DATA_END \*/)", re.DOTALL
)


def render_html(template_path, output_path, payload_json):
    if not template_path.exists():
        raise FileNotFoundError(f"template not found: {template_path}")
    html = template_path.read_text()
    new_block = f"\nwindow.CLAUDE_ACTIVITY_DATA = {payload_json};\n"
    if not DATA_BLOCK_RE.search(html):
        raise RuntimeError("DATA_START / DATA_END markers missing in template")
    new_html = DATA_BLOCK_RE.sub(
        lambda m: m.group(1) + new_block + m.group(3), html, count=1
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(new_html)


def _strip_alien_session_sources(months, expected_source):
    """Remove session items whose `source` field doesn't match the bucket's
    source key. Hygiene for buckets that should be source-pure (claude/codex).
    Earlier bugs let mixed-source items leak in; this clamps them out on load
    so the next history write is clean."""
    for mval in months.values():
        for dval in (mval.get("days") or {}).values():
            sessions = dval.get("sessions") or {}
            for hkey in list(sessions.keys()):
                items = sessions[hkey] or []
                filtered = [
                    s for s in items
                    if (s.get("source") or "claude") == expected_source
                ]
                if filtered:
                    sessions[hkey] = filtered
                else:
                    del sessions[hkey]
    return months


def _iter_history_files(history_file):
    """The live history.json plus every `history*.json*` backup in the output
    dir, de-duplicated. Reading from all of them is what makes history
    self-healing: if an outdated generate.py clobbers history.json, the next
    proper run rebuilds the full picture from the backups."""
    output_dir = history_file.parent
    seen, files = set(), []
    candidates = [history_file]
    try:
        candidates += sorted(output_dir.glob("history*.json*"))
    except OSError:
        pass
    for p in candidates:
        try:
            if not p.exists():
                continue
            rp = p.resolve()
        except OSError:
            continue
        if rp not in seen:
            seen.add(rp)
            files.append(p)
    return files


def _parse_history_sources(raw):
    """One file's raw JSON → {source: months_dict}, handling the legacy
    single-source schema and stripping alien-source session items."""
    out = {s: {} for s in SOURCES}
    if isinstance(raw.get("sources"), dict):
        for s in SOURCES:
            months = (raw["sources"].get(s) or {}).get("months") or {}
            if s in ("claude", "codex"):
                months = _strip_alien_session_sources(months, s)
            out[s] = months
    elif isinstance(raw.get("months"), dict):
        out["claude"] = _strip_alien_session_sources(raw["months"], "claude")
    return out


def _load_history_sources(history_file):
    """Return {source: months_dict}, merged (union/max) across history.json AND
    every backup snapshot — self-healing against a clobber by stale code."""
    out = {s: {} for s in SOURCES}
    for p in _iter_history_files(history_file):
        try:
            raw = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        parsed = _parse_history_sources(raw)
        for s in SOURCES:
            out[s] = merge_months(parsed[s], out[s])
    return out


def _load_history_prices(history_file):
    """Union of dated price snapshots across history.json and all backups, so a
    clobber that drops the `prices` section is healed from the backups."""
    by_eff = {}
    for p in _iter_history_files(history_file):
        try:
            raw = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for snap in (raw.get("prices") or []):
            eff = snap.get("effective")
            if eff and eff not in by_eff:
                by_eff[eff] = snap
    return [by_eff[e] for e in sorted(by_eff)]


def _fmt_hours(secs):
    return f"{int(secs // 3600)} h {int((secs % 3600) // 60)} min"


def prune_stale_cache_versions():
    """When running as the *installed* plugin, delete sibling cache directories
    of other (stale) plugin versions, so an outdated cached generate.py — which
    may write the legacy history format or still carry SessionStart/SessionEnd
    hooks — can never run again and clobber data.

    Safety:
    - No-op unless this file lives under .../.claude/plugins/cache/.../<ver>/
      (i.e. running from a source checkout does nothing).
    - Never removes its own version directory.
    - Never touches the output dir: history.json and the price book live in
      output_dir (default ~/.claude-activity), a completely separate tree, so
      cleaning the cache cannot affect saved history or token prices.
    """
    here = Path(__file__).resolve()
    parts = here.parts
    if "plugins" not in parts or "cache" not in parts:
        return  # source checkout — nothing to prune
    try:
        version_dir = here.parents[1]              # .../<ver>
        versions_root = here.parents[2]            # .../claude-activity/claude-activity
        if versions_root.name != "claude-activity":
            return
        removed = []
        for child in versions_root.iterdir():
            if child.is_dir() and child.resolve() != version_dir:
                shutil.rmtree(child, ignore_errors=True)
                if not child.exists():
                    removed.append(child.name)
        if removed:
            print(f"  pruned stale plugin versions: {', '.join(sorted(removed))}")
    except OSError:
        pass


def main():
    prune_stale_cache_versions()
    cfg = load_config()
    gap_limit = timedelta(minutes=int(cfg["gap_minutes"]))
    work_intervals = [
        [int(s), int(e)] for s, e in cfg.get("work_intervals", DEFAULTS["work_intervals"])
    ]
    work_days = list(cfg.get("work_days", DEFAULTS["work_days"]))
    first_day_of_week = int(cfg.get("first_day_of_week", DEFAULTS["first_day_of_week"]))
    auto_open = bool(cfg.get("auto_open", DEFAULTS["auto_open"]))
    cache_read_weight = float(cfg["cache_read_weight"])
    output_dir = Path(os.path.expanduser(cfg["output_dir"]))
    history_file = output_dir / "history.json"
    output_html = output_dir / "index.html"

    print("Reading JSONL logs ...")
    print(f"  Claude:  {PROJECTS_DIR}")
    print(f"  Codex:   {CODEX_SESSIONS_DIR}")
    events_by_source, session_meta = collect()
    counts = {s: len(events_by_source[s]) for s in SOURCES}
    print(f"  events found:    "
          f"claude {counts['claude']:,} · codex {counts['codex']:,} "
          f"· both {counts['both']:,}")
    print(f"  sessions seen:   {len(session_meta):,}")
    if not events_by_source["both"]:
        print("No events — nothing to write.")
        return 0

    print("Building buckets ...")
    history_by_source = _load_history_sources(history_file)
    price_book = update_price_book(_load_history_prices(history_file))
    sources_payload = {}
    sources_history = {}

    for src in SOURCES:
        ev = events_by_source[src]
        hour_b, sess_b, day_tok, day_models, day_proj_tok, day_proj_models = build_buckets(ev, gap_limit, cache_read_weight)
        current_months = shape_output(hour_b, sess_b, day_tok, day_models, session_meta,
                                      day_proj_tok, day_proj_models)
        merged_months = merge_months(current_months, history_by_source.get(src, {}))
        total_cost = apply_costs(merged_months, price_book)
        available = sorted(merged_months.keys())
        total_sec_run = sum(hour_b.values())
        total_sec_merged = sum(m["total"] for m in merged_months.values())
        total_tokens = sum(m.get("tokens_total", 0) for m in merged_months.values())
        sources_payload[src] = {
            "available_months": available,
            "months": merged_months,
            "total_seconds": total_sec_merged,
            "total_tokens": total_tokens,
            "total_cost": total_cost,
            "events_count": len(ev),
        }
        sources_history[src] = {"months": merged_months}
        print(f"  [{src:>6}] this run: {_fmt_hours(total_sec_run)} · "
              f"merged: {_fmt_hours(total_sec_merged)} · "
              f"tokens: {total_tokens:,} · ~${total_cost:,.0f} API")

    # Persist history with new schema (drop legacy top-level "months" key).
    history_file.parent.mkdir(parents=True, exist_ok=True)
    history_text = json.dumps(
        {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
         "sources": sources_history,
         "prices": price_book},
        ensure_ascii=False, indent=2,
    )
    history_file.write_text(history_text)
    # Back up the freshly-merged (rich) content — never the possibly-clobbered
    # on-disk file. A per-day snapshot is kept (so history can always be rebuilt
    # by _load_history_sources) plus a one-step history.json.bak rollback.
    try:
        today = datetime.now().astimezone().date().isoformat()
        (output_dir / f"history-{today}.bak.json").write_text(history_text)
        (output_dir / "history.json.bak").write_text(history_text)
    except OSError:
        pass

    # available_months union across sources — used by the toggle to know the
    # full set of months to render in the month-picker.
    available_union = sorted({
        m for s in SOURCES for m in sources_payload[s]["available_months"]
    })

    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "gap_limit_minutes": int(gap_limit.total_seconds() // 60),
        "work_intervals": work_intervals,
        "work_days": work_days,
        "first_day_of_week": first_day_of_week,
        "cache_read_weight": cache_read_weight,
        "default_source": "both",
        "available_months": available_union,
        "sources": sources_payload,
        "icons": load_brand_logos(),
    }

    render_html(TEMPLATE_FILE, output_html, json.dumps(payload, indent=2, ensure_ascii=False))

    print(f"  months covered:  {len(available_union)}")
    print(f"  output:          {output_html}")
    print(f"  history:         {history_file}")

    if "--open" in sys.argv or (auto_open and "--no-open" not in sys.argv):
        webbrowser.open(f"file://{output_html.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
