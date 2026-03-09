@echo off
setlocal enabledelayedexpansion

set TOKENIZERS_PARALLELISM=false
set TF_ENABLE_ONEDNN_OPTS=0
set LOCAL_RANK=0
set RANK=0
set WORLD_SIZE=1
set MASTER_ADDR=127.0.0.1
set MASTER_PORT=29500

set VBENCH_JSON=C:\workspace\world\VBench\vbench2_beta_i2v\vbench2_beta_i2v\data\i2v-bench-info.json
set VBENCH_CROP=C:\workspace\world\VBench\vbench2_beta_i2v\vbench2_beta_i2v\data\crop\1-1
set PS_SCRIPT=%~dp0vbench_runner.ps1

cd /d "%~dp0..\\.."

powershell -NoProfile -Command "Get-Date -Format 'yyyyMMdd_HHmmss'" > _tmp_ts.txt
set /p RUN_TS=<_tmp_ts.txt
del _tmp_ts.txt

powershell -NoProfile -Command "Get-Random -Maximum 2000000000" > _tmp_seed.txt
set /p BASE_SEED=<_tmp_seed.txt
del _tmp_seed.txt

echo Output:    %CD%\outputs\%RUN_TS%
echo VBench:    %VBENCH_JSON%
echo Crop:      %VBENCH_CROP%
echo Base seed: %BASE_SEED%

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" ^
    -RunTs "%RUN_TS%" ^
    -BaseSeed %BASE_SEED% ^
    -VbenchJson "%VBENCH_JSON%" ^
    -VbenchCrop "%VBENCH_CROP%" ^
    -WorkDir "%CD%"

exit /b %ERRORLEVEL%
