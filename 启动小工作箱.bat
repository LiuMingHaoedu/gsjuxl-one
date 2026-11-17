@echo off
setlocal
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 (
    start "" python "novel_toolbox.py"
) else (
    start "" pythonw "novel_toolbox.py"
)
exit /b 0
