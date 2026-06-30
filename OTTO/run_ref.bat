@echo off
REM OTTO SEG REF (refrigerator) full pipeline. Default (no args) = full run + DB load + email.
REM Pass args to override, e.g. --save-html (adds raw HTML), --only ... , --db-dry-run.
cd /d "%~dp0"
set "OTTO_ARGS=%*"
if "%~1"=="" set "OTTO_ARGS=--only schema,listing,targets,full,db,notify"
python ref\run.py %OTTO_ARGS%
