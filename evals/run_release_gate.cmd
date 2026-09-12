@echo off
rem evals\run_release_gate.cmd — RELEASING.md steps 2, 4 and 5, detached.
rem
rem   evals\run_release_gate.cmd <tag> <multiturn_baseline.json>
rem
rem Fresh server, then: smoke at 1 run/case (must be 10/10; a cold server
rem rebuilds the prompt prefix on the first requests, so a miss on a case
rem that is 5/5 in the arms is worth one rerun), the multi-turn suite at 3
rem runs with 15 s think time compared with the baseline (no case at 3/3
rem may be lost on valid runs), and the stall probe at 800 user tokens,
rem n=20 (at most one hang; run on AC power). Log: evals\results\release_<tag>.log
setlocal
cd /d "%~dp0.."
set TAG=%~1
set MTB=%~2
if "%MTB%"=="" ( echo usage: run_release_gate.cmd ^<tag^> ^<multiturn_baseline.json^> & exit /b 2 )
set LOG=evals\results\release_%TAG%.log

echo === RELEASE GATE %TAG%: restart server === >> %LOG%
taskkill /F /IM npurun.exe >nul 2>&1
timeout /t 3 /nobreak >nul
python -c "from hexcli import launcher; launcher._start_npurun_server(); print('npurun up:', launcher._wait_npurun(120))" >> %LOG% 2>&1
timeout /t 20 /nobreak >nul
echo === RELEASE GATE %TAG%: smoke === >> %LOG%
python -u evals\cases_smoke.py < NUL >> %LOG% 2>&1
echo smoke exit %ERRORLEVEL% >> %LOG%
echo === RELEASE GATE %TAG%: multiturn, 3 runs, think time 15 === >> %LOG%
python -u evals\cases_multiturn.py --runs 3 --think-time 15 < NUL >> %LOG% 2>&1
echo multiturn exit %ERRORLEVEL% >> %LOG%
copy /Y evals\results\multiturn_v2_results.json evals\results\multiturn_%TAG%.json >nul
echo === RELEASE GATE %TAG%: multiturn compare vs %MTB% === >> %LOG%
python evals\compare.py %MTB% evals\results\multiturn_%TAG%.json >> %LOG% 2>&1
echo === RELEASE GATE %TAG%: stall probe === >> %LOG%
python -u tools\backend_bench\stall_rate.py --tag %TAG% --user-tokens 800 --n 20 --max-tokens 400 >> %LOG% 2>&1
echo stall exit %ERRORLEVEL% >> %LOG%
echo === RELEASE GATE %TAG% done === >> %LOG%
endlocal
