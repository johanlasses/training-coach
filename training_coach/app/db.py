"""SQLite storage. Single-user, small data: a plain sqlite3 connection is plenty."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY,
    name TEXT,
    sport_type TEXT,
    start_date TEXT,          -- UTC ISO
    start_date_local TEXT,    -- local ISO (naive)
    distance REAL,            -- m
    moving_time INTEGER,      -- s
    elapsed_time INTEGER,
    elevation_gain REAL,
    trainer INTEGER,
    avg_watts REAL,
    weighted_avg_watts REAL,
    max_watts REAL,
    device_watts INTEGER,
    avg_hr REAL,
    max_hr REAL,
    kilojoules REAL,
    avg_speed REAL,
    streams_fetched INTEGER DEFAULT 0,
    np REAL,
    mmp TEXT,                 -- JSON {seconds: watts}
    power_hist TEXT,          -- JSON {bin_start_watts: seconds}, 25 W bins
    decoupling REAL,          -- Pw:HR drift %, steady rides only
    spark_watts TEXT,         -- JSON list, downsampled
    spark_hr TEXT,
    feedback TEXT,            -- JSON {headline, notification, detail, coach, model, at} from the post-ride coach call
    notified INTEGER DEFAULT 0, -- 1 once the post-ride notification went out (or the ride was too old for one)
    ftp_used REAL,
    tss REAL,
    intensity REAL,
    tss_source TEXT,          -- power | hr | estimate (rides), rhr | rpace | estimate (runs), estimate (walks)
    duplicate_of INTEGER      -- another recording of the same session (Zwift + head unit): left out of everything
);
CREATE INDEX IF NOT EXISTS idx_act_start ON activities(start_date_local);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT,
    created_at TEXT,
    model TEXT,
    payload TEXT,             -- JSON plan
    warnings TEXT,            -- JSON list
    coach TEXT                -- coaches.COACHES key; NULL for plans made before coaches existed
);
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    kind TEXT,                -- weekly | manual
    model TEXT,
    markdown TEXT,
    coach TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,                  -- local ISO
    purpose TEXT,             -- plan | analysis | chat
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    role TEXT,                -- user | assistant
    coach TEXT,
    content TEXT,
    proposal TEXT,            -- JSON {plan_id, days: [...]} when the coach proposed plan changes
    warnings TEXT,            -- JSON list, checks the proposal still fails
    notes TEXT,               -- JSON list of note texts saved with this reply
    applied_plan_id INTEGER,
    remembered TEXT,          -- JSON list of memory facts saved with this reply
    kind TEXT,                -- NULL for chat replies, checkin for the coach's daily check-in
    ask TEXT                  -- a follow-up the athlete can send with one tap (the check-in's "Adjust my week")
);
CREATE TABLE IF NOT EXISTS coach_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT,
    kind TEXT,                -- absence | event | injury | other
    start TEXT,               -- YYYY-MM-DD
    end TEXT,                 -- YYYY-MM-DD, last day it matters
    text TEXT,
    removed TEXT              -- local ISO when deleted; kept so later coaches still see it
);
CREATE TABLE IF NOT EXISTS athlete_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,                  -- local ISO
    kind TEXT,                -- coach | profile
    text TEXT,                -- readable, e.g. "Weight: 78 → 76.5 kg"
    data TEXT                 -- JSON {field, old, new} or {from, to} for coach
);
CREATE TABLE IF NOT EXISTS athlete_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT,
    coach TEXT,               -- who learned it
    text TEXT,
    removed TEXT
);
CREATE TABLE IF NOT EXISTS health_daily (
    date TEXT,
    metric TEXT,              -- rhr | hrv | sleep | weight | vo2max
    value REAL,
    unit TEXT,
    PRIMARY KEY (date, metric)
);
"""

ACTIVITY_COLUMNS = [
    "id", "name", "sport_type", "start_date", "start_date_local", "distance", "moving_time",
    "elapsed_time", "elevation_gain", "trainer", "avg_watts", "weighted_avg_watts", "max_watts",
    "device_watts", "avg_hr", "max_hr", "kilojoules", "avg_speed",
]
JSON_COLUMNS = {"mmp", "power_hist", "spark_watts", "spark_hr", "feedback"}


