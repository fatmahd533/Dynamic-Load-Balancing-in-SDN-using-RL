"""
One-shot CLI sync: pulls any new data from events/*.jsonl and stats/*.csv
into dashboard.db. Uses the exact same incremental logic the live API
uses when polled during a running test -- so running this after a test,
or just leaving the dashboard open with live mode on, both converge on
the same data.

Safe to re-run anytime; only reads what's new since the last sync.

    python3 ingest.py
"""
import sync_lib


def main():
    conn = sync_lib._conn()
    touched = sync_lib.sync(conn)
    rows = conn.execute(
        "SELECT id, algorithm, event_count, stats_file FROM runs ORDER BY id"
    ).fetchall()
    for r in rows:
        print(f"  run {r['id']}: {r['algorithm']}  events={r['event_count']}  "
              f"stats={r['stats_file'] or 'none'}")
    conn.close()
    print(f"\nSynced {len(touched)} run file(s) into {sync_lib.DB_PATH}")


if __name__ == '__main__':
    main()
