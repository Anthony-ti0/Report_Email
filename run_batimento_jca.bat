@echo off
setlocal

set "PROJECT_DIR=C:\Users\adm.gabrieL\Documents\REPORT_EMAIL"
set "PYTHON_EXE=%PROJECT_DIR%\.venv\Scripts\python.exe"
set "LOG_FILE=%PROJECT_DIR%\logs\batimento_jca.log"

cd /d "%PROJECT_DIR%"

echo ============================================== >> "%LOG_FILE%"
echo %date% %time% - iniciando batimento TIM JCA >> "%LOG_FILE%"

"%PYTHON_EXE%" "%PROJECT_DIR%\batimento_tim_jca.py" %* >> "%LOG_FILE%" 2>&1

echo %date% %time% - fim da execucao, exit code %ERRORLEVEL% >> "%LOG_FILE%"

endlocal
