param(
    [ValidateSet('setup','web','crawler','import','doctor')][string]$Mode = 'web',
    [Parameter(ValueFromRemainingArguments=$true)][string[]]$AppArgs
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:AVTOOL_ROOT = $PSScriptRoot
function Check-Exit { if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE" } }
if (!(Test-Path '.venv/Scripts/python.exe')) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $PythonCommand = 'py'; $PythonArgs = @('-3') }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { $PythonCommand = 'python'; $PythonArgs = @() }
    else { throw 'Install Python 3.10+ with PATH enabled, then run this script again.' }
    & $PythonCommand @PythonArgs -c 'import sys; assert sys.version_info >= (3,10)'; Check-Exit
    & $PythonCommand @PythonArgs -m venv .venv; Check-Exit
}
$NeedDependencies = $true
try {
    & .\.venv\Scripts\python.exe -c 'import yt_dlp.version as v; assert tuple(map(int,v.__version__.split(chr(46)))) == (2026,8,19)' 2>$null
    $NeedDependencies = $LASTEXITCODE -ne 0
} catch { $NeedDependencies = $true }
if ($NeedDependencies) {
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt; Check-Exit
}
if (!(Test-Path 'web/out/index.html')) {
    if (!(Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw 'Install Node.js 22.12+ to build the Web UI locally. Source archives do not include built assets.' }
    & node -e 'const [m,n]=process.versions.node.split(String.fromCharCode(46)).map(Number); if(m<22||(m===22&&n<12))process.exit(1)'; Check-Exit
    & npm.cmd ci --ignore-scripts; Check-Exit
    & npm.cmd run build; Check-Exit
}
if ($Mode -eq 'setup') { $Mode = 'doctor' }
& .\.venv\Scripts\python.exe run.py $Mode @AppArgs
Check-Exit
