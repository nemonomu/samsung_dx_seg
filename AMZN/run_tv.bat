@echo off
REM Amazon.de SEG TV full pipeline. Default includes DB load and email report.
cd /d "%~dp0"
python run.py --product tv --email-report %*
