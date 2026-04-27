@echo off
set "ROOT=%~dp0"

if exist "%ROOT%.venv\Scripts\pythonw.exe" (
    start "" "%ROOT%.venv\Scripts\pythonw.exe" "%ROOT%src\main.py"
) else (
    start "" pythonw "%ROOT%src\main.py"
)
