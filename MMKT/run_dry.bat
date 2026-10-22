@echo off
REM Standalone DRY: collect, validate, backfill NULLs and load TEST by default.
REM --collect-only skips DB. --write-db loads the last validated CSV only.
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
python -B -X utf8 -u -m dry.run %*
