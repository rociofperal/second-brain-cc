# Finds a Python 3.9+ for the Second Brain scripts on Windows. Dot-source it, then call Find-BrainPython:
#     . "$PSScriptRoot\..\_bin\find-python.ps1"
#     $py = Find-BrainPython          # the full path of python.exe; a message and exit 9 when there is none
#     & $py -X utf8 script.py args
#
# Order: BRAIN_PYTHON when set (a script that was started by another one hands its Python down), then
# `py -3` (the launcher python.org installs), then `python`, then `python3`. The Microsoft Store stubs
# that only open the Store fail the version probe and are skipped.
#
# Every Brain entry point starts Python with `-X utf8`: on Windows Python reads and writes files in the
# ANSI code page otherwise, and the vault is UTF-8 notes.

function Test-BrainPython {
    param([string]$Exe, [string[]]$PreArgs = @())
    try {
        $out = & $Exe @PreArgs -c 'import sys; print(sys.executable if sys.version_info[:2] >= (3, 9) else str())' 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) {
            $path = ($out | Select-Object -First 1).ToString().Trim()
            if ($path -and (Test-Path -LiteralPath $path)) { return $path }
        }
    } catch { }
    return $null
}

function Find-BrainPython {
    if ($env:BRAIN_PYTHON) {
        $found = Test-BrainPython -Exe $env:BRAIN_PYTHON
        if ($found) { return $found }
    }
    $candidates = @(
        [pscustomobject]@{ Name = 'py'; PreArgs = @('-3') },
        [pscustomobject]@{ Name = 'python'; PreArgs = @() },
        [pscustomobject]@{ Name = 'python3'; PreArgs = @() }
    )
    foreach ($candidate in $candidates) {
        if (-not (Get-Command $candidate.Name -ErrorAction SilentlyContinue)) { continue }
        $found = Test-BrainPython -Exe $candidate.Name -PreArgs $candidate.PreArgs
        if ($found) { return $found }
    }
    [Console]::Error.WriteLine('Python 3.9 or newer was not found (tried: py -3, python, python3).')
    [Console]::Error.WriteLine('Install it, then open a new terminal and run this again:')
    [Console]::Error.WriteLine('    winget install Python.Python.3.12')
    [Console]::Error.WriteLine('or download it from https://www.python.org/downloads/ (tick "Add python.exe to PATH").')
    exit 9
}
