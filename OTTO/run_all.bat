@echo off
setlocal
REM OTTO SEG all categories in sequence: TV -> REF -> LDY. Each emails on its own completion.
REM Default (no args) = full run + DB load + email per category.
REM Pass-through args apply to each, e.g. --save-html (adds raw HTML), --only ... , --db-dry-run.
cd /d "%~dp0"
set "OTTO_ARGS=%*"
if "%~1"=="" set "OTTO_ARGS=--only schema,listing,targets,full,db,notify"
set "OTTO_EXIT_CODE=0"
echo ===== OTTO TV =====
python tv\run.py %OTTO_ARGS%
if errorlevel 1 set "OTTO_EXIT_CODE=1"
echo ===== OTTO REF =====
python ref\run.py %OTTO_ARGS%
if errorlevel 1 set "OTTO_EXIT_CODE=1"
echo ===== OTTO LDY =====
python ldy\run.py %OTTO_ARGS%
if errorlevel 1 set "OTTO_EXIT_CODE=1"
if "%OTTO_EXIT_CODE%"=="0" echo ===== OTTO RUN SUCCESS =====
if not "%OTTO_EXIT_CODE%"=="0" echo ===== OTTO RUN FAILED =====
endlocal & exit /b %OTTO_EXIT_CODE%
