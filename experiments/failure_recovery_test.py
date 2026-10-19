"""
Failure/recovery experiment (Step 5): single link failure followed by
an overlapping second failure that fully isolates the source switch,
then recovery once both links are restored. Requires the failure-
handling support added to topology.py/controller.py (live adjacency
updates via set_link_down()/set_link_up(), proactive flow eviction on
link-down via EventOFPPortStatus) -- without that, "recovery" would
just measure the fixed idle_timeout=10 constant, not real routing
behavior.

Test pair: h1 (10.0.0.1, switch e1) <-> h5 (10.0.0.5, switch e3) --
cross-pod, 8 candidate shortest paths in the healthy topology, via
aggregation switches a1/a2 and core switches c1/c2. This gives room for
a single failure to be gracefully routed around (paths via a2 still
work) before the second, overlapping failure removes ALL of e1's
uplinks and genuinely isolates it -- a real full outage, reported
honestly rather than treated as a script bug.

Phases (wall-clock):
  0. Baseline            -- healthy network, establish traffic
  1. Single failure      -- e1<->a1 down. Paths via a2 still work.
  2. Overlapping failure -- e1<->a2 ALSO down. e1 now has zero working
                             uplinks -- total isolation.
  3. Recovery            -- both links restored, confirm connectivity.

Measures (README's "recovery time after failure" KPI):
  - Ping-based recovery time: gap between failure-injection timestamp
    and the first successful ping reply afterward.
  - iperf-based throughput impact: full per-second trace across all
    phases, so the dip (phase 1) and full stall (phase 2) are visible.

Run with: sudo python3 experiments/failure_recovery_test.py
"""

import sys
import os
import re
import time

sys.path.insert(0, '.')

from mininet.net import Mininet
from mininet.node import RemoteController
from mininet.link import TCLink
from mininet.log import setLogLevel

from fattree_topo import FatTreeTopo

RESULTS_DIR = 'results'
PING_LOG = '/tmp/failure_test_ping.log'
IPERF_LOG = '/tmp/failure_test_iperf.log'

BASELINE_SECONDS = 8
PHASE1_SECONDS = 15
PHASE2_SECONDS = 15
RECOVERY_WAIT_SECONDS = 30
POST_CHECK_SECONDS = 5
TOTAL_SECONDS = (BASELINE_SECONDS + PHASE1_SECONDS + PHASE2_SECONDS +
                 RECOVERY_WAIT_SECONDS + POST_CHECK_SECONDS + 5)


def parse_ping_log(path):
    """Sorted epoch timestamps for each SUCCESSFUL ping reply, parsed
    from `ping -D` lines like:
    [1730000123.456789] 64 bytes from 10.0.0.5: icmp_seq=42 ttl=64 time=0.123 ms
    """
    timestamps = []
    if not os.path.exists(path):
        return timestamps
    pattern = re.compile(r'^\[(\d+\.\d+)\]\s+64 bytes from')
    with open(path) as f:
        for line in f:
            m = pattern.match(line)
            if m:
                timestamps.append(float(m.group(1)))
    return timestamps


def recovery_time_after(timestamps, injected_at):
    after = [t for t in timestamps if t > injected_at]
    if not after:
        return None
    return after[0] - injected_at


def print_iperf_trace(path):
    print("\n  --- iperf per-second trace (h1 -> h5) ---")
    if not os.path.exists(path):
        print("  (no iperf log found)")
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if 'bits/sec' in line:
                print(f"  {line}")


