#!/usr/bin/env python3
"""db.py — SQLite persistence for Wi-Fi Radar (RSSI history, events, occupancy)."""

import csv
import io
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path("~/.wifi_radar.db").expanduser()
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(str(DB_PATH), check_same_thread=False)


def init_db() -> None:
    with _lock:
        con = _conn()
        # WAL mode allows concurrent reads during writes — important since the
        # dashboard reads while the radar thread writes at 1 Hz.
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS rssi_history (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       REAL    NOT NULL,
                rssi     REAL    NOT NULL,
                smoothed REAL
            );
            CREATE INDEX IF NOT EXISTS rssi_history_ts ON rssi_history(ts);

            CREATE TABLE IF NOT EXISTS events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         REAL NOT NULL,
                event_type TEXT NOT NULL,
                z_score    REAL,
                details    TEXT
            );
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);

            CREATE TABLE IF NOT EXISTS occupancy (
                date    TEXT NOT NULL PRIMARY KEY,
                seconds REAL NOT NULL DEFAULT 0
            );
        """)
        con.commit()
        con.close()


def record_rssi(ts: float, rssi: float, smoothed: float = None) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO rssi_history (ts, rssi, smoothed) VALUES (?, ?, ?)",
            (ts, rssi, smoothed),
        )
        con.commit()
        con.close()


def record_event(ts: float, event_type: str, z_score: float = None, details: str = None) -> None:
    with _lock:
        con = _conn()
        con.execute(
            "INSERT INTO events (ts, event_type, z_score, details) VALUES (?, ?, ?, ?)",
            (ts, event_type, z_score, details),
        )
        con.commit()
        con.close()


def record_occupancy(seconds: float) -> None:
    date = time.strftime("%Y-%m-%d")
    with _lock:
        con = _conn()
        con.execute(
            """INSERT INTO occupancy (date, seconds) VALUES (?, ?)
               ON CONFLICT(date) DO UPDATE SET seconds = seconds + excluded.seconds""",
            (date, seconds),
        )
        con.commit()
        con.close()


def get_history(since: float = None, until: float = None, limit: int = 5000) -> list:
    since = since if since is not None else (time.time() - 3600)
    until = until if until is not None else time.time()
    with _lock:
        con = _conn()
        rows = con.execute(
            "SELECT ts, rssi, smoothed FROM rssi_history WHERE ts >= ? AND ts <= ? ORDER BY ts LIMIT ?",
            (since, until, limit),
        ).fetchall()
        con.close()
    return [{"ts": r[0], "rssi": r[1], "smoothed": r[2]} for r in rows]


def get_events(since: float = None, until: float = None, limit: int = 1000) -> list:
    since = since if since is not None else (time.time() - 3600)
    until = until if until is not None else time.time()
    with _lock:
        con = _conn()
        rows = con.execute(
            "SELECT ts, event_type, z_score, details FROM events WHERE ts >= ? AND ts <= ? ORDER BY ts LIMIT ?",
            (since, until, limit),
        ).fetchall()
        con.close()
    return [{"ts": r[0], "type": r[1], "z_score": r[2], "details": r[3]} for r in rows]


def get_occupancy(days: int = 30) -> list:
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    with _lock:
        con = _conn()
        rows = con.execute(
            "SELECT date, seconds FROM occupancy WHERE date >= ? ORDER BY date",
            (cutoff,),
        ).fetchall()
        con.close()
    return [{"date": r[0], "hours": round(r[1] / 3600, 2)} for r in rows]


def export_csv(since: float = None, until: float = None) -> str:
    since = since if since is not None else (time.time() - 86400)
    until = until if until is not None else time.time()
    history = get_history(since=since, until=until, limit=500_000)
    events_map: dict[float, dict] = {}
    for e in get_events(since=since, until=until, limit=50_000):
        events_map[e["ts"]] = e

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["timestamp", "datetime", "rssi_dbm", "smoothed_rssi", "event_type", "z_score"])
    for h in history:
        ts = h["ts"]
        dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        evt = events_map.get(ts, {})
        writer.writerow([
            f"{ts:.3f}", dt,
            h["rssi"],
            f"{h['smoothed']:.2f}" if h["smoothed"] is not None else "",
            evt.get("type", ""),
            f"{evt['z_score']:.2f}" if evt.get("z_score") else "",
        ])
    return out.getvalue()


def prune_old_data(days: int = 7) -> None:
    cutoff = time.time() - days * 86400
    with _lock:
        con = _conn()
        con.execute("DELETE FROM rssi_history WHERE ts < ?", (cutoff,))
        con.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        con.commit()
        con.close()
