# `brain` for PowerShell: with this folder on PATH, `brain recall x` works like it does on macOS and Linux.
# Finds Python 3.9+ and runs the `brain` script next to this file in UTF-8 mode (the vault is UTF-8 notes).
$ErrorActionPreference = 'Stop'
$vault = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$finder = Join-Path $vault '_bin\find-python.ps1'
if (-not (Test-Path -LiteralPath $finder)) {
    [Console]::Error.WriteLine("brain: cannot find $finder (is this folder inside the vault?)")
    exit 1
}
. $finder
$py = Find-BrainPython
& $py -X utf8 (Join-Path $PSScriptRoot 'brain') @args
exit $LASTEXITCODE
