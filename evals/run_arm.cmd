@echo off
rem evals\run_arm.cmd — one A/B arm, detached from any terminal session.
rem
rem   evals\run_arm.cmd <name> <seed> <baseline1.json> [baseline2.json] [--case ID]
rem
rem Restarts the model server (a server that has run for hours degrades and
rem reads as a regression), runs the extended suite at 5 runs/case under the
rem given seed, copies the results to evals\results\<name>_r5.json, then runs
rem the gate against the baselines and a compare against the first one.
rem Everything goes to evals\results\<name>.log, so a Monitor (or tail) can
rem follow "=== " section markers, "gate exit", "verdict" and "Traceback".
rem
rem With --case ID only that case is probed (5 runs, not saved): the quick
rem check before spending 40 minutes on the full arm.
rem
rem Launch detached so a session's command timeout cannot kill it:
rem   Start-Process cmd.exe -ArgumentList "/c", "evals\run_arm.cmd guard 20260916 evals\results\baseline_a.json" -WindowStyle Hidden
setlocal
cd /d "%~dp0.."
set NAME=%~1
set SEED=%~2
set B1=%~3
set B2=%~4
if "%NAME%"=="" ( echo usage: run_arm.cmd ^<name^> ^<seed^> ^<baseline1^> [baseline2] [--case ID] & exit /b 2 )
if "%SEED%"=="" ( echo usage: run_arm.cmd ^<name^> ^<seed^> ^<baseline1^> [baseline2] [--case ID] & exit /b 2 )
set LOG=evals\results\%NAME%.log
set OUT=evals\results\%NAME%_r5.json

echo === %NAME%: restart server === >> %LOG%
taskkill /F /IM npurun.exe >nul 2>&1
timeout /t 3 /nobreak >nul
python -c "from hexcli import launcher; launcher._start_npurun_server(); print('npurun up:', launcher._wait_npurun(120))" >> %LOG% 2>&1

if /I "%B2%"=="--case" (
    echo === %NAME%: probe %5, 5 runs === >> %LOG%
    python -u evals\cases_extended.py --case %5 --runs 5 --no-save < NUL >> %LOG% 2>&1
    echo === %NAME% probe done === >> %LOG%
    exit /b 0
)
if /I "%5"=="--case" (
    echo === %NAME%: probe %6, 5 runs === >> %LOG%
    python -u evals\cases_extended.py --case %6 --runs 5 --no-save < NUL >> %LOG% 2>&1
)

echo === %NAME%: full candidate, 5 runs, seed %SEED% === >> %LOG%
python -u evals\cases_extended.py --runs 5 --seed %SEED% < NUL >> %LOG% 2>&1
echo suite exit %ERRORLEVEL% >> %LOG%
copy /Y evals\results\extended_v2_results.json %OUT% >nul

echo === %NAME%: gate === >> %LOG%
if "%B2%"=="" (
    python evals\gate.py --baseline %B1% %OUT% >> %LOG% 2>&1
) else if /I "%B2%"=="--case" (
    python evals\gate.py --baseline %B1% %OUT% >> %LOG% 2>&1
) else (
    python evals\gate.py --baseline %B1% --baseline %B2% %OUT% >> %LOG% 2>&1
)
echo gate exit %ERRORLEVEL% >> %LOG%
echo === %NAME%: compare vs %B1% === >> %LOG%
python evals\compare.py %B1% %OUT% >> %LOG% 2>&1
echo === %NAME% done === >> %LOG%
endlocal
