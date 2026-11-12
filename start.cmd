@echo off
cd /d "%~dp0"
python -S -m agentboard --open %*
if errorlevel 1 pause
