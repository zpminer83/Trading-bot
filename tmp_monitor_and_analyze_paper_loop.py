import subprocess
import time
from datetime import datetime
from pathlib import Path

process_pattern = 'scripts.run_rest_paper_loop'

while True:
    proc = subprocess.run(
        [
            'powershell.exe',
            '-NoProfile',
            '-NonInteractive',
            '-ExecutionPolicy',
            'Bypass',
            '-Command',
            'Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json'
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'PowerShell error: {proc.stderr.strip()}')

    try:
        processes = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-Command', 'Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json'],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        raise RuntimeError('powershell.exe not found')

    text = processes.stdout.strip()
    if not text:
        break

    # Convert JSON output to objects in Python
    import json

    try:
        items = json.loads(text)
    except json.JSONDecodeError:
        # sometimes a single object is returned
        if text.startswith('{') and text.endswith('}'):
            items = [json.loads(text)]
        else:
            raise

    running = [item for item in items if process_pattern in (item.get('CommandLine') or '') and item.get('ProcessId') != int(subprocess.check_output(['powershell.exe','-NoProfile','-NonInteractive','-Command','echo $PID']).strip())]
    if not running:
        break

    latest = sorted(Path('data/paper_runs').glob('paper_run_*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)[0]
    line_count = sum(1 for _ in latest.open('r', encoding='utf-8'))
    print(f"{datetime.now():%H:%M:%S} — records: {line_count}")
    time.sleep(30)

print('Paper loop finished.')

latest = sorted(Path('data/paper_runs').glob('paper_run_*.jsonl'), key=lambda p: p.stat().st_mtime, reverse=True)[0]
line_count = sum(1 for _ in latest.open('r', encoding='utf-8'))
print(f'File: {latest}')
print(f'Final records: {line_count}')

subprocess.run(['.\\.venv\\Scripts\\python.exe', '-m', 'scripts.analyze_paper_run', str(latest)], check=True)
subprocess.run(['.\\.venv\\Scripts\\python.exe', '-m', 'scripts.analyze_depth_structure', str(latest)], check=True)
