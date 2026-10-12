@echo off
cd /d "%~dp0"
set "TRANCY_DESKTOP_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%TRANCY_DESKTOP_PYTHON%" set "TRANCY_DESKTOP_PYTHON=D:\ProjectsPycharm\MultiProjects\.venv\Scripts\python.exe"
"%TRANCY_DESKTOP_PYTHON%" -B "%~dp0tools\prepare_extension.py"
pause
