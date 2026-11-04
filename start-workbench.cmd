@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -m ai_workflow.web --root "%~dp0."
if errorlevel 1 pause
