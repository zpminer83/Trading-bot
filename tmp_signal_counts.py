import json
from pathlib import Path

root = Path("data/paper_runs")
files = sorted(root.glob("paper_run_*.jsonl"), key=lambda p: p.stat().st_mtime)
print(f"{'File':<40} {'TotalRecords':>13} {'SignalRecords':>14}")
for p in files:
    rows = [json.loads(line) for line in p.read_text(encoding='utf-8').splitlines() if line.strip()]
    signal_rows = [r for r in rows if r.get('signal_state') is not None and r.get('signal_depth_imbalance') is not None and r.get('signal_microprice_edge_bps') is not None and r.get('signal_rolling_momentum_bps') is not None]
    print(f"{p.name:<40} {len(rows):13d} {len(signal_rows):14d}")
