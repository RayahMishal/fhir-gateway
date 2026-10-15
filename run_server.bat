@echo off
cd /d "%~dp0"
echo === FHIR Gateway setup (first run takes about a minute) ===
set PY=python
where py >nul 2>nul
if %errorlevel%==0 set PY=py -3
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo Could not create the Python environment. Is Python installed?
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo Package install failed. Check your internet connection.
  pause
  exit /b 1
)
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:5000/docs"
echo Server starting. Close this window to stop it.
".venv\Scripts\python.exe" api_server.py
pause
