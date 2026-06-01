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
    parts = cwd.rstrip("/").split("/")
    return parts[-1] if parts else cwd


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
                    if obj.get("type") == "ai-title":
                        t = obj.get("aiTitle")
                        if isinstance(t, str) and t.strip():
                            file_title = t.strip()
                    elif obj.get("type") == "last-prompt":
                        lp = obj.get("lastPrompt")
                        if isinstance(lp, str) and lp.strip():
                            file_last = lp.strip()

                if file_sid:
                    title = file_title or file_last or ""
                    if len(title) > 80:
                        title = title[:77] + "…"
                    session_meta[file_sid] = {
                        "project": file_proj or "unknown",
                        "title": title,
                        "source": "claude",
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


def _codex_tokens_from_event(payload):
    """Codex `event_msg.payload.type=token_count` carries per-turn usage in
    `last_token_usage`. Map onto the same shape as Claude's extract_tokens()."""
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
            elif t == "event_msg" and payload.get("type") == "user_message" and not file_title:
                msg = payload.get("message")
                if isinstance(msg, str) and msg.strip():
                    file_title = msg.strip().splitlines()[0]
                    if len(file_title) > 80:
                        file_title = file_title[:77] + "…"

        if file_sid:
            session_meta[file_sid] = {
                "project": file_proj,
                "title": file_title,
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
            tokens = _codex_tokens_from_event(payload) if ptype == "token_count" else None
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

    for i in range(1, len(events)):
        ts_prev = events[i - 1][0]
        ts_cur, _, sid, *_ = events[i]
        if ts_cur - ts_prev <= gap_limit:
            distribute_interval(ts_prev, ts_cur, sid, hour_b, session_b)

    for ts, tok, *_ in events:
        if not tok:
            continue
        k = (ts.year, ts.month, ts.day)
        for f in ("input", "output", "cache_read", "cache_create"):
            daily_tokens[k][f] += tok[f]
        daily_tokens[k]["all"] += (
            tok["input"] + tok["output"] + tok["cache_create"]
            + int(tok["cache_read"] * cache_read_weight)
        )
    return hour_b, session_b, daily_tokens


def shape_output(hour_b, session_b, daily_tokens, session_meta):
    months = defaultdict(lambda: defaultdict(lambda: {
        "hours": {}, "sessions": {}, "total": 0, "tokens": None,
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
            months[f"{y:04d}-{m:02d}"][f"{d:02d}"]["sessions"][str(h)] = items[:6]

    for (y, m, d), tk in daily_tokens.items():
        months[f"{y:04d}-{m:02d}"][f"{d:02d}"]["tokens"] = tk

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
        )[:6]
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


def _load_history_sources(history_file):
    """Return {source: months_dict}. Migrates legacy single-source format
    and strips alien-source session items from claude/codex buckets."""
    out = {s: {} for s in SOURCES}
    if not history_file.exists():
        return out
    try:
        raw = json.loads(history_file.read_text())
    except (json.JSONDecodeError, OSError):
        return out
    if isinstance(raw.get("sources"), dict):
        for s in SOURCES:
            entry = raw["sources"].get(s) or {}
            months = entry.get("months") or {}
            if s in ("claude", "codex"):
                months = _strip_alien_session_sources(months, s)
            out[s] = months
    elif isinstance(raw.get("months"), dict):
        out["claude"] = _strip_alien_session_sources(raw["months"], "claude")
    return out


def _fmt_hours(secs):
    return f"{int(secs // 3600)} h {int((secs % 3600) // 60)} min"


def main():
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
    sources_payload = {}
    sources_history = {}

    for src in SOURCES:
        ev = events_by_source[src]
        hour_b, sess_b, day_tok = build_buckets(ev, gap_limit, cache_read_weight)
        current_months = shape_output(hour_b, sess_b, day_tok, session_meta)
        merged_months = merge_months(current_months, history_by_source.get(src, {}))
        available = sorted(merged_months.keys())
        total_sec_run = sum(hour_b.values())
        total_sec_merged = sum(m["total"] for m in merged_months.values())
        total_tokens = sum(m.get("tokens_total", 0) for m in merged_months.values())
        sources_payload[src] = {
            "available_months": available,
            "months": merged_months,
            "total_seconds": total_sec_merged,
            "total_tokens": total_tokens,
            "events_count": len(ev),
        }
        sources_history[src] = {"months": merged_months}
        print(f"  [{src:>6}] this run: {_fmt_hours(total_sec_run)} · "
              f"merged: {_fmt_hours(total_sec_merged)} · "
              f"tokens: {total_tokens:,}")

    # Persist history with new schema (drop legacy top-level "months" key).
    history_file.parent.mkdir(parents=True, exist_ok=True)
    # Rotate a one-step backup first, so a bad or partial write can always be
    # rolled back from history.json.bak (guards against accidental data loss).
    if history_file.exists():
        try:
            (output_dir / "history.json.bak").write_bytes(history_file.read_bytes())
        except OSError:
            pass
    history_file.write_text(json.dumps(
        {"updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
         "sources": sources_history},
        ensure_ascii=False, indent=2,
    ))

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
