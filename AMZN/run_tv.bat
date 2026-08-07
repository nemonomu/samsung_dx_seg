@echo off
REM Amazon.de SEG TV full pipeline. Default includes DB load and email report.
cd /d "%~dp0"
python -c "import bs4, lxml, selenium, undetected_chromedriver, psycopg2" >nul 2>nul
if errorlevel 1 (
    echo Missing Python dependencies. Run this once:
    echo   python -m pip install -r requirements.txt
    exit /b 1
)
python run.py --product tv --email-report %*
