@echo off
setlocal EnableExtensions DisableDelayedExpansion

for %%D in ("%PATH:;=" "%") do (
    if exist "%%~D\python3.exe" call :probe "%%~D\python3.exe"
    if defined LITERATE_AI_PYTHON goto found
    if exist "%%~D\python.exe" call :probe "%%~D\python.exe"
    if defined LITERATE_AI_PYTHON goto found
)
exit /b 0

:probe
call "%~1" "%~dp0python_resolver.py" --check >nul 2>nul
if not errorlevel 1 set "LITERATE_AI_PYTHON=%~1"
exit /b 0

:found
echo %LITERATE_AI_PYTHON%
exit /b 0
