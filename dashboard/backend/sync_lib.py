"""
Shared incremental ingestion logic, used by both ingest.py (one-shot CLI,
run after a test finishes) and main.py (called on every API request, so
polling the API during a live run picks up new events/stats as they're
written -- no change to controller.py's hot path required).

Each events_<algo>_<starttime>.jsonl file is tracked by a byte offset in
the `offsets` table, so repeated calls to sync() only read what's new
since the last call. Safe to call as often as you like.
"""
import os
import re
import json
import glob
import time
import sqlite3
import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
EVENTS_DIR = os.path.join(PROJECT_ROOT, 'events')
STATS_DIR = os.path.join(PROJECT_ROOT, 'stats')
DB_PATH = os.path.join(os.path.dirname(__file__), 'dashboard.db')

# A run is considered "live" if its events file was modified more
# recently than this many seconds ago.
LIVE_THRESHOLD_SECONDS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    algorithm TEXT NOT NULL,
    events_file TEXT NOT NULL UNIQUE,
    stats_file TEXT,
    start_ts REAL,
    end_ts REAL,
    event_count INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run_ts ON events(run_id, ts);
CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    dpid INTEGER,
    port INTEGER,
    tx_bytes INTEGER,
    rx_bytes INTEGER,
    tx_rate_Bps REAL,
    rx_rate_Bps REAL
);
CREATE INDEX IF NOT EXISTS idx_stats_run_ts ON stats(run_id, timestamp);
CREATE TABLE IF NOT EXISTS offsets (
    run_id INTEGER PRIMARY KEY,
    events_offset INTEGER DEFAULT 0,
    stats_offset INTEGER DEFAULT 0
);
"""

EVENTS_FNAME_RE = re.compile(r'events_(?P<algo>[a-zA-Z0-9_]+)_(?P<ts>\d+)\.jsonl$')

# SQLite's INTEGER column is a signed 64-bit int. OVS sometimes reports
# an unsupported port counter as the uint64 sentinel 0xFFFFFFFFFFFFFFFF
# (18446744073709551615) rather than a real byte count -- that's larger
# than SQLite can store, so it's clamped to 0 ("counter unavailable")
# instead of crashing the sync.
SQLITE_INT_MAX = 2**63 - 1


def _safe_int(s):
    v = int(s)
    if v > SQLITE_INT_MAX or v < -SQLITE_INT_MAX - 1:
        return 0
    return v


STATS_FNAME_RE = re.compile(
    r'port_stats_(?P<algo>[a-zA-Z0-9_]+)_(?P<date>\d{8})_(?P<time>\d{6})\.csv$')


def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _stats_start_ts(fname):
    m = STATS_FNAME_RE.search(fname)
    if not m:
        return None
    dt = datetime.datetime.strptime(m.group('date') + m.group('time'), '%Y%m%d%H%M%S')
    return dt.timestamp()


def _find_best_stats_match(algo, start_ts, stats_files):
    best, best_diff = None, None
    for f in stats_files:
        base = os.path.basename(f)
        m = STATS_FNAME_RE.search(base)
        if not m or m.group('algo') != algo:
            continue
        csv_ts = _stats_start_ts(base)
        if csv_ts is None:
            continue
        diff = abs(csv_ts - start_ts)
        if best_diff is None or diff < best_diff:
            best, best_diff = f, diff
    if best is not None and best_diff is not None and best_diff <= 600:
        return best
    return None


def _get_or_create_run(conn, events_path):
    base = os.path.basename(events_path)
    m = EVENTS_FNAME_RE.search(base)
    if not m:
        return None
    algo, start_ts_hint = m.group('algo'), int(m.group('ts'))
    row = conn.execute("SELECT id FROM runs WHERE events_file = ?", (base,)).fetchone()
    if row:
        return row['id']
    cur = conn.execute(
        "INSERT INTO runs (algorithm, events_file, start_ts, end_ts, event_count) "
        "VALUES (?, ?, ?, ?, 0)",
        (algo, base, start_ts_hint, start_ts_hint))
    conn.execute(
        "INSERT INTO offsets (run_id, events_offset, stats_offset) VALUES (?, 0, 0)",
        (cur.lastrowid,))
    return cur.lastrowid


def _sync_events_file(conn, run_id, events_path):
    off_row = conn.execute(
        "SELECT events_offset FROM offsets WHERE run_id = ?", (run_id,)).fetchone()
    offset = off_row['events_offset'] if off_row else 0
    with open(events_path, 'rb') as fh:
        fh.seek(offset)
        chunk = fh.read()
    last_nl = chunk.rfind(b'\n')
    if last_nl == -1:
        return  # no complete new line since last sync
    complete = chunk[:last_nl + 1]
    new_offset = offset + len(complete)

    new_rows = []
    for line in complete.decode('utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            new_rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if new_rows:
        conn.executemany(
            "INSERT INTO events (run_id, ts, type, data) VALUES (?, ?, ?, ?)",
            [(run_id, r.get('ts', time.time()), r.get('type', 'unknown'), json.dumps(r))
             for r in new_rows])
        ts_values = [r['ts'] for r in new_rows if 'ts' in r]
        if ts_values:
            conn.execute(
                "UPDATE runs SET end_ts = MAX(COALESCE(end_ts, 0), ?) WHERE id = ?",
                (max(ts_values), run_id))
            row = conn.execute("SELECT start_ts FROM runs WHERE id = ?", (run_id,)).fetchone()
            current_start = row['start_ts']
            new_start = min(ts_values + ([current_start] if current_start else []))
            conn.execute("UPDATE runs SET start_ts = ? WHERE id = ?", (new_start, run_id))
        conn.execute(
            "UPDATE runs SET event_count = event_count + ? WHERE id = ?",
            (len(new_rows), run_id))

    conn.execute(
        "UPDATE offsets SET events_offset = ? WHERE run_id = ?", (new_offset, run_id))


def _sync_stats_file(conn, run_id, stats_path):
    off_row = conn.execute(
        "SELECT stats_offset FROM offsets WHERE run_id = ?", (run_id,)).fetchone()
    offset = off_row['stats_offset'] if off_row else 0
    with open(stats_path, 'rb') as fh:
        fh.seek(offset)
        chunk = fh.read()
    last_nl = chunk.rfind(b'\n')
    if last_nl == -1:
        return
    complete = chunk[:last_nl + 1]
    new_offset = offset + len(complete)

    lines = complete.decode('utf-8', errors='replace').splitlines()
    if offset == 0 and lines and lines[0].startswith('timestamp,'):
        lines = lines[1:]  # skip header only on this file's first read

    rows = []
    for line in lines:
        parts = line.split(',')
        if len(parts) != 7:
            continue
        try:
            rows.append((
                run_id, float(parts[0]), int(parts[1]), int(parts[2]),
                _safe_int(parts[3]), _safe_int(parts[4]), float(parts[5]), float(parts[6]),
            ))
        except ValueError:
            continue

    if rows:
        conn.executemany(
            "INSERT INTO stats (run_id, timestamp, dpid, port, tx_bytes, rx_bytes, "
            "tx_rate_Bps, rx_rate_Bps) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)

    conn.execute(
        "UPDATE offsets SET stats_offset = ? WHERE run_id = ?", (new_offset, run_id))


def sync(conn):
    """Incrementally pulls in any new lines from every events_*.jsonl and
    links/tails the best-matching port_stats CSV for each run. Call this
    as often as you like -- a no-op if nothing's changed on disk."""
    events_files = sorted(glob.glob(os.path.join(EVENTS_DIR, 'events_*.jsonl')))
    stats_files = sorted(glob.glob(os.path.join(STATS_DIR, 'port_stats_*.csv')))
    touched = []

    for ef in events_files:
        run_id = _get_or_create_run(conn, ef)
        if run_id is None:
            continue
        _sync_events_file(conn, run_id, ef)

        row = conn.execute(
            "SELECT algorithm, stats_file, start_ts FROM runs WHERE id = ?",
            (run_id,)).fetchone()
        stats_path = None
        if row['stats_file']:
            candidate = os.path.join(STATS_DIR, row['stats_file'])
            if os.path.exists(candidate):
                stats_path = candidate
        else:
            match = _find_best_stats_match(row['algorithm'], row['start_ts'] or 0, stats_files)
            if match:
                conn.execute(
                    "UPDATE runs SET stats_file = ? WHERE id = ?",
                    (os.path.basename(match), run_id))
                stats_path = match
        if stats_path:
            _sync_stats_file(conn, run_id, stats_path)

        touched.append(run_id)

    conn.commit()
    return touched


def is_live_file(events_path):
    try:
        return (time.time() - os.path.getmtime(events_path)) < LIVE_THRESHOLD_SECONDS
    except OSError:
        return False


def get_live_run_id(conn):
    """Returns the run_id of the most recently-written events file still
    within the live-activity window, or None if nothing's currently live."""
    events_files = glob.glob(os.path.join(EVENTS_DIR, 'events_*.jsonl'))
    live_files = [f for f in events_files if is_live_file(f)]
    if not live_files:
        return None
    newest = max(live_files, key=os.path.getmtime)
    row = conn.execute(
        "SELECT id FROM runs WHERE events_file = ?",
        (os.path.basename(newest),)).fetchone()
    return row['id'] if row else None
