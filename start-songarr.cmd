@echo off
rem Starts Songarr in the background (no console window) and opens it in your browser.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m songarr --open
