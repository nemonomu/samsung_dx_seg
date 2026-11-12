@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"
python -B -u dry\run.py %*
set "DRY_EXIT=%ERRORLEVEL%"
popd
exit /b %DRY_EXIT%
