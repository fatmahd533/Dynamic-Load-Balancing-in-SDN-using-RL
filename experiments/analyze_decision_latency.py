"""
Parses a captured controller stdout log (started with `... | tee log.txt`)
for "DECISION_LATENCY: X.XXXXms" lines and reports summary statistics --
the one "should-have" KPI (decision latency) never measured anywhere
else in this project.

Run with: python3 experiments/analyze_decision_latency.py <log_file>
"""

import sys
import re

PATTERN = re.compile(r'DECISION_LATENCY:\s*([\d.]+)ms')


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 experiments/analyze_decision_latency.py <log_file>")
        sys.exit(1)

    values = []
    with open(sys.argv[1]) as f:
        for line in f:
            m = PATTERN.search(line)
            if m:
                values.append(float(m.group(1)))

    if not values:
        print("No DECISION_LATENCY lines found -- was controller.py's "
              "instrumented version actually running for this log?")
        sys.exit(1)

    values.sort()
    n = len(values)
    mean = sum(values) / n
    median = values[n // 2]
    p95 = values[int(n * 0.95)]

    print(f"Decisions measured: {n}")
    print(f"Mean:   {mean:.4f} ms")
    print(f"Median: {median:.4f} ms")
    print(f"P95:    {p95:.4f} ms")
    print(f"Min:    {values[0]:.4f} ms")
    print(f"Max:    {values[-1]:.4f} ms")


if __name__ == '__main__':
    main()
