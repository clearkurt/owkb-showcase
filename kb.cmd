@echo off
rem OWKB CLI wrapper: run `kb <command>` from the repository root.
setlocal
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%;%PYTHONPATH%"
py -3.12 -m app.cli %*
exit /b %errorlevel%
