@echo off
rem Descarga la ultima version de QSTS. Cierra antes la ventana de QSTS.
cd /d "%~dp0"
echo Carpeta del proyecto: %cd%
echo.
git pull
if errorlevel 1 (
  echo.
  echo *** NO SE PUDO ACTUALIZAR. Copia todo el texto de esta ventana y pasaselo a Claude. ***
  echo.
  git status
  pause
  exit /b 1
)
python -m pip install -e . --quiet
echo.
echo Version descargada:
git log --oneline -1
echo.
echo Listo. Abre QSTS con el icono del escritorio (si estaba abierta, se cerrara la version anterior).
pause
