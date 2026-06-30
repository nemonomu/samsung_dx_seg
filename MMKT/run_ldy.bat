@echo off
REM MMKT (MediaMarkt) SEG LDY (Waschmaschinen) full pipeline, ZenRows-free via local UC.
cd /d "%~dp0"
python run.py --product ldy --concurrency 1 %*
