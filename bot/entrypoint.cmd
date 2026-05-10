@echo off
setlocal enableextensions enabledelayedexpansion

echo [entrypoint] PORT=%PORT%
echo [entrypoint] MAX_BOT_COUNT=%MAX_BOT_COUNT%
echo [entrypoint] SERVICE_MODEL=%SERVICE_MODEL%
echo [entrypoint] HEADLESS=%HEADLESS%
echo [entrypoint] BOT_USERNAME=%BOT_USERNAME%

rem 1) Если CHROME_PATH задан в .env и файл существует — используем как есть
if defined CHROME_PATH (
  if exist "%CHROME_PATH%" (
    echo [entrypoint] Using CHROME_PATH=%CHROME_PATH%
    goto :run
  ) else (
    echo [entrypoint] CHROME_PATH set but file not found: %CHROME_PATH%
  )
)

rem 2) Пытаемся дефолтный путь
set "CHROME_PATH=C:\app\worker\chr\chrome.exe"
if exist "%CHROME_PATH%" (
  echo [entrypoint] Using CHROME_PATH=%CHROME_PATH%
  goto :run
)

rem 3) Ищем chrome.exe в worker\chr\<version>\chrome.exe
for /f "delims=" %%F in ('dir /b /s "C:\app\worker\chr\*\chrome.exe" 2^>NUL') do (
  set "CHROME_PATH=%%F"
  echo [entrypoint] Found CHROME_PATH=%CHROME_PATH%
  goto :run
)

echo [entrypoint] ERROR: Could not locate chrome.exe under C:\app\worker\chr
exit /b 1

:run
rem Быстрая проверка, что chrome вообще стартует (и сразу выходит)
"%CHROME_PATH%" --headless --disable-gpu --no-first-run --no-default-browser-check --dump-dom about:blank 1>nul 2>chrome_smoke_err.log
if errorlevel 1 (
  echo [entrypoint] ERROR: Chrome smoke test FAILED. Tail:
  powershell -NoProfile -Command "Get-Content -Path chrome_smoke_err.log -Tail 120"
  exit /b 1
) else (
  echo [entrypoint] Chrome smoke test OK
)

echo [entrypoint] Starting app...
"C:\Program Files\nodejs\npm.cmd" start
