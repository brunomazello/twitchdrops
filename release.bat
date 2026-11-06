@echo off
setlocal
rem Publishes a new version for everyone (the GitHub workflow builds the .exe and
rem installed copies update themselves). Usage: release.bat 1.0.1

cd /d "%~dp0"

set "VERSION=%~1"
if "%VERSION%"=="" set /p "VERSION=Nova versao (ex.: 1.0.1): "
if "%VERSION%"=="" (
    echo Nenhuma versao informada.
    exit /b 1
)

git diff --quiet && git diff --cached --quiet
if errorlevel 1 (
    echo Ha alteracoes sem commit. Faca o commit antes de publicar.
    exit /b 1
)

powershell -NoProfile -Command "(Get-Content version.py) -replace '^__app_version__ = .*', '__app_version__ = \"%VERSION%\"' | Set-Content version.py -Encoding ascii"
git add version.py
git commit -m "Release v%VERSION%" || exit /b 1
git tag "v%VERSION%" || exit /b 1
git push origin HEAD:main || exit /b 1
git push origin "v%VERSION%" || exit /b 1

echo.
echo Versao v%VERSION% enviada. O GitHub vai compilar e publicar em alguns minutos:
echo https://github.com/brunomazello/twitchdrops/actions
endlocal
