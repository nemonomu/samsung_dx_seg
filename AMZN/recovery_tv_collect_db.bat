@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul
title Amazon TV - Collection Recovery and DB Load
echo Amazon TV - Recover missing details, then validate and load ALL rows.
echo Automatically selects the latest TV run from TV\data\output.
echo Check the selected filename and counts before answering Y.
echo After validation, DB loading starts automatically without another prompt.
echo This is not the DB-only resume_tv_db.bat tool.
echo.
where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found in PATH.
    set "RESULT=1"
    goto :finish
)
if "%~1"=="" (
    python -X utf8 recovery_collect_db.py --product tv
) else (
    python -X utf8 recovery_collect_db.py --product tv --jsonl "%~1"
)
set "RESULT=%ERRORLEVEL%"
:finish
echo.
if not "%RESULT%"=="0" echo Recovery did not complete. Keep the original JSONL and retry this BAT.
pause
exit /b %RESULT%
