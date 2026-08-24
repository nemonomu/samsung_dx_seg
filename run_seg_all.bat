@echo off
setlocal
REM Task Scheduler entry point. Python writes a live 10-day rotating log under .\logs.
set "ROOT=%~dp0"
cd /d "%ROOT%"
python "%ROOT%run_seg_all.py"
set "SEG_EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %SEG_EXIT_CODE%
