"""
Local API server for the SDN dashboard -- replay, live, and lab control.

Run:
    uvicorn main:app --reload --port 8000

Every request first calls sync_lib.sync(), which incrementally tails
events/*.jsonl and stats/*.csv for anything new. That means polling
this API while a test is running (GET /api/live/current, then
GET /api/runs/{id}/events?since=...) surfaces new events within one
poll interval -- no separate "live" code path, no changes needed to
controller.py beyond what it already writes.

The /api/lab/* endpoints are thin proxies to the privileged lab daemon
(dashboard/lab/lab_daemon.py), which must be running separately via
'sudo python3 dashboard/lab/lab_daemon.py' for those to work.
"""
import json
import socket
import sync_lib
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="SDN Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LAB_HOST = "127.0.0.1"
LAB_PORT = 9191


def lab_call(cmd_dict, timeout=10):
    """Sends one JSON-line command to the lab daemon and returns its
    response. Raises HTTPException(503) if the daemon isn't reachable --
    distinct from the daemon returning a normal {'error': ...} response,
    which is passed through as-is for the frontend to display."""
    try:
        s = socket.create_connection((LAB_HOST, LAB_PORT), timeout=timeout)
        s.sendall((json.dumps(cmd_dict) + "\n").encode())
        s.settimeout(timeout)
        chunks = []
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        s.close()
        return json.loads(b"".join(chunks).decode())
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        raise HTTPException(
            status_code=503,
            detail=f"Lab daemon not reachable at {LAB_HOST}:{LAB_PORT} -- "
                   f"start it with 'sudo python3 dashboard/lab/lab_daemon.py' "
                   f"from the project root. ({e})")


class AlgorithmRequest(BaseModel):
    algorithm: str


class CompareRequest(BaseModel):
    algorithms: list
    flows: int = 6


class TrafficRequest(BaseModel):
    src: str
    dst: str
    protocol: str
    duration: int = 10
    bandwidth: str = None


@app.get("/api/lab/status")
def lab_status():
    return lab_call({"cmd": "status"})


@app.post("/api/lab/start")
def lab_start():
    return lab_call({"cmd": "start_topology"}, timeout=60)


@app.post("/api/lab/stop")
def lab_stop():
    return lab_call({"cmd": "stop_topology"}, timeout=30)


@app.post("/api/lab/algorithm")
def lab_set_algorithm(req: AlgorithmRequest):
    return lab_call({"cmd": "set_algorithm", "algorithm": req.algorithm}, timeout=20)


@app.post("/api/lab/traffic")
def lab_run_traffic(req: TrafficRequest):
    return lab_call({"cmd": "run_traffic", "src": req.src, "dst": req.dst,
                      "protocol": req.protocol, "duration": req.duration,
                      "bandwidth": req.bandwidth})


@app.post("/api/lab/compare")
def lab_compare_start(req: CompareRequest):
    return lab_call({"cmd": "compare_start", "algorithms": req.algorithms,
                      "flows": req.flows}, timeout=30)


@app.get("/api/lab/compare")
def lab_compare_status():
    return lab_call({"cmd": "compare_status"})


@app.delete("/api/lab/traffic/{traffic_id}")
def lab_stop_traffic(traffic_id: str):
    return lab_call({"cmd": "stop_traffic", "traffic_id": traffic_id})


@app.get("/api/lab/traffic")
def lab_list_traffic():
    return lab_call({"cmd": "list_traffic"})


def get_conn():
    conn = sync_lib._conn()
    sync_lib.sync(conn)
    return conn


@app.get("/api/runs")
def list_runs():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, algorithm, events_file, stats_file, start_ts, end_ts, event_count "
        "FROM runs ORDER BY start_ts DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/live/current")
def live_current():
    """Which run_id (if any) is currently being actively written to.
    Poll this to detect a new test starting, or the current one ending."""
    conn = get_conn()
    run_id = sync_lib.get_live_run_id(conn)
    conn.close()
    return {"run_id": run_id, "is_live": run_id is not None}


@app.get("/api/runs/{run_id}/events")
def get_events(run_id: int, since: float = Query(default=None, description="only events with ts > since")):
    conn = get_conn()
    run = conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        conn.close()
        raise HTTPException(status_code=404, detail="run not found")
    if since is not None:
        rows = conn.execute(
            "SELECT ts, type, data FROM events WHERE run_id = ? AND ts > ? ORDER BY ts ASC",
            (run_id, since)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts, type, data FROM events WHERE run_id = ? ORDER BY ts ASC",
            (run_id,)
        ).fetchall()
    conn.close()
    return [{"ts": r["ts"], "type": r["type"], **json.loads(r["data"])} for r in rows]


@app.get("/api/runs/{run_id}/stats")
def get_stats(run_id: int, since: float = Query(default=None, description="only rows with timestamp > since")):
    conn = get_conn()
    run = conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        conn.close()
        raise HTTPException(status_code=404, detail="run not found")
    if since is not None:
        rows = conn.execute(
            "SELECT timestamp, dpid, port, tx_bytes, rx_bytes, tx_rate_Bps, rx_rate_Bps "
            "FROM stats WHERE run_id = ? AND timestamp > ? ORDER BY timestamp ASC",
            (run_id, since)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT timestamp, dpid, port, tx_bytes, rx_bytes, tx_rate_Bps, rx_rate_Bps "
            "FROM stats WHERE run_id = ? ORDER BY timestamp ASC",
            (run_id,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/")
def root():
    return {"status": "ok", "message": "SDN Dashboard API. See /api/runs or /api/live/current."}
