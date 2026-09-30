@echo off
REM Сборка FPSBooster.exe (нужен Python 3.10+ с галочкой "Add to PATH")
chcp 65001 >nul
python -m pip install --upgrade pip || goto :error
python -m pip install -r requirements-dev.txt || goto :error
python -m PyInstaller --noconfirm --onefile --windowed --uac-admin ^
  --name FPSBooster ^
  --add-data "fps_booster/ui;fps_booster/ui" ^
  run.py || goto :error
python -m PyInstaller --noconfirm --onefile --windowed ^
  --name MicPro ^
  --add-data "mic_booster/ui;mic_booster/ui" ^
  run_mic.py || goto :error
echo.
echo Готово: dist\FPSBooster.exe и dist\MicPro.exe
pause
exit /b 0
:error
echo Сборка не удалась.
pause
exit /b 1
