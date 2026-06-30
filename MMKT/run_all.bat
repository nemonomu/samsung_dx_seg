@echo off
REM MMKT (MediaMarkt) SEG all categories in sequence: TV -> REF -> LDY (ZenRows-free, local UC).
REM Needs a German IP (VPN/RDP) + Chrome. Pass-through args apply to each, e.g. run_all.bat --steps detail,full,db,notify
cd /d "%~dp0"
set PYTHONUNBUFFERED=1
echo ===== MMKT TV =====
python run.py --product tv --concurrency 1 %*
echo ===== MMKT REF =====
python run.py --product ref --concurrency 1 %*
echo ===== MMKT LDY =====
python run.py --product ldy --concurrency 1 %*
echo ===== done =====
