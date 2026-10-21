@echo off
REM Standalone DRY: collect/validate CSV only unless explicitly passed --write-db.
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
python -B -X utf8 -u -m dry.run %*
