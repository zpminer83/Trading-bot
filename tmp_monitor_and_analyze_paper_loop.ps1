Set-Location 'D:\Projects\Trading-bot'
$processPattern = 'scripts\.run_rest_paper_loop'

while (
    Get-CimInstance Win32_Process |
    Where-Object {
        $_.ProcessId -ne $PID -and
        $_.CommandLine -match $processPattern
    }
) {
    $latest = Get-ChildItem 'D:\Projects\Trading-bot\data\paper_runs\paper_run_*.jsonl' |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1

    $lineCount = (
        Get-Content $latest.FullName |
        Measure-Object -Line
    ).Lines

    Write-Host "$(Get-Date -Format 'HH:mm:ss') — records: $lineCount"

    Start-Sleep -Seconds 30
}

Write-Host 'Paper loop finished.'

$latest = Get-ChildItem 'D:\Projects\Trading-bot\data\paper_runs\paper_run_*.jsonl' |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

$lineCount = (
    Get-Content $latest.FullName |
    Measure-Object -Line
).Lines

Write-Host "File: $($latest.FullName)"
Write-Host "Final records: $lineCount"

& '.\.venv\Scripts\python.exe' -m scripts.analyze_paper_run $latest.FullName
& '.\.venv\Scripts\python.exe' -m scripts.analyze_depth_structure $latest.FullName
