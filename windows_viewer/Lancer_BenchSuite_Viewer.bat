@echo off
title BenchSuite Viewer - Windows
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "PY_CMD="

call :try_py py -3
if defined PY_CMD goto :run_viewer

call :try_py python
if defined PY_CMD goto :run_viewer

call :try_py python3
if defined PY_CMD goto :run_viewer

if exist "%SCRIPT_DIR%python\python.exe" (
    call :try_py "%SCRIPT_DIR%python\python.exe"
    if defined PY_CMD goto :run_viewer
)

for /D %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "C:\Program Files\Python3*" "C:\Python3*") do (
    if exist "%%~D\python.exe" (
        call :try_py "%%~D\python.exe"
        if defined PY_CMD goto :run_viewer
    )
)

echo.
echo [ERREUR] Python 3.10 ou plus recent n'a pas ete trouve sur ce PC Windows.
echo.
echo Remarque : sous Windows 10/11, la commande "python" par defaut peut etre
echo un raccourci vide du Microsoft Store qui ne lance pas Python.
echo.
echo Solutions :
echo   1. Installer Python 3.12 depuis le Microsoft Store ou python.org
echo      en cochant "Add python.exe to PATH".
echo   2. Ou decompresser "Windows embeddable package" dans :
echo      %SCRIPT_DIR%python\
echo.
pause
exit /b 1

:try_py
set "CAND=%*"
%CAND% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if %errorlevel%==0 set "PY_CMD=%CAND%"
goto :eof

:run_viewer
echo Lancement de BenchSuite Viewer avec : %PY_CMD%
echo.
%PY_CMD% "%SCRIPT_DIR%benchsuite_viewer.py" %*
set "RC=%errorlevel%"
echo.
if not "%RC%"=="0" (
    echo [ERREUR] BenchSuite Viewer s'est arrete avec le code %RC%.
) else (
    echo BenchSuite Viewer ferme.
)
pause
exit /b %RC%
