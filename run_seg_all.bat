@echo off
REM SEG retail.com full run — MMKT + OTTO interleaved by product: TV -> REF -> LDY.
REM Needs a German IP (VPN/RDP) + Chrome. Run from a clean German IP.
set ROOT=%~dp0

echo ===== MMKT TV =====
cd /d "%ROOT%MMKT" && python run.py --product tv --concurrency 1
echo ===== OTTO TV =====
cd /d "%ROOT%OTTO" && python tv\run.py

echo ===== MMKT REF =====
cd /d "%ROOT%MMKT" && python run.py --product ref --concurrency 1
echo ===== OTTO REF =====
cd /d "%ROOT%OTTO" && python ref\run.py

echo ===== MMKT LDY =====
cd /d "%ROOT%MMKT" && python run.py --product ldy --concurrency 1
echo ===== OTTO LDY =====
cd /d "%ROOT%OTTO" && python ldy\run.py

echo ===== ALL DONE =====
