@echo off
cd /d "%~dp0"
py run.py tick >> run.log 2>&1
