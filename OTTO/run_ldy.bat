@echo off
REM OTTO SEG LDY (washing machine) full pipeline. Default (no args) = full run + DB load + email.
REM ldy_loading_type (Bauart) is collected Kasada-free via /vergleich/; no PDP supplement needed.
REM Pass args to override, e.g. --save-html (adds raw HTML), --only ... , --db-dry-run.
cd /d "%~dp0"
set "OTTO_ARGS=%*"
if "%~1"=="" set "OTTO_ARGS=--only schema,listing,targets,full,db,notify"
python ldy\run.py %OTTO_ARGS%
