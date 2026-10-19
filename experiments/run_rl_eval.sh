#!/bin/bash
# Full Q-learning evaluation: train two tables, then evaluate every arm
# under simultaneous (sim) and spaced (spaced) flow arrivals.
set -e
REPS=${REPS:-6}
ARMS="shortest_path round_robin least_congested random_path q_learning ql_frozen_busiest ql_frozen_per_link"

python3 experiments/train_ql.py --state busiest
python3 experiments/train_ql.py --state per_link
python3 experiments/gate_matrix.py --reps "$REPS" --stagger 0   --tag sim    --algorithms $ARMS
python3 experiments/gate_matrix.py --reps "$REPS" --stagger 2.5 --tag spaced --algorithms $ARMS

echo
echo "ALL DONE. Summaries:"
python3 experiments/summarize_gate.py --file results/gate_runs_sim.csv
python3 experiments/summarize_gate.py --file results/gate_runs_spaced.csv
