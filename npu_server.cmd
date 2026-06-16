@echo off
:: NPU server launcher — reads model path and template from shellai_npu.json
setlocal

set SCRIPT_DIR=%~dp0
set CONFIG=%SCRIPT_DIR%shellai_npu.json
set CONDA=%USERPROFILE%\miniconda3\Scripts\conda.exe
set ENV_NAME=shellai-npu
set PORT=8123

if not exist "%CONFIG%" (
    echo shellai_npu.json not found. Run setup first:
    echo   python "%SCRIPT_DIR%setup_npu.py"
    pause
    exit /b 1
)

:: Extract _npu_model_path and _npu_template from shellai_npu.json using PowerShell
for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "$c=Get-Content '%CONFIG%'|ConvertFrom-Json; $c._npu_model_path"`) do set MODEL_PATH=%%A
for /f "usebackq delims=" %%A in (`powershell -NoProfile -Command "$c=Get-Content '%CONFIG%'|ConvertFrom-Json; $c._npu_template"`) do set TEMPLATE=%%A

if "%MODEL_PATH%"=="" (
    echo Could not read _npu_model_path from shellai_npu.json.
    pause
    exit /b 1
)

echo.
echo  NPU Server
echo  Model:    %MODEL_PATH%
echo  Template: %TEMPLATE%
echo  Port:     %PORT%
echo.

"%CONDA%" run -n %ENV_NAME% python "%SCRIPT_DIR%npu_server.py" --model "%MODEL_PATH%" --template %TEMPLATE% --port %PORT%

pause
