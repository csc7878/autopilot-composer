@echo off
start /min cmd /c "cd /d "%~dp0" && .venv\Scripts\activate && python main_task.py >> run_log.log 2>&1"