def main():
    setLogLevel('info')
    os.makedirs(RESULTS_DIR, exist_ok=True)

    topo = FatTreeTopo()
    net = Mininet(topo=topo, link=TCLink,
                   controller=lambda name: RemoteController(
                       name, ip='127.0.0.1', port=6633))
    net.start()

    print("\n=== Waiting for controller to install initial table-miss rules ===")
    time.sleep(2)

    h1, h5 = net.get('h1', 'h5')

    print("\n=== Resolving ARP ===")
    h1.cmd('ping -c1 10.0.0.5')
    time.sleep(1)

    print("\n=== Starting persistent ping h1 -> h5 (0.2s interval, timestamped) ===")
    h1.cmd(f'rm -f {PING_LOG}')
    h1.cmd(f'ping -D -i 0.2 10.0.0.5 > {PING_LOG} 2>&1 &')

    print(f"=== Starting iperf h1 -> h5 for the full test duration ({TOTAL_SECONDS}s) ===")
    h5.cmd('iperf -s &')
    time.sleep(1)
    h1.cmd(f'rm -f {IPERF_LOG}')
    h1.cmd(f'iperf -c 10.0.0.5 -t {TOTAL_SECONDS} -i 1 -b 8M > {IPERF_LOG} 2>&1 &')

    print(f"\n=== Phase 0: baseline, {BASELINE_SECONDS}s ===")
    time.sleep(BASELINE_SECONDS)

    print("\n=== Phase 1: SINGLE FAILURE -- bringing down link e1 <-> a1 ===")
    phase1_injected_at = time.time()
    net.configLinkStatus('e1', 'a1', 'down')
    print(f"  Injected at {phase1_injected_at:.3f}. Waiting {PHASE1_SECONDS}s "
          f"to observe rerouting (paths via a2 should still work)...")
    time.sleep(PHASE1_SECONDS)

    print("\n=== Phase 2: OVERLAPPING FAILURE -- also bringing down link e1 <-> a2 ===")
    print("  (e1 now has ZERO working uplinks -- genuine full isolation,")
    print("   not a bug if traffic stops completely.)")
    phase2_injected_at = time.time()
    net.configLinkStatus('e1', 'a2', 'down')
    print(f"  Injected at {phase2_injected_at:.3f}. Waiting {PHASE2_SECONDS}s...")
    time.sleep(PHASE2_SECONDS)

    print("\n=== Phase 3: RECOVERY -- restoring both links ===")
    recovery_injected_at = time.time()
    net.configLinkStatus('e1', 'a1', 'up')
    net.configLinkStatus('e1', 'a2', 'up')
    print(f"  Both links restored at {recovery_injected_at:.3f}. "
          f"Waiting {RECOVERY_WAIT_SECONDS}s for reconvergence...")
    time.sleep(RECOVERY_WAIT_SECONDS)

    print(f"\n=== Post-check: confirming connectivity for {POST_CHECK_SECONDS}s ===")
    time.sleep(POST_CHECK_SECONDS)

    print("\n=== Stopping background traffic ===")
    h1.cmd('kill %ping')
    time.sleep(1)
    h1.cmd('kill %iperf')
    h5.cmd('kill %iperf')
    time.sleep(1)

    ping_timestamps = parse_ping_log(PING_LOG)
    print(f"\n=== Results ({len(ping_timestamps)} successful pings recorded) ===")

    rt1 = recovery_time_after(ping_timestamps, phase1_injected_at)
    print(f"\nPhase 1 (single failure, e1<->a1 down):")
    if rt1 is not None:
        print(f"  Recovery time: {rt1:.3f}s (first successful ping after injection)")
    else:
        print("  No successful ping recorded after injection -- unexpected for a "
              "single failure with alternate paths available; check that the "
              "failure-handling controller changes are active.")

    during_phase2 = [t for t in ping_timestamps
                      if phase2_injected_at <= t < recovery_injected_at]
    print(f"\nPhase 2 (overlapping failure, e1 fully isolated):")
    print(f"  Successful pings during this phase: {len(during_phase2)} "
          f"(expected: 0 -- no path can exist while e1 has zero uplinks)")

    rt3 = recovery_time_after(ping_timestamps, recovery_injected_at)
    print(f"\nPhase 3 (recovery, both links restored):")
    if rt3 is not None:
        print(f"  Recovery time: {rt3:.3f}s (first successful ping after both links restored)")
    else:
        print("  No successful ping recorded after restoration -- investigate: "
              "this would mean the network did not recover.")

    print_iperf_trace(IPERF_LOG)

    print("\n=== Cleaning up ===")
    net.stop()

    print(f"\nRaw logs kept at {PING_LOG} and {IPERF_LOG} for further inspection.")


if __name__ == '__main__':
    main()
