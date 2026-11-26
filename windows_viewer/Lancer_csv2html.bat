@echo off
title BenchSuite - Convertisseur CSV vers HTML
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "PY_CMD="

call :try_py py -3
if defined PY_CMD goto :run_csv2html

call :try_py python
if defined PY_CMD goto :run_csv2html

call :try_py python3
if defined PY_CMD goto :run_csv2html

if exist "%SCRIPT_DIR%python\python.exe" (
    call :try_py "%SCRIPT_DIR%python\python.exe"
    if defined PY_CMD goto :run_csv2html
)

for %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "C:\Program Files\Python3*" "C:\Python3*") do (
    if exist "%%~D\python.exe" (
        call :try_py "%%~D\python.exe"
        if defined PY_CMD goto :run_csv2html
    )
)

echo.
echo ========================================================
echo   [ERREUR] Python n'a pas ete trouve sur ce PC Windows.
echo ========================================================
echo.
echo Solutions :
echo   1. Installer Python depuis https://www.python.org/
echo      (Cochez bien la case "Add python.exe to PATH" lors de l'installation).
echo   2. Ou depuis le Microsoft Store (cherchez "Python 3.12").
echo.
pause
exit /b 1

:try_py
set "CAND=%*"
%CAND% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul
if %errorlevel%==0 set "PY_CMD=%CAND%"
goto :eof

:run_csv2html
echo ========================================================
echo   BenchSuite - Conversion des fichiers CSV vers HTML
echo ========================================================
echo.
echo Python utilise : %PY_CMD%
echo Dossier courant : %CD%
echo.

if exist "%SCRIPT_DIR%csv2html.py" (
    set "CSV_PY=%SCRIPT_DIR%csv2html.py"
) else if exist "%SCRIPT_DIR%..\csv2html.py" (
    set "CSV_PY=%SCRIPT_DIR%..\csv2html.py"
) else (
    set "CSV_PY=csv2html.py"
)

%PY_CMD% "%CSV_PY%" %*
set "RC=%errorlevel%"
echo.
if not "%RC%"=="0" (
    echo [ATTENTION] La conversion a signale une erreur (code %RC%).
) else (
    echo [SUCCES] Conversion terminee avec succes !
)
echo.
echo Appuyez sur une touche pour fermer cette fenetre...
pause >nul
exit /b %RC%
