@echo off
setlocal
REM Standalone retry: collect, validate, backfill, load production and notify.
REM --collect-only skips DB. --write-db loads the last validated CSV only.
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
python -B -X utf8 -u -m dry.run %*
set "DRY_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %DRY_EXIT_CODE%
