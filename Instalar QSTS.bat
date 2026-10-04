@echo off
rem Instala QSTS en este ordenador (una vez): componentes de Python, ventana propia, Yahoo y el icono.
cd /d "%~dp0"
echo Instalando QSTS en: %cd%
echo.
python -m pip install -e ".[dev,desktop,yahoo]"
if errorlevel 1 (
  echo.
  echo *** NO SE PUDO INSTALAR. Comprueba que Python esta instalado con "Add python.exe to PATH". ***
  echo *** Copia todo el texto de esta ventana y pasaselo a Claude. ***
  pause
  exit /b 1
)
python -m qsts.app.shortcut
echo.
echo Listo. Abre QSTS con el icono del escritorio.
pause
