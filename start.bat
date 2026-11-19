@echo off
cd /d "%~dp0"
title yt-downloader
where python >nul 2>&1
if errorlevel 1 goto nopython

python app.py
goto end

:nopython
echo Python was not found in PATH.
pause

:end
