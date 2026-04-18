@echo off
setlocal enabledelayedexpansion

REM --- Run from project root ---
cd /d "%~dp0"

REM =====================
REM   CONFIG (EDIT ME)
REM =====================

REM Server
set "PORT=3000"
set "MAX_BOT_COUNT=15"
set "RETRY_AFTER_SEC=45"
set "REQUEST_TIMEOUT_MS=180000"

REM Worker (DeepSeek UI-bot)
set "SERVICE_MODEL=deepseek"
set "HEADLESS=true"
set "VIEWPORT_W=800"
set "VIEWPORT_H=800"

REM Credentials
set "BOT_USERNAME=zimerfmm@gmail.com"
set "BOT_PASSWORD=qp123kfn43"

REM Chrome path: prefer ./worker/chr/chrome.exe, fallback to system Chrome


REM Puppeteer: if you are using your own Chrome and don't want Chromium download
REM set "PUPPETEER_SKIP_DOWNLOAD=1"

echo.
echo ===== Starting API =====
echo PORT=%PORT%
echo MAX_BOT_COUNT=%MAX_BOT_COUNT%
echo SERVICE_MODEL=%SERVICE_MODEL%
echo HEADLESS=%HEADLESS%
echo.

REM Install deps if node_modules is missing
if not exist "node_modules" (
  echo node_modules not found. Running npm i ...
  npm i
  if errorlevel 1 (
    echo npm i failed.
    pause
    exit /b 1
  )
)

npm start

pause
