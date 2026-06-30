@echo off
REM MMKT (MediaMarkt) SEG REF (Kühlschränke) full pipeline, ZenRows-free via local UC.
cd /d "%~dp0"
python run.py --product ref --concurrency 1 %*
