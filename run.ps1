param([Parameter(ValueFromRemainingArguments = $true)][string[]]$ScreenArguments)
$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$EnvironmentDirectory = if ($env:BOND_SCREEN_VENV) { $env:BOND_SCREEN_VENV } else { Join-Path $Root ".venv" }
$Python = Join-Path $EnvironmentDirectory "Scripts/python.exe"
if (-not (Test-Path $Python)) {
    python -m venv $EnvironmentDirectory
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$Requirements = Join-Path $Root "requirements.txt"
$Marker = Join-Path $EnvironmentDirectory ".bond-screen-requirements"
if (-not (Test-Path $Marker) -or (Get-FileHash $Marker).Hash -ne (Get-FileHash $Requirements).Hash) {
    & $Python -m pip install -r $Requirements
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Copy-Item $Requirements $Marker
}
& $Python (Join-Path $Root "scripts/run_market.py") @ScreenArguments
exit $LASTEXITCODE
