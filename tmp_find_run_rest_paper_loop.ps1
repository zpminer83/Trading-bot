$process = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'scripts\.run_rest_paper_loop' }
if ($process) {
    $process | Select-Object ProcessId, CommandLine | Format-List
} else {
    Write-Output 'NO_PROCESS'
}
