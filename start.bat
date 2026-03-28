@echo off
cd /d "%~dp0"
echo Starting AI Trading Bot API...
.venv\Scripts\uvicorn.exe api.server:app --host 0.0.0.0 --port 8000
