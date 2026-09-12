@echo off
rem evals\run_recheck.cmd — the gate's RECHECK step, detached.
rem
rem   evals\run_recheck.cmd <name> <case1,case2,...> <baseline1.json> [baseline2.json]
rem
rem Re-runs the named gated cases at 6 runs on a fresh server, merges them
rem into evals\results\<name>_r5.json (the arm run_arm.cmd produced), and
rem runs the gate again. A case is broken when it passes fewer than 5 of 6.
setlocal
cd /d "%~dp0.."
set NAME=%~1
set CASES=%~2
set B1=%~3
set B2=%~4
if "%CASES%"=="" ( echo usage: run_recheck.cmd ^<name^> ^<case1,case2^> ^<baseline1^> [baseline2] & exit /b 2 )
set LOG=evals\results\%NAME%.log
set OUT=evals\results\%NAME%_r5.json

echo === %NAME% recheck: restart server === >> %LOG%
taskkill /F /IM npurun.exe >nul 2>&1
timeout /t 3 /nobreak >nul
python -c "from hexcli import launcher; launcher._start_npurun_server(); print('npurun up:', launcher._wait_npurun(120))" >> %LOG% 2>&1
echo === %NAME% recheck: %CASES% at 6 runs === >> %LOG%
python -u evals\run_chunk.py --cases %CASES% --runs 6 --out %OUT% < NUL >> %LOG% 2>&1
echo recheck exit %ERRORLEVEL% >> %LOG%
echo === %NAME% gate after recheck === >> %LOG%
if "%B2%"=="" (
    python evals\gate.py --baseline %B1% %OUT% >> %LOG% 2>&1
) else (
    python evals\gate.py --baseline %B1% --baseline %B2% %OUT% >> %LOG% 2>&1
)
echo gate exit %ERRORLEVEL% >> %LOG%
echo === %NAME% recheck done === >> %LOG%
endlocal
