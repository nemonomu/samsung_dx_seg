@echo off
REM OTTO SEG TV full pipeline. Pass-through args e.g. --pdp-supplement zenrows --limit 10
cd /d "%~dp0"
python tv\run.py %*
