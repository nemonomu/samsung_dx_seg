@echo off
REM OTTO SEG REF (refrigerator) full pipeline.
cd /d "%~dp0"
python ref\run.py %*
