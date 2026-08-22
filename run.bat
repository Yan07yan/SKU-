@echo off
cd /d "%~dp0"

set "PROJECT_DIR=%~dp0"
set "CODEX_AUTH=%USERPROFILE%\.codex\auth.json"
set "PROJECT_CODEX_AUTH=%PROJECT_DIR%.codex\auth.json"
set "PROJECT_CODEX_AUTH_BACKUP=%PROJECT_DIR%codex_auth_backup\auth.json"

echo ========================================
echo SKU Workflow Dashboard
echo Project: %PROJECT_DIR%
echo ========================================
echo.

if exist "%APPDATA%\npm" (
  set "PATH=%APPDATA%\npm;%PATH%"
)

if exist "%CODEX_AUTH%" (
  echo [OK] Codex auth found: %CODEX_AUTH%
) else (
  echo [WARN] Codex auth not found: %CODEX_AUTH%
  echo.
  echo Please login on this computer before using Codex-related features:
  echo   codex login
  echo.
  if exist "%PROJECT_CODEX_AUTH%" (
    echo [INFO] Project contains .codex\auth.json, but it was NOT copied automatically.
  )
  if exist "%PROJECT_CODEX_AUTH_BACKUP%" (
    echo [INFO] Project contains codex_auth_backup\auth.json, but it was NOT copied automatically.
  )
  echo For account safety, do not share or auto-copy another user's auth.json.
  echo.
)

if exist "C:\Program Files\ShadowBot\shadowbot-6.2.23\python310\python.exe" (
  "C:\Program Files\ShadowBot\shadowbot-6.2.23\python310\python.exe" app.py
) else (
  py -3 app.py
  if errorlevel 1 (
    python app.py
  )
)
pause
