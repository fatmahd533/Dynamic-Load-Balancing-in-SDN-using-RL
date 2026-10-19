#!/usr/bin/env bash
# One-command reproduction of the project's experiments.
# Run from the project root as a normal user (it calls sudo itself):
#     bash reproduce.sh quick     # ~10 min  - checks everything works, 2 repetitions
#     bash reproduce.sh full      # ~6-7 h   - all experiments with the report's repetitions
# Needs: Ubuntu, Mininet + Open vSwitch (system python3), and the ryu-env
# virtualenv (see SETUP.md). Nothing else may be running (no controller, no dashboard).
set -euo pipefail
MODE="${1:-}"
[[ "$MODE" == quick || "$MODE" == full ]] || { echo "usage: bash reproduce.sh quick|full"; exit 1; }
cd "$(dirname "$0")"

fail() { echo "CHECK FAILED: $1"; exit 1; }
[[ -x ryu-env/bin/ryu-manager ]] || fail "ryu-env/bin/ryu-manager not found (see SETUP.md)"
command -v mn >/dev/null           || fail "Mininet ('mn') not installed"
command -v ovs-vsctl >/dev/null    || fail "Open vSwitch not installed"
command -v iperf >/dev/null        || fail "iperf (version 2) not installed"
python3 -c "import mininet" 2>/dev/null || fail "system python3 cannot import mininet"
for f in controller.py experiments/gate_matrix.py experiments/gate_bg.py \
         experiments/repeat_extra2.py experiments/gate_mixed.py experiments/summarize_gate.py \
         algorithms/lcp_pending.py algorithms/pending.py; do
  [[ -f $f ]] || fail "missing file $f"
done
grep -q SDN_PATH_CACHE_TTL controller.py || fail "controller.py lacks the once-per-flow patch (python3 experiments/patch_dedupe.py)"

# the anomaly detector must be off for routing experiments
if [[ -f models/anomaly_model.joblib ]]; then
  mv models/anomaly_model.joblib models/anomaly_model.joblib.off
  echo "Anomaly model moved to models/anomaly_model.joblib.off (restore it for the anomaly demo)."
fi
mkdir -p results models
LOG=results/reproduce_${MODE}_$(date +%Y%m%d_%H%M).log
echo "Logging to $LOG"
run() { echo; echo ">>> $*" | tee -a "$LOG"; "$@" 2>&1 | tee -a "$LOG"; }

if [[ $MODE == quick ]]; then
  run sudo python3 experiments/gate_matrix.py --reps 2 --flows 6 --stagger 0 --tag rq \
      --algorithms shortest_path random_path least_congested lcp_pending
  run python3 experiments/summarize_gate.py --file results/gate_runs_rq.csv --ref random_path
  run sudo python3 experiments/repeat_extra2.py failure --reps 1
  run sudo python3 experiments/repeat_extra2.py qos --reps 1
  run python3 experiments/repeat_extra2.py summary
  echo; echo "QUICK CHECK DONE. Expect lcp_pending to deliver clearly more than shortest_path and random_path."
  exit 0
fi

# ---- full ----
[[ -f models/ql_per_link.json ]] || run sudo python3 experiments/train_ql.py --state per_link
[[ -f models/ql_pending.json  ]] || run sudo python3 experiments/train_ql.py --state pending --flows 6 --staggers 0 0 1
ALGS="shortest_path global_rr random_path least_congested lcp_pending ql_frozen_pending ql_frozen_per_link"
run sudo python3 experiments/gate_matrix.py --reps 15 --flows 6 --stagger 0 --tag pend --algorithms $ALGS
run python3 experiments/summarize_gate.py --file results/gate_runs_pend.csv --ref random_path
run sudo python3 experiments/gate_bg.py --reps 15 --tag bgfix --algorithms $ALGS ql_frozen_busiest
run python3 experiments/summarize_gate.py --file results/gate_runs_bgfix.csv --ref random_path
run sudo python3 experiments/gate_matrix.py --reps 15 --flows 8 --stagger 0 --tag wide8 \
    --algorithms shortest_path random_path least_congested lcp_pending ql_frozen_per_link
run python3 experiments/summarize_gate.py --file results/gate_runs_wide8.csv --ref random_path
run sudo python3 experiments/gate_matrix.py --reps 15 --flows 6 --stagger 1 --tag wide_s1 \
    --algorithms shortest_path random_path least_congested lcp_pending ql_frozen_per_link
run python3 experiments/summarize_gate.py --file results/gate_runs_wide_s1.csv --ref random_path
run sudo python3 experiments/gate_matrix.py --reps 15 --flows 8 --stagger 0 --tag wide8c \
    --algorithms random_path lcp_pending ql_frozen_pending
run python3 experiments/summarize_gate.py --file results/gate_runs_wide8c.csv --ref random_path
run sudo python3 experiments/gate_mixed.py --reps 15
run python3 experiments/gate_mixed.py summary
rm -f results/repeat_failure.csv results/repeat_qos.csv
run sudo python3 experiments/repeat_extra2.py failure --reps 10
run sudo python3 experiments/repeat_extra2.py qos --reps 10
run python3 experiments/repeat_extra2.py summary
echo; echo "FULL RUN DONE. Results are in results/*.csv and $LOG"
