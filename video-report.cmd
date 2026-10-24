@echo off
setlocal
wsl.exe --distribution Ubuntu-26.04 --exec bash /mnt/d/Project/video-report-agent/scripts/run-wsl.sh %*
exit /b %ERRORLEVEL%
