@echo off
setlocal enabledelayedexpansion

set VBENCH_JSON=C:\workspace\world\VBench\vbench2_beta_i2v\vbench2_beta_i2v\data\i2v-bench-info.json
set VBENCH_CROP=C:\workspace\world\VBench\vbench2_beta_i2v\vbench2_beta_i2v\data\crop\1-1
set PY_SCRIPT=%~dp0vbench_runner.py

cd /d "%~dp0..\\.."

echo Output:    %CD%\outputs\vbench
echo VBench:    %VBENCH_JSON%
echo Crop:      %VBENCH_CROP%

python "%PY_SCRIPT%" ^
    --vbench-json "%VBENCH_JSON%" ^
    --vbench-crop "%VBENCH_CROP%" ^
    --work-dir "%CD%"

exit /b %ERRORLEVEL%
