@echo off
REM MMKT (MediaMarkt) SEG TV full pipeline, ZenRows-free via local UC.
REM Needs a German IP (VPN/RDP) + Chrome installed. Pass-through args e.g. --steps detail,full,db,notify
cd /d "%~dp0"
python run.py --product tv --concurrency 1 %*
