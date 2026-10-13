@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul
title Amazon REF - Collection Recovery and DB Load
echo Amazon REF - Recover missing details, then validate and load ALL rows.
echo Automatically selects the latest REF run from REF\data\output.
echo Check the selected filename and counts before answering Y.
echo After validation, DB loading starts automatically without another prompt.
echo This is not the DB-only resume_ref_db.bat tool.
echo.
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found in PATH.
    set "RESULT=1"
    goto :finish
)
if "%~1"=="" (
    python -X utf8 recovery_collect_db.py --product ref
) else (
    python -X utf8 recovery_collect_db.py --product ref --jsonl "%~1"
)
set "RESULT=%ERRORLEVEL%"
:finish
echo.
if not "%RESULT%"=="0" echo Recovery did not complete. Keep the original JSONL and retry this BAT.
pause
exit /b %RESULT%
