@echo off
chcp 65001 >nul
echo.
echo  ContaSimple Dashboard
echo  ─────────────────────────────────
cd /d "%~dp0"

if not exist ".env" (
  echo  [!] Crea el archivo .env primero
  echo      Copia .env.example → .env y rellena tus credenciales
  echo.
  pause
  exit /b
)

pip install -q requests flask python-dotenv
echo.
echo  Abriendo http://127.0.0.1:5050 ...
echo.
python app.py
pause
