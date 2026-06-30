@echo off
REM OTTO SEG all categories in sequence: TV -> REF -> LDY. Pass-through args apply to each.
cd /d "%~dp0"
echo ===== OTTO TV =====
python tv\run.py %*
echo ===== OTTO REF =====
python ref\run.py %*
echo ===== OTTO LDY =====
python ldy\run.py %*
echo ===== done =====
