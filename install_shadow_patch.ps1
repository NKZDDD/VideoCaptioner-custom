$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyCandidates = @(
    (Join-Path $scriptDir "runtime\python.exe"),
    (Join-Path $scriptDir "VideoCaptioner\runtime\python.exe"),
    (Join-Path $scriptDir "python.exe")
)
$py = $pyCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py) {
    Write-Host "ERROR: Python runtime was not found." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}
& $py (Join-Path $scriptDir "patch_shadow.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "Patch failed." -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit $LASTEXITCODE
}
