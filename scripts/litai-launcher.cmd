@echo off
setlocal
set "LITAI_PREFIX=%~dp0.."
set "LITAI_HOST_INSTALL=1"
if defined LITAI_EVIDENCE_RUN (
  "%LITAI_PREFIX%\share\literate-ai\venv\Scripts\python.exe" -m literate_ai.step_harness --name launcher -- "%LITAI_PREFIX%\share\literate-ai\venv\Scripts\litai.exe" %*
  exit /b %ERRORLEVEL%
)
"%LITAI_PREFIX%\share\literate-ai\venv\Scripts\litai.exe" %*
exit /b %ERRORLEVEL%
