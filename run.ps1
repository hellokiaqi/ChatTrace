param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CliArgs)
$ErrorActionPreference = 'Stop'
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) { throw 'Create .venv first. See docs/setup.md.' }
Push-Location $PSScriptRoot
try { & $projectPython -X utf8 (Join-Path $PSScriptRoot 'main.py') @CliArgs; $result = $LASTEXITCODE }
finally { Pop-Location }
exit $result
