@echo off
REM OTTO SEG LDY (washing machine) full pipeline.
REM Note: ldy_loading_type (Bauart) is PDP-only; add --pdp-supplement zenrows to fill it.
cd /d "%~dp0"
python ldy\run.py %*
