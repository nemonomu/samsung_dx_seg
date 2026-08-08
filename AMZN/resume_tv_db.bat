@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found in PATH.
    goto :failed
)
python -c "import psycopg2" >nul 2>nul
if errorlevel 1 (
    echo Missing Python dependency: psycopg2
    echo Run: python -m pip install -r requirements.txt
    goto :failed
)

if "%~1"=="" (
    python resume_db.py --product tv --interactive
) else (
    python resume_db.py --product tv --jsonl "%~1" --interactive
)
set "RESULT=%ERRORLEVEL%"
goto :finish

:failed
set "RESULT=1"

:finish
echo.
if not "%RESULT%"=="0" echo TV DB resume did not complete.
pause
exit /b %RESULT%
