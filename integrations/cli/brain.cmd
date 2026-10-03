@echo off
rem `brain` for cmd.exe: with this folder on PATH, `brain recall x` works like on macOS and Linux.
rem Finds Python 3.9+ (py -3, then python, then python3) and runs the brain script next to this file in UTF-8 mode.
setlocal
set "BRAIN_PYRUN="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if not errorlevel 1 set "BRAIN_PYRUN=py -3"
if defined BRAIN_PYRUN goto run
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if not errorlevel 1 set "BRAIN_PYRUN=python"
if defined BRAIN_PYRUN goto run
python3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>nul
if not errorlevel 1 set "BRAIN_PYRUN=python3"
if defined BRAIN_PYRUN goto run
echo brain: Python 3.9 or newer not found. Install it with: winget install Python.Python.3.12 1>&2
exit /b 9
:run
%BRAIN_PYRUN% -X utf8 "%~dp0brain" %*
exit /b %errorlevel%
