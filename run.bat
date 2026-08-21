@echo off
cd /d "%~dp0"
set PATH=C:\Users\yan\AppData\Roaming\npm;%PATH%
"C:\Program Files\ShadowBot\shadowbot-6.2.23\python310\python.exe" app.py
pause