class DB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for table in ("plans", "analyses"):  # added in 0.3.0
            if "coach" not in {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN coach TEXT")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(activities)")}
        for col, decl in (("feedback", "TEXT"), ("notified", "INTEGER DEFAULT 0")):  # added in 0.5.0
            if col not in cols:
                self.conn.execute(f"ALTER TABLE activities ADD COLUMN {col} {decl}")
                if col == "notified":  # rides from before the upgrade were seen already: no pushes for them
                    self.conn.execute("UPDATE activities SET notified=1")
        if "duplicate_of" not in cols:  # added in 0.12.0
            self.conn.execute("ALTER TABLE activities ADD COLUMN duplicate_of INTEGER")
        for table, col in (("coach_notes", "removed"), ("chat_messages", "remembered"),  # added in 0.6.0
                           ("chat_messages", "kind"), ("chat_messages", "ask")):  # added in 0.8.0
            if col not in {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        self.conn.commit()

    # --- kv -------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
        self.conn.commit()

    # --- activities -----------------------------------------------------
    def upsert_activity_summary(self, a: dict) -> bool:
        """Insert or update Strava summary fields. Returns True if the activity is new."""
        exists = self.conn.execute("SELECT 1 FROM activities WHERE id=?", (a["id"],)).fetchone()
        cols = ", ".join(ACTIVITY_COLUMNS)
        marks = ", ".join("?" for _ in ACTIVITY_COLUMNS)
        updates = ", ".join(f"{c}=excluded.{c}" for c in ACTIVITY_COLUMNS if c != "id")
        self.conn.execute(
            f"INSERT INTO activities({cols}) VALUES({marks}) ON CONFLICT(id) DO UPDATE SET {updates}",
            [a.get(c) for c in ACTIVITY_COLUMNS],
        )
        self.conn.commit()
        return not exists

    def update_activity(self, activity_id: int, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        vals = [json.dumps(v) if k in JSON_COLUMNS and v is not None else v for k, v in fields.items()]
        self.conn.execute(f"UPDATE activities SET {sets} WHERE id=?", [*vals, activity_id])
        self.conn.commit()

    def delete_activities_not_in(self, ids: set[int], since_iso: str) -> int:
        """Drop activities deleted on Strava (only within the window we just listed)."""
        rows = self.conn.execute("SELECT id FROM activities WHERE start_date >= ?", (since_iso,)).fetchall()
        gone = [r["id"] for r in rows if r["id"] not in ids]
        self.conn.executemany("DELETE FROM activities WHERE id=?", [(i,) for i in gone])
        self.conn.commit()
        return len(gone)

    def activities(self, since_local: str | None = None, limit: int | None = None, duplicates: bool = False) -> list[dict]:
        """Newest first. A second recording of one session (duplicate_of, see sync.mark_duplicates) is left out, so
        load, lists and views count each session once; `duplicates` includes them (for finding them)."""
        sql = "SELECT * FROM activities WHERE (duplicate_of IS NULL OR ?)"
        args: list[Any] = [duplicates]
        if since_local:
            sql += " AND start_date_local >= ?"
            args.append(since_local)
        sql += " ORDER BY start_date_local DESC"
        if limit:
            sql += " LIMIT ?"
            args.append(limit)
        return [_row(r) for r in self.conn.execute(sql, args).fetchall()]

    def activity(self, activity_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
        return _row(r) if r else None

    def activities_missing_streams(self, limit: int) -> list[dict]:
        """Newest first. Walks never: a stream call (Strava's rate limit) for a dog walk's HR sparkline isn't worth it."""
        rows = self.conn.execute(
            "SELECT * FROM activities WHERE streams_fetched=0 AND sport_type NOT IN ('Walk', 'Hike') AND duplicate_of IS NULL "
            "ORDER BY start_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row(r) for r in rows]

    def activities_to_notify(self, since_utc: str) -> list[dict]:
        """Rides started at or after `since_utc` that haven't had their post-ride notification, oldest first."""
        rows = self.conn.execute(
            "SELECT * FROM activities WHERE notified=0 AND duplicate_of IS NULL AND start_date >= ? ORDER BY start_date",
            (since_utc,)
        ).fetchall()
        return [_row(r) for r in rows]

    # --- plans / analyses -------------------------------------------------
    def save_plan(self, week_start: str, created_at: str, model: str, payload: dict, warnings: list[str],
                  coach: str | None = None) -> int:
        """Stores a plan. The first plan for a week becomes its active plan."""
        cur = self.conn.execute(
            "INSERT INTO plans(week_start, created_at, model, payload, warnings, coach) VALUES(?,?,?,?,?,?)",
            (week_start, created_at, model, json.dumps(payload), json.dumps(warnings), coach),
        )
        self.conn.commit()
        if self.get(f"active_plan:{week_start}") is None:
            self.set_active_plan(week_start, cur.lastrowid)
        return cur.lastrowid

    def latest_plan(self, week_start: str) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM plans WHERE week_start=? ORDER BY id DESC LIMIT 1", (week_start,)
        ).fetchone()
        return _plan(r) if r else None

    def week_plans(self, week_start: str) -> dict[str | None, dict]:
        """The latest plan per coach for a week."""
        rows = self.conn.execute(
            "SELECT * FROM plans WHERE id IN (SELECT MAX(id) FROM plans WHERE week_start=? GROUP BY coach)",
            (week_start,)).fetchall()
        return {r["coach"]: _plan(r) for r in rows}

    def active_plan(self, week_start: str) -> dict | None:
        """The plan that drives today's session, compliance and notifications; else the latest one."""
        plan_id = self.get(f"active_plan:{week_start}")
        return (self.plan(plan_id) if plan_id else None) or self.latest_plan(week_start)

    def set_active_plan(self, week_start: str, plan_id: int) -> None:
        self.set(f"active_plan:{week_start}", plan_id)

    def plan(self, plan_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        return _plan(r) if r else None

    def save_analysis(self, created_at: str, kind: str, model: str, markdown: str, coach: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO analyses(created_at, kind, model, markdown, coach) VALUES(?,?,?,?,?)",
            (created_at, kind, model, markdown, coach),
        )
        self.conn.commit()
        return cur.lastrowid

    def analyses(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, created_at, kind, model, coach FROM analyses ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def analysis(self, analysis_id: int | None = None) -> dict | None:
        if analysis_id is None:
            r = self.conn.execute("SELECT * FROM analyses ORDER BY id DESC LIMIT 1").fetchone()
        else:
            r = self.conn.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
        return dict(r) if r else None

    def analysis_before(self, ts: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM analyses WHERE created_at <= ? ORDER BY created_at DESC, id DESC LIMIT 1",
                              (ts,)).fetchone()
        return dict(r) if r else None

    # --- Claude usage -----------------------------------------------------
    def log_llm(self, ts: str, purpose: str, msg) -> None:
        u = msg.usage
        self.conn.execute(
            "INSERT INTO llm_calls(ts, purpose, model, input_tokens, output_tokens, cache_read_tokens) VALUES(?,?,?,?,?,?)",
            (ts, purpose, msg.model, u.input_tokens, u.output_tokens, getattr(u, "cache_read_input_tokens", 0) or 0))
        self.conn.commit()

    def llm_usage(self, since: str | None = None, cost=None) -> dict[str, dict]:
        """{purpose: {calls, input_tokens, output_tokens, models, cost}} for calls at or after `since`. `cost` is
        llm.cost (passed in to keep the db free of the SDK); without it the cost is 0."""
        rows = self.conn.execute(
            "SELECT purpose, model, COUNT(*) AS calls, SUM(input_tokens) AS input_tokens, SUM(cache_read_tokens) AS "
            "cache_read, SUM(output_tokens) AS output_tokens FROM llm_calls WHERE ts >= ? GROUP BY purpose, model "
            "ORDER BY purpose, model", (since or "",)).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            u = out.setdefault(r["purpose"], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "models": [], "cost": 0.0})
            u["calls"] += r["calls"]
            u["input_tokens"] += (r["input_tokens"] or 0) + (r["cache_read"] or 0)
            u["output_tokens"] += r["output_tokens"] or 0
            u["models"].append(r["model"])
            if cost:
                u["cost"] += cost(r["model"], r["input_tokens"] or 0, r["output_tokens"] or 0, r["cache_read"] or 0)
        return out

    def last_llm_call(self, purpose: str) -> str | None:
        return self.conn.execute("SELECT MAX(ts) FROM llm_calls WHERE purpose=?", (purpose,)).fetchone()[0]

    # --- chat and coach notes ----------------------------------------------
    def add_chat(self, ts: str, role: str, coach: str, content: str, proposal: dict | None = None,
                 warnings: list[str] = (), notes: list[str] = (), remembered: list[str] = (), kind: str | None = None,
                 ask: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO chat_messages(ts, role, coach, content, proposal, warnings, notes, remembered, kind, ask) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, role, coach, content, json.dumps(proposal) if proposal else None, json.dumps(list(warnings)),
             json.dumps(list(notes)), json.dumps(list(remembered)), kind, ask))
        self.conn.commit()
        return cur.lastrowid

    def chat(self, limit: int = 50) -> list[dict]:
        """The last `limit` messages, oldest first."""
        rows = self.conn.execute("SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_chat(r) for r in reversed(rows)]

    def last_chat_id(self, kind: str) -> int | None:
        return self.conn.execute("SELECT MAX(id) FROM chat_messages WHERE kind=?", (kind,)).fetchone()[0]

    def chat_message(self, msg_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM chat_messages WHERE id=?", (msg_id,)).fetchone()
        return _chat(r) if r else None

    def mark_applied(self, msg_id: int, plan_id: int) -> None:
        self.conn.execute("UPDATE chat_messages SET applied_plan_id=? WHERE id=?", (plan_id, msg_id))
        self.conn.commit()

    def clear_chat(self) -> None:
        self.conn.execute("DELETE FROM chat_messages")
        self.conn.commit()

    def add_note(self, created: str, kind: str, start: str, end: str, text: str) -> int:
        cur = self.conn.execute("INSERT INTO coach_notes(created, kind, start, end, text) VALUES(?,?,?,?,?)",
                                (created, kind, start, end, text))
        self.conn.commit()
        return cur.lastrowid

    def delete_note(self, note_id: int, ts: str) -> None:
        """Takes a note out of play but keeps it for the athlete's history."""
        self.conn.execute("UPDATE coach_notes SET removed=? WHERE id=? AND removed IS NULL", (ts, note_id))
        self.conn.commit()

    def active_notes(self, today: str) -> list[dict]:
        """Notes that still matter: their end date is today or later, and nobody removed them."""
        rows = self.conn.execute("SELECT * FROM coach_notes WHERE end >= ? AND removed IS NULL ORDER BY start, id",
                                 (today,)).fetchall()
        return [dict(r) for r in rows]

    def notes_between(self, first: str, last: str) -> list[dict]:
        """Notes whose dates overlap first..last, including ones removed after they started (they still applied)."""
        rows = self.conn.execute("SELECT * FROM coach_notes WHERE start <= ? AND end >= ? AND (removed IS NULL OR "
                                 "substr(removed, 1, 10) >= start) ORDER BY start, id", (last, first)).fetchall()
        return [dict(r) for r in rows]

    def past_notes(self, today: str, since: str, limit: int = 20) -> list[dict]:
        """Notes that ended or were removed since `since`, latest first."""
        rows = self.conn.execute(
            "SELECT * FROM coach_notes WHERE (end < ? AND end >= ?) OR removed >= ? ORDER BY end DESC, id DESC LIMIT ?",
            (today, since, since, limit)).fetchall()
        return [dict(r) for r in rows]

    # --- athlete log and memory ---------------------------------------------
    def log(self, ts: str, kind: str, text: str, data: dict | None = None) -> int:
        cur = self.conn.execute("INSERT INTO athlete_log(ts, kind, text, data) VALUES(?,?,?,?)",
                                (ts, kind, text, json.dumps(data or {})))
        self.conn.commit()
        return cur.lastrowid

    def log_entries(self, since: str = "", limit: int = 40) -> list[dict]:
        """Latest first."""
        rows = self.conn.execute("SELECT * FROM athlete_log WHERE ts >= ? ORDER BY ts DESC, id DESC LIMIT ?",
                                 (since, limit)).fetchall()
        return [_log(r) for r in rows]

    def last_log(self, kind: str, before: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM athlete_log WHERE kind=? AND ts <= ? ORDER BY ts DESC, id DESC LIMIT 1",
                              (kind, before)).fetchone()
        return _log(r) if r else None

    def add_memory(self, created: str, coach: str, text: str) -> int:
        cur = self.conn.execute("INSERT INTO athlete_memory(created, coach, text) VALUES(?,?,?)", (created, coach, text))
        self.conn.commit()
        return cur.lastrowid

    def forget_memory(self, memory_id: int, ts: str) -> None:
        self.conn.execute("UPDATE athlete_memory SET removed=? WHERE id=? AND removed IS NULL", (ts, memory_id))
        self.conn.commit()

    def memories(self) -> list[dict]:
        """Facts still held, oldest first."""
        return [dict(r) for r in self.conn.execute("SELECT * FROM athlete_memory WHERE removed IS NULL ORDER BY id")]

    # --- health -------------------------------------------------------------
    def set_health(self, day: str, metric: str, value: float, unit: str, keep_first: bool = False) -> None:
        self.conn.execute(
            "INSERT INTO health_daily(date, metric, value, unit) VALUES(?,?,?,?) ON CONFLICT(date, metric) DO "
            + ("NOTHING" if keep_first else "UPDATE SET value=excluded.value, unit=excluded.unit"),
            (day, metric, value, unit))
        self.conn.commit()

    def health(self, since: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM health_daily WHERE date >= ? ORDER BY date", (since,)).fetchall()
        return [dict(r) for r in rows]

    def clear_health(self) -> None:
        """On a switch of health source: Garmin's overnight rMSSD and Apple's spot SDNN must never share a baseline."""
        self.conn.execute("DELETE FROM health_daily")
        self.conn.commit()

    def mark_all_notified(self) -> None:
        self.conn.execute("UPDATE activities SET notified=1")
        self.conn.commit()

    def count_activities(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]

    def set_duplicates(self, dupes: dict[int, int]) -> None:
        """Activity id → the recording kept for that session; every other activity is nobody's duplicate."""
        self.conn.execute("UPDATE activities SET duplicate_of=NULL WHERE duplicate_of IS NOT NULL")
        self.conn.executemany("UPDATE activities SET duplicate_of=? WHERE id=?", [(k, d) for d, k in dupes.items()])
        self.conn.commit()

    def count_health_days(self) -> int:
        return self.conn.execute("SELECT COUNT(DISTINCT date) FROM health_daily").fetchone()[0]

    def backup(self, dest: Path) -> None:
        """A consistent copy of this DB (SQLite's online backup), e.g. before the owner resets an athlete's data."""
        target = sqlite3.connect(str(dest))
        try:
            self.conn.backup(target)
        finally:
            target.close()

    def clear_activities(self) -> None:
        """On a switch of activity source: ids from two services could collide, and the same ride would count twice."""
        self.conn.execute("DELETE FROM activities")
        self.conn.commit()


def _chat(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["proposal"] = json.loads(d["proposal"]) if d["proposal"] else None
    d["warnings"] = json.loads(d["warnings"] or "[]")
    d["notes"] = json.loads(d["notes"] or "[]")
    d["remembered"] = json.loads(d["remembered"] or "[]")
    return d


def _log(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["data"] = json.loads(d["data"] or "{}")
    return d


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    for c in JSON_COLUMNS:
        if d.get(c):
            d[c] = json.loads(d[c])
    if d.get("mmp"):
        d["mmp"] = {int(k): v for k, v in d["mmp"].items()}
    if d.get("power_hist"):
        d["power_hist"] = {int(k): v for k, v in d["power_hist"].items()}
    return d


def _plan(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    d["warnings"] = json.loads(d["warnings"] or "[]")
    return d
