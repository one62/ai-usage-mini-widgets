@echo off
setlocal
set "ROOT_DIR=%~dp0"
set "WIDGET_DIR=%ROOT_DIR%codex-usage-widget"

powershell -NoProfile -Command ^
  "$procs = Get-CimInstance Win32_Process -Filter \"Name = 'pythonw.exe' OR Name = 'python.exe'\" | Where-Object { $_.CommandLine -match 'codex-usage(-mini)?-widget(-main)?[\\/]+widget\.py' }; foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; Wait-Process -Id $p.ProcessId -Timeout 5 -ErrorAction SilentlyContinue }"

set "PYW="
for /f "delims=" %%I in ('where pythonw.exe 2^>nul') do (
  echo "%%~I" | findstr /i "\\WindowsApps\\" >nul
  if errorlevel 1 if not defined PYW set "PYW=%%~I"
)
if defined PYW (
  start "" /d "%WIDGET_DIR%" "%PYW%" "%WIDGET_DIR%\widget.py"
) else (
  where pyw.exe >nul 2>nul
  if not errorlevel 1 (
    start "" /d "%WIDGET_DIR%" pyw.exe "%WIDGET_DIR%\widget.py"
  ) else (
    start "" /d "%WIDGET_DIR%" python "%WIDGET_DIR%\widget.py"
  )
)
endlocal
