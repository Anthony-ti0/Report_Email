@echo off
setlocal

set "PROJECT_DIR=C:\Users\adm.gabrieL\Documents\REPORT_EMAIL"
set "PYTHON_EXE=C:\Users\adm.gabrieL\PycharmProjects\Atividades_Uni\.venv\Scripts\python.exe"
set "LOG_FILE=%PROJECT_DIR%\logs\validacao.log"

cd /d "%PROJECT_DIR%"

echo ============================================== >> "%LOG_FILE%"
echo %date% %time% - iniciando validacao diaria >> "%LOG_FILE%"

"%PYTHON_EXE%" "%PROJECT_DIR%\validacao_remessas.py" %* >> "%LOG_FILE%" 2>&1

echo %date% %time% - fim da execucao, exit code %ERRORLEVEL% >> "%LOG_FILE%"

endlocal
