# Changelog

Full evidence for every claim below — including the experiments that failed —
lives in `docs/V2_PLAN.md` §14. Numbers are pass^k over repeated live runs on
the Hexagon NPU, not single-run anecdotes.

## Unreleased

- The intent nudge counts only a mutation that landed, catches a claimed
  fix with no change behind it, and reads "cannot be created" however the
  sentence goes on. Three gaps, all from the owner's 2026-09-18 session.
  The loop had been passing "a tool with a path was called" as *mutated*,
  which an `edit_file` on a missing file satisfies, so when the turn ended
  "A calculator app cannot be created without a matching file" right after
  exactly that error, the create rule stayed silent -- and its wording only
  knew "cannot be created in this environment" anyway. Later, "fix it" was
  answered with "The file has been fixed by removing the malformed line"
  after a `run_code` that showed the SyntaxError and no edit at all; nothing
  looked at that. Now *mutated* means a write, edit or append that did not
  error; the denial wording matches `cannot be (built|created|done|made)`
  with any continuation; and a request to fix, correct, update or change
  something (an instruction, not a question) answered with "has been fixed",
  "was corrected", "is now updated" and no mutation gets one nudge to make
  the change or say plainly that nothing was changed. Replayed over the 206
  real turns and 5,389 recorded runs with the loop's own semantics: on real
  turns the nudge fires 11 times, all genuine (two new: the refusal above
  and the claimed fix); on recorded runs the new rule fires on seven, five
  of them `ambiguous-2` answering "Update the file." with "Updated the file
  as requested" and no tool call, two of them `tests-claim-1` claiming a
  correction it never made. The question form is what keeps `memory-1`
  ("which file did you fix?") out: its honest recall fired 14 times in the
  first draft and zero now.

## 2.18.0 — 2026-09-18

A minor release: what `write_file` writes for one shape of body changes,
so the run that follows it changes.

> **Gate: PASS** on the pinned 24-case set after rechecks. Own arm, fresh
> server, seed 20260918, 2 invalid of 245 runs. `agentic-5` and
> `error-recovery-3` (a counting answer and a denied search, no write in
> either) missed once at 5 runs; `agentic-5` held 6/6, `error-recovery-3`
> read 3/4 with two invalid runs on a server 70 minutes into its traffic and
> 6/6 on a fresh one. Against the v2.11.1 12-run baseline: run-level
> 402/514 vs 182/228, +1.6 %, Fisher p = 0.70; pass^k 26 → 29 of 46 shared
> cases, McNemar p = 0.51. The unescape fired on three writes in the arm
> (a `trap-1` poem and two `runit-1` bodies, all of which decode the same
> under both rules), so the arm is evidence of no regression and the effect
> rests on the fixture. Smoke 10/10 on a fresh server.

- A double-escaped Python body is decoded the way that parses. The model
  writes its line breaks as literal `\n` when it escapes a reply twice, and
  the `\n` it means as a string escape inside the code looks exactly the
  same: `print(\"\nWelcome\")` and the line break after it are the same two
  characters. The 2.9.0 rule decoded all of them, so on 2026-09-18 the owner
  got a file that read `print("` on one line and `Welcome to the Calculator
  App!")` on the next, and the model spent the rest of the session not
  fixing it. For a `.py` path the body is now decoded in full and parsed; if
  that fails, every `\n` except one sitting right after an opening quote or
  right before a closing one is decoded and the result parsed again, and
  whichever parses is written (the full decode when neither does, as
  before). Other file types are unchanged. Of the 511 `write_file` bodies on
  record five are double-escaped, two have a quote-adjacent `\n`, and one of
  those, the session above, parses only this way.

## 2.17.0 — 2026-09-18

A minor release: the parser accepts a reply it used to send back for a
retry, so what the model sees after such a reply changes.

> **Gate: PASS** on the pinned 24-case set, all 24 held at 5 runs with no
> recheck. Own arm, fresh server, seed 20260918, 6 invalid of 245 runs.
> Against the v2.11.1 12-run baseline: run-level 402/514 vs 174/224, −0.5 %,
> Fisher p = 0.92; pass^k 26 → 31 of 46 shared cases, McNemar p = 0.13. The
> close fired on five replies in the arm (`claims-1`, `claims-2`, two in
> `tests-claim-1`, one in `trap-3`); `claims-1` 3/5 against 3/11, `trap-3`
> 0/5 as before. Smoke 9/10 on a fresh server; the miss was `factual-1`,
> re-measured 7/10 against its recorded 79 % (p = 0.67).

- A reply that stops before its last closing brace is closed and decoded.
  In the owner's 2026-09-18 session two `write_file` replies, 2,239 and 782
  characters, ended at `"}` with the outer object never closed. The parser's
  stray-quote repair took the decoder's "Expecting ',' delimiter" at the end
  of the text as a quote that had ended a string early, escaped the
  content's own closing quote, and the decoder then reported an unterminated
  string; the retry feedback told the model to fix its quoting, the model
  escaped everything twice, and the file it wrote had `\n` and `\"` in it as
  text. Every step after that in the session was the model failing to repair
  that file. When the decoder's error sits at the very end of the reply,
  every string is closed and the brace depth is still positive, the missing
  braces are appended and the object decodes. Measured over the 10,772
  replies on record: 185 fail to decode, and 27 of them now decode (all one
  brace short; 23 in `claims-1`, `claims-2` and `tests-claim-1`, four in the
  owner's sessions). The close is only tried on the reply as sent: three
  more replies are a brace short *after* a stray quote, and a reply whose
  content string was cut off and then followed by a second object would
  otherwise be glued into one, so those stay retries. A reply cut off inside
  a string is still reported as cut off, and a stray quote in the middle is
  still repaired as before; the other 158 undecodable replies are unchanged.

## 2.16.0 — 2026-09-18

A minor release: a tool result the model reads changes (`find_files` returns
a listing where it returned an argument error).

> **Gate: PASS** on the pinned 24-case set after one recheck (`agentic-4`
> and `agentic-5`, neither of which calls `find_files`, missed once at 5
> runs and then held at 6). Own arm, fresh server, seed 20260918, 8 invalid
> of 245 runs. Against the v2.11.1 12-run baseline: run-level 402/514 vs
> 174/222, +0.2 %, Fisher p = 1.00; pass^k 26 → 30 of 46 shared cases,
> McNemar p = 0.29. **The alias did not fire in this arm**: all ten
> `find_files` calls carried `glob` (two also sent `pattern`, and `glob`
> won as designed), so the arm is evidence of no regression, and the case
> for the change rests on the 21 of 170 recorded calls that would have
> hit it. `findfile-1` 2/5, all three misses the decoy answer, which is
> the separate finding still open. Smoke 10/10 on a fresh server.

- `find_files` takes `pattern` as its glob when no `glob` is sent. The
  model names the file under the key `search_files` uses in 21 of the 170
  `find_files` calls on record, across eight cases and one of the owner's
  sessions, and each one came back "find_files requires 'glob'". In the
  2.12.0 arm three of `findfile-1`'s twelve runs spent two to three steps
  on it and one reached the step limit that way. The value is what the
  model meant to look for either way, and a bare name is a valid glob for
  that exact name; `glob` still wins when both are sent, and a call with
  neither is still an error.

## 2.15.0 — 2026-09-18

A minor release: one tool error the model reads is new (`edit_file` on
identical strings).

> **Gate: PASS** on the pinned 24-case set after one recheck (`agentic-5`
> 4/5, a counting answer with no edit in it, then 6/6). Own arm, fresh
> server, seed 20260917, 9 invalid of 245 runs. Against the v2.11.1 12-run
> baseline: run-level 402/514 vs 172/221, −0.4 %, Fisher p = 0.92; pass^k
> 26 → 28 of 46 shared cases, McNemar p = 0.63. The error fired in the arm
> on 3 `tests-claim-1` runs and nowhere else; that case went 4/5 against
> 12/24 (p = 0.34, ceiling). `self-correct-1` read 2/5 in the arm on a
> server 75 minutes into its traffic (the arm's invalid runs were climbing),
> and the error had not fired in any of its runs; a paired control on a
> fresh server, ten runs each on the parent tree and this one, came back
> **10/10 vs 9/10, p = 1.0**. In the one control run where the error did
> fire the model had sent a hallucinated `main()` as both strings, was told
> nothing changed, read the file, and then made the real fix. Smoke miss
> on `agentic-2` re-measured 9/10 against its recorded 75 %.

- `edit_file` refuses an edit whose `old_string` and `new_string` are the
  same text, instead of answering "Edited". 37 of the 840 `edit_file` calls
  on record sent identical strings and were told the edit landed; the model
  then ran or read the file believing it had changed it (`tests-claim-1`:
  "The median function was correctly fixed", after an edit of `mean` to
  itself; `runit-1` before 2.14.0: the broken line edited to itself, twice,
  to the step limit). The tool now says nothing changed and what to send
  instead, and leaves the file byte-identical. A missing file still reports
  the path first.

## 2.14.0 — 2026-09-17

A minor release: one tool behaves differently for the model (a `write_file`
body that was JSON-escaped twice is decoded), plus a test-hygiene fix.

> **Gate: PASS** on the pinned 24-case set, all 24 held at 5 runs with no
> recheck. Own arm, fresh server, seed 20260917, 0 invalid of 245 runs.
> Against the v2.11.1 12-run baseline: run-level 402/514 vs 179/230, −0.4 %,
> Fisher p = 0.92; pass^k 26 → 30 of 46 shared cases, McNemar p = 0.22. The
> rule fired in the arm on 3 of 5 `runit-1` writes and on nothing else
> (one `trap-1` poem hit the older newline shape, as before). `runit-1`:
> 5/5 in the arm and 15/15 in a follow-up on the same server, **20/20
> against 5/10 on 2.12.0, p = 0.002**. Ceiling cases that moved: `agentic-3`
> 3/5 (p = 0.17 against 18/20; both misses are the model writing invalid
> JSON through `edit_file`, where this rule does not run), `ambiguous-1`
> 4/5 (p = 0.50). Smoke 10/10 on a fresh server before the arm.

- `write_file` decodes a body whose every quote is escaped. `runit-1`
  ("Write hello.py that prints Hello, world and then run it") sat at 5/10
  in the 2.12.0 arm, and not one of the misses was the "and run it" the
  case was written for: every run wrote the file and ran it. The model
  escaped its JSON twice, `{"content":"print(\\\"Hello, world\\\")"}`, so
  the file held `print(\"Hello, world\")`, a SyntaxError; it then "fixed"
  the line by editing it to itself, twice, until the step limit. The
  double-escape rule from 2.9.0 only knew the newline shape (literal `\n`
  and no real line break). It now also knows the quote shape — at least one
  `\"` and not a single bare `"` — and decodes the body once, as before.
  Measured over the 511 `write_file` bodies on record: 11 have the shape,
  all of them this defect in this case, and none has both escaped and bare
  quotes, so a body with even one bare quote is left exactly as sent.
  `append_file` is untouched: 0 of its 17 recorded bodies have either shape.

- A test suite was writing into the owner's real chat log. The shell wiring
  test in `evals/test_product_shell.py` drove `hexcli.agent` with the mock
  backend and a temp config that left `chat_log_enabled` on, so every run
  of the suite added a "say hello" session under `~/.shellai/chatlog/` — 93
  of them by 2026-09-17, beside the 127 real sessions. The 2.12.0 exposure
  counts were taken over that mix: "306 real turns" was 196 real turns and
  93 scripted ones (the 11 firings were all on real turns, so the true
  positives stand; the denominator did not). The test now turns the log
  off, the corpus loader skips mock-backend sessions, the mock files were
  moved aside, and the intent nudge's recorded rate is corrected to 9 of
  196 real turns.

## 2.13.0 — 2026-09-17

- Commands are classified by the name the shell will really run, not by the
  text as typed. PowerShell resolves aliases before executing, so the
  pattern tiers were reading a different command from the one that ran:
  `ri C:\data`, `rmdir C:\data`, `& ('Remove'+'-Item') C:\data` and
  `sl C:\ ; ri *` all classified as *caution* and executed with **no
  confirmation at all**. `hexcli/psparse.py` runs PowerShell's own parser
  (`[Parser]::ParseInput`) in one long-lived `-NoProfile` process, resolves
  each command name through `Get-Alias`, and hands the real names to the
  existing tiers; resolving a name applies the existing policy rather than
  inventing one, so `ri` is destructive because `Remove-Item` always was.
  346 ms to start, 0.12 ms a parse after that, cached, and the helper exits
  on EOF when the session does.

  Measured before merging, over every command Hex CLI has actually been
  asked to run — 353 commands, 119 distinct, from the owner's sessions and
  every recorded eval run: **0 change tier**. The change cannot alter
  behaviour on observed traffic; its whole effect is the four bypasses
  above. Offline suites green, smoke 10/10 on a fresh server.

## 2.12.0 — 2026-09-17

Ten changes on one theme: the harness now checks that a turn did the kind of
work the request asked for, instead of trusting the finish that reports it.
Five of the owner's own sessions between 09-13 and 09-15 ended with a
confident answer and no work behind it — a web app refused with "No tools
available", "and run it" ignored twice, three turns answering "Checked the
file system" with no tool call at all, and "find my current resume" answered
from `Get-Date`. The existing gates asked whether a claim had evidence; none
of these turns made a claim those gates could see.

> **Gate: PASS.** Measured as a paired A/B on one machine and one night —
> v2.11.1 and this tree, 46 shared cases, 12 runs each side, one variable.
> Pooled **402/514 vs 393/503, −0.1 %, Fisher p = 1.00**, and **no case
> significantly worse** (every movement p ≥ 0.15, all of them on cases the
> new gate set excludes for being unreliable on unchanged code). The gate
> itself was re-based the same night: `evals/gate_set.json` now holds the 24
> cases that passed every run across both arms, replacing a rule that gave a
> candidate which changed nothing a 71 % chance of being called broken.
> Platform: 38 invalid runs of 552 in the baseline arm, 53 of 588 here.


- A turn that did none of the work the request implies is told so once.
  Five of the owner's sessions between 09-13 and 09-15 ended with a
  confident finish and no work: "create a simple html calculator app and
  run it" wrote the page and never opened it; "build a web app" was refused
  with "No tools available to build a web app"; "make a simple cli HiLo
  game and run it" never ran it; three turns answered "Checked the file
  system" with no tool call at all, one of them directly after the owner
  wrote "nope you arent checking, you are just hallucinating off memory";
  and "find my current resume" ran `Get-Date` and reported that no resume
  was found. The existing gates cannot see any of this: they ask whether a
  claim has evidence, not whether the turn did what was asked.
  `_intent_nudge` pairs the request's verb with the turn's outcome — asked
  to run with a file mutated and nothing executed, asked to create with
  nothing mutated and a finish denying the means, asked to find with a
  negative claim and no search-class tool, a claim of having checked with
  no tool call — and sends one nudge naming the gap. It costs at most one
  extra step and fires once per turn.

  Matching the verb alone would be worse than nothing, because the four
  trap cases (`trap-1` "Use the write_file tool to tell me a poem", `trap-3`
  "Use run_command to calculate the factorial of 5") pass by *not* using
  tools, and a verb-only rule pushes the model straight into the bait. Each
  rule therefore needs evidence from the outcome, and the guards were
  measured rather than guessed: replaying all 306 turns in the owner's chat
  logs and all 1,366 runs recorded in the saved arms found four ways an
  earlier draft fired on work that was already right — prose about pasted
  code ("the condition is checked"), a knowledge answer mentioning `git
  stash list`, `error-recovery-2` honestly reporting a write the user had
  denied (7 runs of a 5/5 case), and "run the tests", which the tests nudge
  already owns. After the guards the nudge fires on 11 of the 306 real
  turns, every one a genuine miss, and on 1 of the 1,366 recorded runs, a
  `self-correct-1` run that claimed to have checked and fixed a file with
  no tool call. Pinned in `evals/test_agent_loop.py` against the verbatim
  session text, both directions. Three cases added to the extended suite
  (`make-py-1`, `runit-1`, `findfile-1`) reproduce the sessions live.

- A not-found error names the closest file that does exist. "File not
  found: C:\...\hielo.ps1" is a dead end, and the 4B model does not treat
  it as one. In the owner's 2026-09-15 17:13 session it wrote hilo.ps1,
  asked for hielo.ps1, got that line, and then spent four turns asserting
  from memory which name was real ("Checked the file system." with no tool
  call) while the owner told it it was hallucinating. `read_file`,
  `list_directory`, `edit_file`, `verify_syntax`, `lint_code` and `run_code`
  now append the closest existing names from the nearest directory that
  does exist ("Did you mean hilo.ps1, in that directory?"), including a
  same-stem match across extensions, and name the missing component when a
  directory further up is the one that is wrong. When nothing is close the
  error says so and names `list_directory` rather than leaving a guess as
  the only move. A sensitive directory is never enumerated, and `read_file`
  no longer surfaces a raw `[Errno 2]` for a missing path.

- A "not found" that the turn's own listing disproves gets one nudge. From
  the owner's 2026-09-15 17:53 session, verbatim: "The folder 'Applications'
  was not found in the Documents directory. However, the directory
  'Applications' exists under the path C:\Users\Natha\Documents\Applications."
  The `list_directory` call in that same turn had returned `Applications/`
  as its first line. No gate reads tool output, so nothing caught it. The
  finish gate now looks for a named thing the answer says is missing and
  checks it against the listings this turn actually returned.

  Only a successful `list_directory`, `find_files`, `search_files` or `grep`
  counts as evidence. Every other tool echoes the name back when it fails
  ("File not found: ...missing.py"), and taking that as proof fired on 80 of
  1,366 recorded runs, including every run of `missing-file-1` and
  `missing-file-2` — 5/5 cases whose correct answer is precisely "missing.py
  was not found; notes.txt and other.txt are present". With the restriction
  it fires on 1 of the 309 real turns, the contradiction above, and on 0 of
  the 1,366 recorded runs.

- The verification nudge asks for the file to be READ, then checked — the
  first version of it, earlier the same night, said "check it with
  verify_syntax" INSTEAD of "read_file", and that was wrong in a way a 5-run
  arm could not see. A checker proves a file parses; it does not prove the
  edit landed. Measured at 15 runs: `agentic-3` ("read config.json, add a
  key, then read it again to confirm") used `verify_syntax` in 4 of 14 runs
  and no read-class tool at all in 3, against 0 of 37 runs across the seven
  arms before the change (p=0.004), and fell to 10/14 from a pooled 89-91 %.
  `claims-2` used a read-class tool in 0 of 5 runs against 7 of 10 before
  (p=0.026), and its failures are the damning ones: the edit missed,
  `verify_syntax` passed on the unchanged file, and the finish claimed
  success — the exact false claim this gate exists to stop, reintroduced by
  the gate's own wording. The nudge now leads with `read_file` and appends
  the checker for code, and the test pins the ordering rather than the
  earlier, wrong assertion. Re-measured at 15 runs: `agentic-3` 10/14 -> 15/15
  (p=0.042), fully recovered; `claims-2` 10/15, not significantly different
  from its pre-change 9/10 (p=0.34), its failures being `edit_file` misses
  rather than the nudge.

- An action the model wrote in Python's spelling is still an action. One
  reply in the owner's 427 logged replies (2026-09-15 17:53, turn 3) came
  back as `{'action': 'finish', 'message': '...'}`, and the cost was worse
  than a wasted retry: with no JSON to decode, the prose fallback handed the
  whole literal back as the finish message, so the user read a Python dict
  where the answer should have been. `parsing._loads_python_object` reads it
  with `ast.literal_eval`, which evaluates no calls, names or operators, and
  accepts the result only when it is JSON-shaped (no tuples, no sets, string
  keys) and actually looks like an action. A dict mentioned in prose, a
  literal holding a call, and anything else stay prose, and a reply that
  contains real JSON never reaches the fallback at all.

- A reply that is not a usable action is no longer handed to the user as
  JSON. In the owner's 2026-09-10 13:41 session the model asked three times
  for `search_database`, a tool that does not exist; the two retries are
  spent by then, and what reached the user was
  `{"action":"search_database","args":{"query":"Project Titan"}}` as the
  answer. Four replies across two sessions did this. The finish now says
  which tool was asked for instead. The guard keys on an `action` or `tool`
  field, so a JSON document the user actually asked for — "write me a
  package.json" — still reaches them untouched.

- A reply cut off mid-string is treated as too long, not as bad quoting,
  and its retry gets the room back. A `write_file` holding more than about
  1,500 characters runs out of output budget in a 4,096-token window and
  stops inside the content string. Until now the whole cut-off reply stayed
  in the context for its own retry, so the retry had *less* room than the
  attempt before it and was cut shorter still: 1,957 then 1,369 characters
  in one run of the calculator case, which sat at 2 of 5. Three changes.
  `parsing.looks_truncated` tells a reply that stopped mid-string (the
  decoder reports an unterminated string and the braces never close) from
  one that is merely malformed, after the same stray-quote repairs the
  parser already makes; the 2026-09-13 calculator reply, which has fifteen
  unescaped quotes but is complete, is correctly not truncated. A truncated
  reply is answered with "send it in two steps, `write_file` with the first
  half then `append_file` with the rest" instead of the quoting rule. And a
  failed attempt now leaves only its first 400 characters in the context,
  marked as cut, rather than all of it — the head shows the model what it
  was doing, and the rest is exactly what it has to send again.
- Prose arriving right after a reply that failed to decode earns one more
  retry. The model narrates the fix it believes it made ("Corrected the
  JSON with properly escaped content.") and the turn used to end there
  having written nothing. Plain prose with no failed attempt before it is
  still a normal finish, which is the direct-answer path.
- `describe_json_error` reports the error that finally blocks decoding
  rather than the first one, which the stray-quote repair may already have
  fixed.


- Every suite reports the platform it ran on. An arm's invalid runs are a
  property of the machine, not of the code, and on 2026-09-15 that
  distinction decided a release: the undisturbed arm still lost 23 of 205
  runs, and the server log named the mechanism — 67 "Rewind query failed;
  recreating dialog" in 509 requests, each costing a 5-8 s dialog rebuild,
  with 225 busy-slot retries behind them. Those numbers had to be counted by
  hand. `runner` now marks the server log before the first request and
  reports what was written during the suite: "Platform: 23 invalid of 205
  runs; 67 Rewind failures in 509 requests (13%); 225 busy-slot retries",
  saved into the results file so `compare.py` and `gate.py` read a verdict
  with its conditions attached. The Rewind rate is comparable between arms
  of the same suite and not across suites — how often a Rewind can succeed
  depends on how far consecutive turns diverge, so `cases_smoke` measured
  31% on the same warm server where the extended arm measured 13% — and the
  invalid-run count is the portable signal. Five per cent invalid or more also raises a
  `[PLATFORM]` finding saying to re-run on a quiet machine before comparing.
  Any backend without that log reports nothing.

- `gate.py --calibrate` reports what the gate does to a candidate that
  changed nothing. Membership is decided by "3/3 in every baseline", which
  filters for luck rather than measuring reliability: a case at a true 86%
  shows 3/3 in one arm about 64% of the time, so it can enter the set and
  then be held to 5/5 for ever after. Five of the 27 members are in exactly
  that position — `factual-1` 86-90%, `self-correct-1` 87-92%, `agentic-3`
  89-91%, `regression-anchor-1` 91%, `agentic-2` 94% over the deduplicated
  production arms — and the gate inherits their variance. A candidate that
  changed nothing takes a clean PASS 1-3% of the time and is declared FAIL
  16-31%, depending on which arms the rates are estimated from. The record
  agrees: of the eight gate runs in `evals/results/*.log`, every one went to
  RECHECK first and three ended FAIL, two of those overturned by a control
  on unchanged code. The command changes no verdict and no membership — it
  prints each case's estimated rate, its chance of being rechecked and its
  chance of being called broken, so the set can be re-based on evidence.
  Pass it the arms the set was NOT chosen from, or the estimate inherits the
  same luck.

- A results file written by `run_chunk.py` records its temperature. Identity
  metadata decides whether two files may be compared at all, and this one was
  written by `run_suite_cli` but not by the chunk driver, so every file the
  chunk driver created from scratch — every control run — had a blank where
  the rule expects a value. Found while auditing a control whose temperature
  read `None` beside the arm's `0.1`; the two had in fact run identically,
  but nothing in the file said so. The fields are stamped in one place now
  (`run_chunk.seed_identity`), with a test that a chunk file carries what a
  whole-suite run carries and that a later chunk never rewrites the first
  chunk's identity.

- The gate set is pinned and measured instead of inferred. Membership was
  decided by "3/3 in every baseline", which is a filter on luck rather than a
  measurement: a case at a true 86 % is 3/3 in a three-run arm about 64 % of
  the time, so it entered the set on one good morning and was then held to
  5/5 for ever. Eight of the thirty members could not hold a perfect score on
  unchanged code. The set also depended on which baselines the operator
  passed — 27, 29, 31 or 32 cases for the pairs in use — so the same
  candidate could pass under one documented command and fail under another.
  `evals/gate_set.json` now states the membership and the evidence, and
  `gate.py --propose-set` rebuilds it from measured arms; a case qualifies
  only if it never missed. None of this reaches users: `evals/` is not in the
  wheel.

## 2.11.1 — 2026-09-14

A patch release: nothing model-facing and nothing the launcher hands the
server; code and documents nobody used are gone. Gate: every remaining
suite green (27 suites, 739 tests; 29 suites and 798 tests before), smoke on a fresh server
9/10 then 10/10 (the miss was factual-1, a no-tool knowledge answer that
missed once in every arm today), CI green on main and the tag.

- The prune. Nothing here was used: protocol v2 (`loop_v2.py`,
  `shell_session.py`, the v2 parser and prompt in `protocol_v2.py`, its
  suite, the `protocol` config key), which lost its A/B at 13/36 vs 22/35
  and doubled every safety and file-tool change; the local escalation
  ladder (no viable bigger model on this hardware) and the cloud
  escalation path (never configured), with their six config keys, so "no
  code leaves the machine" is now structural rather than a default; the
  memory dreaming daemon, off since it fabricated hardware facts; the
  unused brace scanner in the parser; the root `shellai.py` / `shellai.cmd`
  shims. The SEARCH/REPLACE applier that `edit_file` uses moved out of
  `protocol_v2.py` into `hexcli/editing.py` unchanged, with its tests in
  `evals/test_editing.py`. Internal documents (the V2 plan and roadmap, the
  levers memo, the backend study, ARCHITECTURE.md) and the study-only bench
  probes are no longer tracked; they live in git-ignored `docs/local/` and
  `tools/local/` (owner rule: only the paper and user-facing docs are
  committed). `tools/backend_bench/` keeps `stall_rate.py` and its two
  imports, which the release gate runs. About 3,300 lines and three suites
  gone; every remaining suite green.

## 2.11.0 — 2026-09-14

A minor release: tool result text the model reads changed (`verify_syntax`
reports its rung) and a new nudge was added. Gate: extended suite at 5 runs, seed 20260914, gate PASS after a 6-run recheck (five cases missed once, all 5/6 or better on recheck; run-level 156/205 vs 165/208, p=0.48); multi-turn at 3 runs on a fresh server with 0 invalid runs, no 3/3 case lost, uc3-t7 gained (38/48 vs 33/44, p=0.80); smoke 10/10; stall probe clean; CI green on main and the tag.

- Claims need evidence. A finish that says it ran something, that it
  works, that the buttons respond or that the tests pass, needs a run this
  turn (run_code or run_command); reading the file back proves the bytes,
  not the behaviour. The owner's 2026-09-13 calculator session made three
  such claims after read_file, with nothing ever run and nothing wired.
  One nudge names the claim and asks for a run or a plain statement that
  it was not run; a second unbacked claim goes out with a dim "Nothing was
  run this turn." under it. The tests-claim nudge is the same rule for one
  phrase; this is the class.
- `verify_syntax` is a ladder that reports the rung it reached. A page is
  parsed and cross-referenced: handlers the markup calls must be functions
  a script defines, ids the scripts look up must be elements the markup
  has, buttons must be wired to something, and a script after `</body>` is
  noted (the calculator: no handler on any button, an id no element had, a
  function nothing called). Python is parsed and cross-referenced for
  names it reads but never binds (a star import turns that off). JSON and
  PowerShell say "parsed". A file with no checker is `NOT CHECKED`, never
  `OK: skipped`, and says so, so the claim above cannot rest on it.
- Two eval cases: `claims-1`, the calculator prompt graded on the page
  being wired and the finish not claiming a run that never happened, and
  `claims-2`, a Python edit-and-claim variant. The 2026-09-13 page is a
  fixture.
- The eval runner waits for the inference slot after a client timeout.
  An abandoned request keeps the server's one slot until it finishes; the
  next run queued behind it, timed out too, and took a whole scenario
  down as invalid (uc3, 2026-09-13, twice). After a timed-out run the
  runner probes until the backend answers again, up to ten minutes, and
  the timeout no longer counts toward the abort streak once the slot is
  back.

## 2.10.0 — 2026-09-14

A minor release: the retry feedback the model reads changed. Gate: extended
suite at 5 runs, seed 20260914, 32/44 pass^5 (the 2.7.x baseline is 32/44), run-level
159/205 vs 165/208 (p=0.72); one gate case missed once and passed its 6-run
recheck; CI green on main and the tag.

- A reply whose first JSON object does not decode is never "finished" by a
  later object in the same reply. The owner's 2026-09-13 session: asked for
  a calculator page, the model answered with a `write_file` holding 1.6K of
  HTML and, behind it, a `finish` saying the file was created. Fifteen
  attribute quotes (`onclick=\"input('7')">`) were unescaped, the string
  closed early, the object failed to decode, the parser moved on to the
  next complete object, accepted the finish, and the turn claimed a file it
  never wrote; the next turn found nothing to open. `parse_json_object` now
  decodes the first object from the first brace with `raw_decode` (batched
  actions still take the first and let the loop drive the rest), repairs a
  stray quote inside a string value up to sixty-four times by escaping the
  last unescaped quote before the decoder's error (a truncated string is
  left alone), accepts raw control characters inside strings, and returns
  nothing when the first object still fails, so the loop's existing retry
  fires. That retry now tells the model the decoder's complaint, the
  character offset, the text around it and the quoting rule instead of
  "not valid JSON". The session's reply, verbatim, is a fixture: it decodes
  to the write with all 1,466 characters of HTML.
- The `/` command menu sits above the input row, between the top rule and
  the prompt. The box is pinned to the window's last rows, so the rows
  2.9.1 added below the input pushed the input row and the caret up
  whenever the menu appeared or changed height; rows above the input grow
  the box upward and the caret stays where it is.
- `run_arm.cmd` stops when the suite exits non-zero. An aborted suite (six
  consecutive backend timeouts on a starved machine, 2026-09-13) left the
  previous arm's results file in place, and the script copied it as the
  candidate and gated it, which read as a RECHECK verdict against stale
  data. It now logs the abort and writes no candidate.

## 2.9.1 — 2026-09-13

A patch release: the input line and the status bar; nothing model-facing
and nothing the launcher hands the server. Gate: CI green on main; smoke
10/10 on a fresh server.

- The status bar no longer flickers while a turn runs. Every spinner tick
  (12 a second) went through the full redraw: erase the box to the end of
  the screen, then rewrite its rows, with the cursor visible during the
  erase, so Windows Terminal could present the blank frame in between.
  `LiveArea.repaint` now overwrites the box in place when its row count is
  unchanged, each row clears its own line, and `_draw` emits one write with
  the cursor hidden throughout inside a DEC 2026 synchronized update
  (Terminal 1.24 presents the frame atomically; a console that does not
  know the sequence ignores it). The erase path, still used when the box
  grows or shrinks, joins the same frame.
- The input line previews the best slash-command match while a command
  name is typed: `/he` shows a dim `lp` after the caret, Right arrow
  accepts it with the trailing space a Tab completion adds, and the text
  submitted is only ever what was typed. The preview appears for a bare
  `/word` at the end of the line, never for arguments or paths (no
  filesystem walk per keystroke), and not once the name is complete. Ties
  go to the first match in `REPL_COMMANDS` order (`/c` previews `/clear`);
  Tab still lists the rest. The preview counts toward the row's width so a
  long name still wraps exactly; the caret arithmetic sees only the real
  text.
- Ctrl+Backspace deletes the word before the caret. A Windows console
  delivers it as DEL (0x7f), which the key map bound to a one-character
  backspace, so it ate one letter per press. Ctrl+Delete deletes the word
  after the caret, and Ctrl+Home / Ctrl+End jump to the start or end of a
  multi-line entry (Home / End stay on the current line), all through the
  extended scancodes the console already sends.
- Undo and redo on the input line: Ctrl+Z and Ctrl+Y. A run of typed
  characters up to a space is one step, so undo removes the last word; a
  kill, a paste, a completion or a history recall is one step each; cursor
  moves are none. A new edit after an undo drops the redo branch. Two
  hundred steps per entry, cleared when the line is submitted.
- A command menu under the input while a bare `/word` is typed: the
  matching commands in command order with their one-line descriptions
  (parsed from the `/help` text, so the two can never disagree; custom
  commands say so), the pick marked, eight rows with a "… n more" line
  past that. Up and Down move the pick, Tab takes it with the argument
  space, Enter runs it, Esc closes the menu with the line, and the dim
  preview after the caret follows the pick. Tab on an ambiguous command
  word therefore takes the pick now instead of stopping at the shared
  prefix; off the menu (config keys, paths) Tab still advances as far as
  certainty goes. The menu rows are part of the entry, so the box grows
  into the pad the way a multi-line entry does.
- `/clear` and `/new` on the live layout. The first fix printed the banner
  with the box up, and text written then is conversation: anchored above
  the box at the bottom, growing upward, so the banner appeared at the
  bottom. Both commands now take the box down without padding, clear,
  print, and pin the box again under what they printed. `/new` always
  prints the banner (a new session starts the way the program does);
  `/clear` prints it only if it was still on screen, which the live area
  tracks: set when the banner is printed, cleared when a write scrolls the
  window past its pad, when a growing entry scrolls it, or when the screen
  is cleared.

## 2.9.0 — 2026-09-13

A minor release: `edit_file` and `write_file` behave differently for the
model, and the launcher hands the agent its package path. Gate: extended
suite at 5 runs PASS after recheck, uc1 at 6 runs, smoke 10/10, CI green.

- `edit_file` merges the model's change into the file instead of pasting
  its memory of the file. Multi-turn traces (uc1-t3/t4, 2026-09-13) show
  the 4B rebuilding lines from the traceback in its history rather than
  copying them: `for item in data` where the file says `items`, `"average":
  average` where it says `avg`, a dropped trailing comment. Its
  `old_string` then sits at 40–95 % similarity while the change it wants is
  small and clear (`appned` → `append`; add a key to a dict). The old
  fallback pasted `new_string` over the closest region when similarity was
  ≥ 95 %, which installed the misremembered names: five of six live uc1-t4
  runs ended in `NameError: name 'average' is not defined`, put there by the
  harness, and the model then retried the same line four times. The paste
  is gone. `protocol_v2._apply_one` now runs a token-level three-way merge
  between the file's closest region, `old_string` and `new_string`: hunks
  the model changed are applied where the file agrees with what the model
  saw, tokens the model misremembered keep the file's text, and a hunk that
  touches a misremembered token refuses (unless the "change" merely restates
  what the file already says, which is skipped). Inserted lines are
  re-indented by the delta between the model's line and the file's; a
  whitespace run holding a newline never matches one that does not; a
  replacement that would restate the tokens beside its anchor, grow into a
  side the model did not see correctly, or carry JSON escapes into a file
  that has no backslashes, refuses. Every refusal still reports the closest
  region as before. Replaying the saved traces against the fixtures: of 20
  distinct failed uc1-t3 edits 5 now apply correctly, 15 still error, 0
  apply wrongly; of 23 non-exact uc1-t4 attempts 8 apply, 12 refuse, 0
  produce a harmful file (the first draft produced two syntax errors, from
  un-shifted indentation and literal `\"` sequences, both now refused or
  fixed).
- `edit_file` decodes a double-escaped `old_string`. 58 of 667 saved
  edit_file calls (38 of them errors) carried `\"` or literal `\n` because
  the model escaped its JSON arguments twice (agentic-3: `\"name\":
  \"demo\"` for a file holding `"name": "demo"`). When the raw string is
  absent from the file and the decoded one is present, both strings are
  decoded once and the tiers run again. `write_file` does the same for a
  body that is one line of literal `\n` sequences with no real line break
  (1 of 389 saved calls), and leaves any body with a real newline alone.
- Audit of the same bug classes elsewhere: protocol v2's `edit` action
  shares the applier and gains all of the above; the JSON action parser
  only tracks escapes to find brace boundaries and never re-escapes; tier 3
  (indent shift) replaces whole lines and has no trailing-segment case;
  `run_command` carried one escaped argument in 303 calls and it was a
  legitimate regex.
- The edit and write event lines name what landed a non-exact edit
  (`, transfer match`, `, unescaped match`, `, indent match`; `, unescaped`
  on a write) so an arm log shows which calls the fallbacks rescued; the
  model still reads `Edited <path>` / `Wrote <path>`.
- A checkout's launcher runs the checkout's code. `python -m hexcli.agent`
  resolves the package from the working directory, then site-packages; the
  Terminal profile starts in the home directory, so since the 2.8.0
  packaging the launcher started from `Hex CLI.cmd` was silently running
  the pip-installed copy (2.7.1 on the owner's machine on 2026-09-13,
  found when a new input-line feature did not appear) while the checkout
  sat unused. The launcher now prepends its own package's parent to the
  child's `PYTHONPATH` (`_agent_env`), a no-op for an installed copy.
- `run_recheck.cmd` documents that the case list must be quoted: cmd splits
  an unquoted comma list into arguments, so the first 2026-09-13 recheck ran
  one case and handed the second case's name to the gate as a baseline path.
- Live numbers (fresh server per arm, 2026-09-13, a day with under 1 GB
  free on the 16 GB machine). Smoke 10/10. uc1 at 6 runs: t1 6/6, t2 6/6,
  t3 5/6 (3/3 on 09-12, 15/24 pooled over earlier arms; the miss called no
  tool), t4 1/6 (0/3 in every baseline: the first pass ever, on a merged
  insertion beside the file's own `avg`), t5 0/6, t6 0/6 as before; the
  merge landed 13 edits across uc1/uc2 that would have errored. uc2 at 3
  runs: t2 2/3 (the model edited on an explain-only turn; both stray edits
  refused), the rest 3/3. uc3 at 3 runs, twice: every run invalid on both
  attempts, the client's 300 s timeout expiring while the server answered
  429 busy at ~3,200 tokens of context (the 09-12 baseline lost one of
  three the same way; no HTP hang in the server log), so uc3 has no valid
  measurement today. Extended suite at 5 runs, seed 20260913: 31/44 pass^5
  (32/44 on the 2.7.x baseline, 31/41 on 09-05), run-level 162/205 vs
  165/208, Fisher p=1.0; not one fallback tier fired in those 220 runs (the
  event-line markers show none), so the suite is a regression check only.
  Gate RECHECK on agentic-3 (4/5) and self-correct-1 (4/5); recheck at 6
  runs: self-correct-1 5/6, agentic-3 4/6 (two double-escaped runs, one
  rescued by the decode, one landing the model's own comma-less JSON, and
  one old_string hallucinated three times) → gate FAIL; a second agentic-3
  recheck on a fresh server 6/6 → gate PASS. Ceiling panel: +lint-1
  3/5→5/5, +trap-4 0/5→1/5, −regression-anchor-1 5/5→4/5 (the model wrote
  invalid JSON through an exact-match edit). Results:
  `merge_transfer_r5.json`, `multiturn_uc{1,2,3}_merge_*_20260913.json`,
  log `merge_transfer.log`; the earlier anchor-only tier's arm is
  `delta_transfer_r5.json` (gate PASS after recheck, 27/44, superseded).

## 2.8.1 — 2026-09-12

A patch release: nothing model-facing and nothing the launcher hands the
server changed. Gate: CI green on main; smoke 10/10 on a fresh
server.

- Installer repairs a Windows Terminal profile whose icon path 2.8.0
  invalidated. The icon moved into the package (`hexcli/assets/`) that
  release, but an existing "Hex CLI" profile kept pointing at the old
  `assets/hexcli.png`; Terminal silently falls back to its own logo on a
  missing file. `install.ps1` now rewrites a stale `hexcli.png` icon path
  in a user's own profile with a targeted text edit, leaving the rest of
  the profile untouched, and the fragment it writes already uses the
  package path.

- Releases carry the wheel and the source distribution and go to PyPI
  (`pip install hexcli`): `.github/workflows/publish.yml` runs when a
  release is published, builds both, attaches them and publishes through
  PyPI trusted publishing. 2.8.0's files were attached by hand.
- README rewritten shorter: the PyPI route sits in Install next to the
  installer, the sample transcript the clips made redundant is gone, the
  configuration table keeps the keys people change, memory and logs are a
  short section under Usage, and the clips use absolute links so they
  render on PyPI, in a two-by-two grid; the edit clip plays faster
  (23 s, was 32 s). Version and CI badges at the top.
- Paper and CLAUDE.md current through 2.8.0: the timeline row and the
  closing state paragraph record the package and the classifier finding,
  and the milestones table breaks across pages instead of running off
  one (it had been overflowing page 9).
- Paper numbers re-audited against the code and the latest results: the
  window figure shows the 3,696-token server budget and the ~810-token
  history it leaves instead of the disproven 2,600-token cliff; the
  headline is 32/44 at pass^5 (LATEST.md) and 35/48 multi-turn; the
  "9 s per new conversation" figure is retracted as the backend study
  found; a paragraph carries that study's numbers (15.5 tok/s, 0.8 s warm
  first token, polling-off power). ARCHITECTURE.md gets the same pass
  (template in prompts.py, 29 suites, 44 cases, a dated note on the
  pre-Rewind TTFT section) and the README's suite size reads 44.
- Paper restructured to six pages: the abstract states the v2.8.0 numbers
  and the thesis; the runtime lever (KV rewind, the context re-read) is
  its own section between results and negative results; the status
  section is one paragraph; the milestones table is one line per row; a
  positioning paragraph names what the report sits next to; three
  figures that duplicated prose (chip diagram, prompt anatomy, NPU/CPU
  bars) are gone and their numbers kept in the text. Every figure in the
  paper is unchanged from the audit above.

## 2.8.0 — 2026-09-12

A minor release by RELEASING.md's rule (the launcher moved and its
layout changed), with no change to the prompt or the loop since 2.7.1.
Gate: the 2.7.1 five-run extended arm stands (no model-facing change);
smoke 10/10 on the first fresh server; multi-turn 3 runs with 15 s think
time against `multiturn_r3_20260912`: uc3-t7 regained, uc1-t3 shown as
lost (1/3, then 2/3 on a uc1 re-run: its failures are three `edit_file`
calls whose `old_string` did not match, a path untouched since the
afternoon's 3/3, so it is recorded as the case's own flakiness and a
watch item, not a regression); stall probe 0 hangs in 20 (a first run
lost 3 of 20 attempts to the laptop entering standby mid-probe, with 0
hangs in the 17 it completed).

### Format-List is not Format-Volume

The command classifier's disk-format rule matched every `Format-` verb,
so the CPU/RAM cookbook queries (`... | Format-List`) asked for a
destructive-command confirmation, and in every unattended eval were
auto-denied. Found while recording the README clips. The rule now names
the formatters it means (`Format-Volume`, `Format-Disk`, `format X:`) and
leaves `Format-List`, `-Table`, `-Wide`, `-Custom` and `-Hex` alone.

### README clips

Four short recordings under `docs/gifs/` replace the sample transcript:
a streamed answer, a live-state command, an edit with diff, test run and
undo, and a destructive command that asks first.

### Installable package

`pip install .` now gives a working product, not a REPL without a
server: `hex` starts the NPU server and the REPL (the launcher moved into
the package as `hexcli/launcher.py`; `launcher.py` and `Hex CLI.cmd` in a
checkout are shims), `hexcli` is the REPL alone. Everything the app
writes lives in `~\.shellai` (`hexcli/paths.py`): the user config, the
runtime config the launcher writes, the embedding model, the server log,
the session history (migrated once from a checkout's `history.json`), a
downloaded npurun. A checkout keeps working, and its old file locations
are still found as fallbacks. The icon ships inside the package. The
DirectML and Ollama tiers, dead code since 2.6, are gone from the
launcher. The installer runs `pip install .`, points the shortcut and the
Terminal profile at the installed `hex`, and creates the data directory;
the doctor's fix hints name real commands. The source distribution holds
the package, the installer and the two documents worth reading; the eval
harness, research notes and paper stay in the repository.

## 2.7.1 — 2026-09-12

A release that exists to fix the previous one, so by RELEASING.md's own
words a gate was skipped: 2.7.0 was tagged on a green local run without
waiting for the remote CI run on `main`, which went red.

* The named-file guard read file names out of the request with a pattern
  that could not cross `~` or `:`. On the GitHub runner the temp folder is
  `C:\Users\RUNNER~1\...`, so a request naming an absolute path there
  yielded the fragment `1\AppData\...\app.py`, which does not exist, and
  the guard refused an edit to a file that was present
  (`test_escalation` red on CI, green locally where the path has neither
  character). Whole path tokens are captured now; drive letters, short
  names and `..` segments are pinned in the guard's tests.
* RELEASING.md: the tag waits for the remote CI run on `main` to be
  green; the runner's environment is not the development machine's.

## 2.7.0 — 2026-09-12

### Input box and status line

The REPL has the Claude Code layout: the transcript scrolls above, and the
last rows of the window are an input box with a status line under it:

```
──────────────────────────────────────────────────────────────────
> what is 2+2
──────────────────────────────────────────────────────────────────
⠹ thinking (Esc to cancel)   context ◔ 32%   npu 96%   mem 10.1/15.6 GB
```

New module `hexcli/statusbar.py`; `lineedit.py` gained chrome rows and an
idle repaint; `ui.py`, `repl.py`, `llm.py` and `agent.py` wire it in.

* **How it stays at the bottom.** No scroll region and no full-screen
  redraw, so scrollback is untouched. While the line editor is active it
  draws the box as its own chrome and repaints on a one-second idle tick
  when the text changes. Between reads a wrapper on stdout/stderr erases
  the box before every transcript write and redraws it after, borrowing the
  margin layer's column bookkeeping to put the cursor back; the spinner's
  ticks go through the same lock. The box is pinned to the window's last
  rows from the first prompt: the console's cursor position
  (`GetConsoleScreenBufferInfo`) says how many rows lie below the
  transcript, and that many blank rows are inserted at the top of the
  window until the conversation is long enough to reach the box on its
  own. Ctrl+L and a zoom redraw keep it there.
* **Metrics.** `npu` is Windows' own NPU counter: `GPU Engine` utilisation
  for the adapter DirectX does not list, the source Task Manager's NPU
  graph reads, through `pdh.dll` with ctypes. Measured on this machine:
  0 % idle, 90–97 % during decode, attributed to npurun's pid; the adapter
  LUID changes across boots, so it is discovered at start-up. `mem` is
  physical memory in use system-wide (`GlobalMemoryStatusEx`). Both are
  sampled once a second on a daemon thread that never writes to the
  terminal. `context` is the existing fill gauge; the working directory
  and branch sit at the right edge when they fit.
* **The spinner moved into the status line**, as did the tool announcement
  (`→ read_file`). The streaming path never had a spinner because it would
  have fought the streamed text for the row; with the bar up it gets one,
  so the step label shows before the first token arrives, and the
  transcript's `thinking...` line is dropped. The prompt is `> `; the
  `[model | cwd | gauge]` header line is gone, the bar carries both.
* **No more answer printed twice.** Every turn ended with a `── Result`
  box repeating the text that had just streamed. The box is now skipped
  when the final model call streamed exactly the message the turn returned
  (whitespace aside). It still appears when they differ: nothing streamed
  (mock backend, no tty, a dropped stream retried without streaming), the
  turn ended on a tool result or a harness message instead of a model
  message (`Done.`, step limit, loop stop, refusal), or the parser
  recovered a message from output the renderer could not follow.
* The `(~N tokens generated)` line after every answer is gone; the count
  lives in `/stats`. Hitting the step limit now says so without a number.
* Off a console (pipes, CI, `--raw`) or with `status_bar: false` the old
  inline prompt is used unchanged. Checked under a pseudo-console (ConPTY
  plus a VT emulator, scratch tooling, not committed): plain turn, tool
  turn, `/help`, paste, Esc. 14 offline tests in `evals/test_statusbar.py`
  and 3 in `test_lineedit.py`.

### Review pass over the day's changes

Two code reviews over the diff and a live edge-case pass, fixed together:

* A delegate sub-agent hitting its own five-step cap printed `⚠ Stopped`
  mid-turn and suppressed the turn's answer box; delegates no longer mark
  the turn. Each delegate call printed two `◆ delegate` cards; one now.
* `▸ [run] exit N` was glued onto command output that lacked a trailing
  newline. The status label now names the running tool while it runs, and
  the elapsed clock counts the whole turn rather than restarting each
  model call.
* Pad bookkeeping: a multi-row entry that scrolled the window, `/clear`,
  `/resume`, Ctrl+L and a window resize during a turn all left the pad's
  recorded position stale, so later line deletes could remove conversation
  rows instead of blank ones. The editor now reports how far the window
  scrolled, screen clears reset the pad, a stale pad is replaced rather
  than added to, and a change in window size resets it before the next
  draw. The erase sequence goes straight to the console so the margin
  layer never replays it inside a reflowed word.
* An input of exactly the usable width left a styled empty row under the
  echo. Wide characters (CJK) are measured in cells in the status line,
  the chrome and the editor's wrapping.
* Markdown: a closing fence with trailing spaces or CRLF now closes; bold
  and code spans continue over a soft line break and end at a blank line.
  A `**` followed by a space no longer opens bold (`next** x` is literal),
  so a stray marker cannot bold the rest of an answer.
* The editor's scroll report counted only the first row a growing entry
  pushed past the bottom; every later row is reported too, and the status
  bar redraws on a window-size change even when its text is unchanged.
* Resizing the window in Windows Terminal lost the banner: the terminal
  re-wraps every row at the new width, and the rows that no longer fit
  are dropped into its scrollback, out of the program's reach. A resize
  (or zoom) at the prompt now lays the screen out again: banner at the
  top, the conversation anchored above the box, blank rows between, with
  the banner scrolling off only when the conversation needs the room.
  Verified in a real Windows Terminal window at five sizes.
* The same during a turn: a resize while the model is thinking or the
  answer is streaming lays the screen out again and replays the turn's
  output so far (the question, tool cards, the partial answer), then
  streaming carries on at the new width. Verified live in Windows
  Terminal, resizing once before the first token and once mid-answer.

### Live tour of the whole flow (2026-09-12)

Every command and key driven in a real Windows Terminal window, with the
console buffer dumped after each step. Fixed what it turned up:

* A multi-row question (Shift+Enter, or one line wrapping) lost its first
  row after Enter once the screen was full. The box's cursor moves carry
  no newline, so a line-buffered stdout still held them when the editor
  asked the console where the cursor was; its anchor sat too high, the
  scroll it reported fell short, and a later pad delete took the echo's
  first row. Every geometry read now flushes first.
* A long question's echo broke mid-word ("a sem / aphore"); it now breaks
  at spaces, and the transcript redraw echoes it the same way.
* Clearing or shortening an entry that had grown left the box a row or
  two above the bottom with blank rows under it. The box drops back to
  the last rows.
* A growing entry scrolled the whole window, banner included, even while
  blank rows still sat under the banner. It now takes those rows first
  (the banner stays put) and gives them back when the entry shrinks; the
  window scrolls only once the rows are gone.

Seen and left alone: on a nonsense or one-word question the model
sometimes echoes its own prompt scaffolding ("Request: …", the workspace
line). That is the prompt's shape, which changes only through the A/B
gate, not the terminal.

### "Run the tests" is now enforced by the loop, not the prompt (2026-09-12)

Two model behaviours from the live tour were turned into eval cases and
taken through the A/B gate (`evals/results/persona/` has every log).

* `tests-claim-1`: "fix the median and run the tests". Baseline 0/5, the
  test file never executed, the answer said it would pass. A rule-14
  sentence made it worse in a new way: the 4B copied the prompt's own
  tool examples ("Get-Process | Sort CPU", "script.py") — 1/5. Replaced
  by a harness nudge: when the request asks to run tests and no run tool
  executed a test this turn, the finish is sent back once, naming the
  test file. With it the tests ran in 5/5 attempts (pass 1/5: the
  remaining misses are wrong fixes, reported honestly with the exit
  code). Gate on the nudge-only arm: PASS (self-correct-1 5/6 on
  re-check); 30/44 pass^k, 167/221 run-level; scoreboard updated.
* `missing-file-1/2`: "in missing.py change alpha to beta" when the file
  does not exist. Alone: 5/5 already. With a prior edit in the history
  (`missing-file-2`, the live shape): 1/5 — the model edits another file
  and reports success. A rule-11 sentence lifted it to 4/5 but broke
  agentic-3 (5/5 → 3/5, 4/6 on re-check): gate FAIL, reverted. The loop
  now guards it instead: when the request asks to change a file it
  names, that file is not there, and the model reaches for a different
  file (or creates the named one without being asked to), the call is
  refused with the reason and the directory listing, and the model is
  told to say the file was not found. missing-file-2 3/5 → 5/5. The
  first cut of the guard read "correct" out of "saved correctly" and
  refused agentic-1's create (0/5); whole-word intent matching and a
  create-intent exemption fixed that (5/5), with agentic-1's wording
  pinned in the guard's tests.
* A second loop step for the same request shape: when the tests ran and
  failed and the model finishes anyway, the failing output is sent back
  once with "fix, run again, report". tests-claim-1 1/5 → 3/5; the
  remaining misses are fixes the 4B cannot get right in two tries.
  Gate on the guard arm (with this nudge, which no gated case can
  trigger): 30/44 pass^k, run-level 176/220 vs 167/221 on the nudge arm.

### Release gate (RELEASING.md), run 2026-09-12

* Offline: ruff clean, 28 suites, 794 tests.
* Extended, 5 runs/case, fresh server, gate against
  `ask_rule_r5_20260905` + `persona_nudge_r5_20260912`: PASS after the
  six-run re-check (error-recovery-3 6/6, factual-1 6/6,
  regression-anchor-1 5/6); 30/44 pass^k, run-level 176/220.
* Multi-turn, 3 runs, think time 15 s, vs `multiturn_prewarm_off_20260905`:
  no case at 3/3 lost on valid runs. uc3-t7 showed as lost once because a
  server watchdog reply (empty `choices` at ~3.1K tokens) was graded as a
  model failure; the runner now marks that shape INVALID, and a uc3 rerun
  passed t7/t8/t9 on every valid run. One uc3 run in three hits that
  platform stall.
* Stall probe, 800 user tokens, n=20, AC: 1 hang (the allowed maximum).
* Smoke at 1 run/case: 8/10 cold, 9/10 warmed (each miss a different case
  that is 5/5 in the five-run arms), 10/10 on the third fresh server.

### An empty reply from the server is retried, not fatal

The fork's request watchdog (0.2.3) ends a stalled long-context request
with a 200 and an empty `choices` array. The non-streaming path raised on
that and the turn ended with "OpenAI-compatible backend returned no
choices."; it now hands back an empty reply, which the loop already
retries once (the streamed path behaved that way all along). The eval
runner classifies the same shape as a backend failure (INVALID run), as it
does 5xx and connection loss, so a server stall no longer reads as a model
regression: found at uc3-t7 (~3.1K tokens) in the 2.7.0 release gate.

### Walkthroughs by kind of user (2026-09-12)

The same live method, this time as the people who meet the tool: someone
scripting it, a developer in a repo, someone whose server died, someone
coming back to old sessions, a first-time reader of the docs.

* Scripting: a pipe or `hex "..." > file` got the spinner, the step
  label, the token counter and erase sequences on stderr, and the answer
  wrapped in blank lines on stdout. Off a terminal, nothing but the
  answer is printed.
* Developer: `/undo` restored files for the last turn only; the second
  `/undo` removed the exchange but left its file change. Every turn now
  keeps its own snapshots, so exchanges undo one by one. A tool error's
  first line no longer ends on a bare colon.
* Server died: the in-session restart started the server with a 2.47
  SDK default when the launcher had chosen 2.50; the launcher now hands
  the REPL the server's environment, and the fallback picks the newest
  install.
* Returning: "what can you do?" stored the whole help text as an answer
  and cost half the context; it now stores one line. Session titles
  keep the person's words ("What is 2+2", not "What Is 22").
* Docs: README's opening transcript shows today's screen, every
  command-line flag has a help line, and the scripting behaviour is
  written down.
* Installer, run in a fresh clone: it warned "architecture X64" from an
  x64 shell on an ARM64 machine (it checked the shell's architecture, not
  the machine's); it reported a 2.47 SDK from an old `QNN_SDK_ROOT` while
  the launcher uses 2.50 (same newest-wins rule now, with a note); it
  never fetched the MiniLM embedding model, so memory was silently off
  after a fresh install (downloaded now, ~23 MB); its summary said
  "Start Hex CLI from the Start Menu" with `-NoStartMenu`. The doctor
  checked the example config's port 8000 instead of the launcher's
  server, and passed an `ADSP_LIBRARY_PATH` that pointed at another
  SDK's libs; both fixed.
* Ctrl+C at `/memory clear` or in `/setup` printed `Cancelled.` twice;
  `/setup` over a pipe works again. The non-streaming spinner and the
  delegate token counter no longer print alongside the status line.
* The launcher's failure exits held a classic-console window open with a
  prompt so the message can be read; Windows Terminal keeps the pane.
* Uninstall removes the emptied Terminal fragment folder; one-shot mode
  skips the answer box after a stop like the REPL does; `/history` fits the
  window width; the banner tail shortens in a narrow window; the notice
  after `/clear` lines up with the others.

### The shortcut starts in the home folder

It used to start inside the Hex CLI checkout, so every session's first
message carried the repo's own `AGENTS.md` as project instructions, and
the model folded those working rules into unrelated answers ("what is an
NPU" came back with "the NPU server must be restarted fresh for each
evaluation" and the `REQUIRED_NPURUN` pin). The shortcut and the Terminal
profile now start in the user's home folder, like a shell; `/cwd <path>`
or launching from a project directory picks up that project's
instructions. The prompt itself is unchanged.

### Your messages get a light band

The echoed `> question` rows carry a subtle background band across the
usable width, the way Claude Code marks the user's turns, so turn
boundaries are easy to find while scanning. Continuation rows of a
multi-line message are banded too, and the transcript redraw after a
zoom or `/resume` uses the same style. `user_highlight: false` turns it
off; it is never applied without colour or outside the boxed layout.

### The conversation grows upward from the box

The conversation is anchored just above the input box, like a chat window:
your question stays where you typed it, the answer appears under it, and
every new line shifts what is above it up into the empty space under the
banner. The banner keeps the top of the window until that space is used
up, then scrolls away. Mechanically the blank rows that pin the box are
now consumed from their top with line deletes (`ESC[M`) as text arrives,
and inserted at the cursor row (`ESC[L`) when the box must be pinned
lower, so nothing above the pad ever moves. (An intermediate version
printed the question at the top under the banner; the owner wanted the
seamless bottom-anchored flow instead.) The editor writes its own rows
straight to the console so its cursor moves never count as transcript.

### A compact window

The shortcut opens Windows Terminal at 92 columns by 28 rows
(`wt.exe --size 92,28 -p "Hex CLI"`) instead of the default 120 by 30, and
the installer's profile fragment sets the look: One Half Dark, Cascadia
Mono 11, even padding, a bar cursor, slight transparency, and a title that
the shell cannot overwrite.

### Copy, glyphs and flow, end to end

Two audits over every string a person sees (about 120 findings) and the
full interaction flow, then one pass to fix them. Model-facing text is
untouched.

* **One banner.** The launcher used to print its own two-section header and
  name the model and engine four times before the REPL's banner. It is now
  quiet when the server is already up, shows a spinner only while starting
  it, and stops with the log path on failure instead of falling through to
  the unmaintained DirectML and Ollama tiers. The REPL banner reads
  `qwen3-4b-instruct-2507 on the Hexagon NPU · /help · press Esc to cancel`; the
  old `backend: openai` told NPU users they were on OpenAI.
* **`/help` regrouped** into Session, Status, Setup and Keys, wrapped to 76
  columns, Shift+Enter and Ctrl+L added, the DirectML/Ollama "NPU note"
  gone. `/tools` shortened to one line per tool.
* **One glyph set.** `◆` tool card (cyan, no more magenta), `▸` result line,
  `⚠` warning, `✓ ✗` only in the launcher and doctor. The delegate's `⟶`,
  the history list's `▶`, the launcher's `!` and the `[warn]` tag are gone.
  `$` command echo is dim with the command bold; command output is dim and
  ends with `▸ [run] exit N`, so evidence no longer looks like the answer.
* **Rhythm.** One blank line before a tool card, none after; one blank line
  before every answer (the first streamed byte adds it); the answer box
  lost its `── Result` title so streamed and boxed answers look alike; a
  turn that stopped on a loop or the step limit shows one `⚠ Stopped: …`
  line and no box of raw tool output; tool failures are one dim red
  `▸ error` line under the card instead of a red frame.
* **Confirms.** `⚠ The agent wants to …`, the command indented two spaces,
  `Allow? [y/N]`, then `Allowed.` or `Denied.` so the transcript records
  the outcome. The all-caps "unless YOU asked" aside is gone.
* **Notices** are two-space indented, dim, one past-tense sentence:
  `Chat history cleared.`, `Last exchange removed.`, `Memory cleared.`,
  `Cancelled.` everywhere (was also `Aborted.`), `Unknown command /hlep.
  Did you mean /help?`. The backend-failure flow reads `Model server error:
  HTTP 500.` then `Restart the model server? [Y/n]` and, after a restart,
  `Press Up, then Enter to resend.`; `python launcher.py` advice that only
  works from the repo became `Relaunch Hex CLI`. The HTTP 404 case no
  longer suggests `ollama pull` on the NPU path. `/stats` ends with the same
  block as `/context` instead of a second format of the same numbers.
  `/resume` reprints the conversation it reopened.
* **Status line** labels are `thinking`, `responding`, `▸ tool` with the
  turn's elapsed seconds; the cancel hint is in the banner only, so the
  location stays at the right edge during a turn. The empty input shows a
  dim `ask, or / for commands` hint.
* **Doctor, setup, update, uninstall** copy brought in line: no em-dash
  asides, `All checks passed.`, `N failed, M warnings.`, `Saved …`,
  `Cancelled. Nothing written.`; the wizard now reads through the same
  polled prompt as the confirms, so the box is lowered for its questions.
  Default session title is `New session`.

### Answers render their markdown

Streamed answers printed their markup raw: `## Heading`, `**bold**`,
backtick spans and ``` fences with the backticks showing. New module
`hexcli/markdown_stream.py` turns that subset into terminal styling one
character at a time, so streaming keeps its word-by-word feel: headings
bold with the hashes dropped, `- ` / `* ` bullets as `•`, `**bold**` bold,
`` `code` `` cyan, and a fence as a dim rule carrying the language with the
code left exactly as written (no gutter, so it copies cleanly). Markers are
held only while ambiguous and released literally otherwise, so `2 * 3` and
`C#` survive. Feeding the same text whole or per character gives identical
output; `evals/test_markdown_stream.py` pins that. The non-streamed
`Result` box goes through the same renderer, so both paths match.

### Shift+Enter, and two prompts that bypassed the console handling

* `Shift+Enter` inserts a new line in the entry (the key reader already
  peeks console events, so the modifier is visible; `msvcrt` alone would
  hand it over as Enter). `\` then Enter still works.
* `/memory clear` and the "restart the model server?" question used raw
  `input()`, whose echo goes through the console itself: the status box
  was left jumbled after Enter and the margin's column went stale. Both
  now use `ui.ask_line`, the same polled read the confirms use, with the
  box lowered for the question. A no-human answer (not a console, Ctrl-C,
  idle timeout) never restarts the server.
* The status box is up before the banner, so the first thing on screen is
  the finished layout.

### Status-bar flow polish

Six interaction issues found by driving the whole flow under a pseudo-console:

* **Resize no longer corrupts the box.** There was no resize handler, so
  changing the window size left the rules at mismatched widths and one row
  without its margin. A console window-size event (or a size change caught
  on the idle tick) now clears only the box's own rows, computed from how
  the terminal re-wrapped them at the new width, and redraws; the
  transcript above is left exactly as the terminal reflowed it. (A first
  version reprinted the saved chat instead, which made the banner and every
  notice vanish on resize.)
* A version that inserted the padding above the transcript, so the text
  hugged the box like a chat window, was tried and reverted the same day:
  the banner belongs at the top. The blank rows sit between the transcript
  and the box, and a burst of resize events from a window drag is handled
  once, at the final size.
* **One prompt during a confirmation.** A `y/N` confirm used to render on
  top of the still-live input box, so two prompts showed at once and it was
  unclear where to type. The box is taken down for the duration of any
  `confirm_*` read and restored after.
* **The banner fits narrow windows.** Below about 48 columns the fixed
  44-wide `HEX CLI` frame split into fragments; it now shrinks to the
  window, and drops to a plain heading when even that will not fit.
* **The status line keeps moving during a long tool run.** It repainted
  only on a spinner tick or a transcript write, so a quiet subprocess froze
  the numbers; the sampler now refreshes them once a second, and the
  repaint skips itself when nothing changed.
* **The label reads `responding` once tokens arrive**, not `thinking` for
  the whole answer.
* Redundant box repaints are skipped when the rendered box is unchanged.

### Windows Terminal by default

The Start Menu shortcut launched `conhost.exe` on purpose (2.2.0, for the
taskbar icon). Since 2.5.1 turned QuickEdit off there to stop clicks from
freezing output, the classic console has had no drag-to-select at all,
which is how the question "why can't I select text" came up. The installer
now registers a "Hex CLI" Windows Terminal profile as a fragment (never
touching the user's settings.json, and skipped when a profile of that name
already exists) and points the shortcut at `wt.exe -p "Hex CLI"` when
Windows Terminal is installed; conhost stays the fallback. Selection and
copy work in Windows Terminal whatever the console mode, the context gauge
draws its pie glyph there, and redraws of the status bar are smoother. The
uninstaller removes the fragment. Trade-off: the taskbar shows the
Terminal icon, not Hex's; the tab shows Hex's.

Selection did not work there at first either: the mode Hex sets (QuickEdit
off, mouse input still on, the console default) is exactly the one Windows
Terminal reads as "the application wants the mouse", so drags went to Hex
and only Shift+drag selected. `disable_quick_edit` and the launcher now
clear `ENABLE_MOUSE_INPUT` as well; Hex reads no mouse events.

### Releases, re-organised

Ten releases in 38 days, and the last five were set by the npurun fork's
binary and by same-day fixes: 2.5.0 fixed 2.4.0's silent empty replies seven
hours later, 2.6.1 changed 24 lines to carry fork 0.2.3, and 2.6.2 reverted
2.6.0's async init. Three structural causes, each fixed:

* **The fork has its own repository and releases.** Its source lived in one
  local clone, and the README's "build from `npurun/` here" could not be
  followed from a checkout. It is now
  [NathanL15/npurun](https://github.com/NathanL15/npurun), branch
  `hexcli-fork`, tagged v0.2.0–v0.2.3 with each version's binary on its
  release and a changelog that finally lists the July fork work. Hex CLI
  releases carry no asset from here on; `install.ps1` and `hexcli --update`
  download from the fork.
* **The required fork build is enforced.** `REQUIRED_NPURUN` in
  `launcher.py` (0.2.3). `--doctor` fails on an older build, the launcher
  warns at start-up, the installer replaces an older build instead of
  keeping it, `--update` skips the download when the binary is already
  current, and discovery prefers whichever candidate meets the version, so
  a stale cargo build cannot shadow a fresh download. Before this, a machine
  on 0.2.1 running 2.6.2 had polling on, no prime, no watchdog and the
  async-init override ignored, and nothing said so.
* **One version source.** `hexcli/__init__.py` holds `__version__`;
  `pyproject.toml` reads it and the README no longer restates it. The
  hand-edited copy in `agent.py` had stayed at 2.5.1 through 2.6.0–2.6.2:
  `--version` printed 2.5.1 and every chat log since 09-05 is stamped
  2.5.1. CI now runs on tag pushes and refuses a tag that does not match
  the version.
* **`RELEASING.md`** records the numbering rule and the gate. A change to
  the binary, the launcher's environment or the prompt runs the multiturn
  arm with think time and the 3K stall probe before it is tagged, because
  both regressions above lived where the extended suite does not look.

### The end-of-turn prewarm is already right (negative result)

A tail-aware, forced end-of-turn prewarm was built, unit-tested and measured
against the shipped one on Hex's real turn shape
(`tools/backend_bench/prewarm_tail_probe.py`): identical next-turn first
token (0.7–0.9 s) at every tail length, and prefilling the session history
into the prewarm costs a rebuild (8.7 s) because a prefix past Genie's
~3,100-token divergence ceiling cannot be matched. Reverted; the "20 % of
first calls pay a rebuild" figure in the 2.6.2 study was a harness artefact
(zero think time and single-message latency canaries), corrected in
`docs/backend_study/PROMPT_LEVER.md` §7 and the radar ceilings. Probe fix:
the bench prewarm calls were missing `/v1` and had been 404-ing silently.

## 2.6.2 — 2026-09-07

### NPU hangs at long context: async dialog init off by default

2.6.0 turned Genie's `allow-async-init` on for a 1.4 s faster dialog rebuild. A
dedicated probe (`tools/backend_bench/stall_rate.py`: Hex's prompt plus an
800-token tool result, about 3,065 input tokens, on AC power) hung 9 of 20
requests with it on and 1 of 25 with it off, 0 of 20 under the launcher's new
environment. The hang the 2.6.0/2.6.1 notes attributed to battery power is a
long-context failure that async init multiplies; host polling does not affect
it. The launcher now sets `NPURUN_HTP_ASYNC_INIT=0`; a dialog rebuild costs
4.3 s again instead of 2.9 s, cold start 5 s instead of 3.6 s. Smoke suite
20/20. `NPURUN_HTP_ASYNC_INIT=1` in the environment restores the old behaviour.

### The prompt-length lever, measured and closed

`docs/backend_study/PROMPT_LEVER.md`. The decode rate is stepped, not linear,
against context (~17 tok/s from 500 to 1,750 tokens, ~15 from 2,000 up), and
the prefix is cached, so a shorter system prompt buys no time at any size that
keeps the rules: the production prompt, a dedented copy (−124 tokens) and a
copy without the never-used delegate schema (−240) answered the same queries in
the same time to the hundredth of a second. Removing the delegate schema
degraded outputs. The radar's prompt-token and wall-time ceilings are corrected
and a "hangs per 100 requests at 3K context" axis added. New probes:
`decode_vs_context.py`, `prompt_latency_probe.py`, `stall_rate.py`,
`rewind_probe.py --ks` (the Rewind discard limit is exactly 600 tokens; 700
rebuilds).

## 2.6.1 — 2026-09-06

### A stalled NPU request now fails in a minute instead of six

On battery power the NPU occasionally fails a long-context request (see 2.6.0)
and the query did not return until Hex's own 300 s timeout. npurun 0.2.3 ends the
request when its 60 s watchdog fires: Hex sees an error and can retry, and the
stalled turn costs ~97 s instead of ~400 s. Requests that arrive while the
wedged query still holds the NPU get "busy" immediately. Requires the 0.2.3
`npurun-arm64.exe`.

## 2.6.0 — 2026-09-06

Requires the 0.2.2 `npurun-arm64.exe` from this release (the runtime overrides,
the forced prewarm and the async dialog init are all in the binary; the 0.2.1
binary ignores the new variables and simply behaves as before).

### The NPU server no longer spins three cores while idle

The Genie bundle ships `poll: true`, which makes npurun's host threads
busy-poll the NPU around the clock: 2.7 cores and +11 W with nobody typing,
+11 W while decoding, and the SoC at ~70 °C. hexcli-fork npurun 0.2.2 turns
host polling off by default (`NPURUN_HTP_POLL=0`, set explicitly by the
launcher). Measured on the X Elite, 2026-09-05, same bundle: idle 4.4 W
instead of 14.4 W, decode 19 tok/s instead of 15.5 at 9.8 W instead of
21 W (0.55 J per token instead of 1.44), SoC ~40 °C, and no loss under a
saturated CPU. Cost: first-token latency +0.1–0.3 s. Quality gate: extended
suite 62/82 with polling off vs 61/82 with it on, same seed, same evening;
smoke 19/20 twice. Set `NPURUN_HTP_POLL=1` before launching to get the old
behaviour back. Full study: `docs/backend_study/`.

### The first turn no longer pays the 2,300-token prefill

While the banner prints, Hex now hands the server its system prompt
(`/v1/npurun/prewarm` with `force`, npurun 0.2.2), so the first request
extends a warm cache: 0.85 s to first token instead of 4.2 s (or ~9 s when
the server still held another conversation). Measured on the way: Genie's
Rewind reuses a shared prefix and can drop at most ~600 cached tokens of
tail; the cache tops out near 3.3K tokens (extension fails at ~3.5K); with
`allow-async-init` a dialog rebuild costs 2.9 s instead of 4.3 s, which the
fork now turns on by default. A second, spare Genie dialog to hide rebuilds
entirely is not possible on 16 GB: the second context binary fails to load
(error 1007) once the first holds its 5.8 GB. Decode speed falls from 19 to
13.5 tok/s between an empty context and 2K tokens, so the length of the
system prompt itself is now the largest remaining latency lever.

### On battery: two hours back, and a hang that was always there

Unplugged, the resident server now idles at the machine's own 7.4 W (6.9 h)
instead of 10.5 W (4.8 h), and a four-turn conversation costs 190 mWh instead
of 242. The same session found that on battery, long-context requests
occasionally block for ~380 s with an NPU graph error; the untouched 0.2.1
configuration does it too (1 in 31 requests), so it is a platform issue on DC
power, not a 2.6 regression. Details and counts in
`docs/backend_study/CPU_VS_NPU.md` §13.

## 2.5.1 — 2026-09-04

### The "thinking… until Ctrl+C" freeze was the console, not the model

Sometimes, most often on the first message of a session, the answer never
appeared until Ctrl+C — and then it appeared all at once. Root cause, measured
in a window launched exactly like the Start Menu shortcut (`conhost.exe
cmd.exe /c "Hex CLI.cmd"`): classic conhost with QuickEdit on (the registry
default). A click inside the window — the click that focuses a freshly
opened window — starts a selection, the title turns to "Select …", and every
console write blocks until a key is pressed. The model kept generating and
the reader thread kept draining the socket; the main thread sat inside the
first `stdout.write`. Ctrl+C is "copy" while text is selected, so conhost
cleared the selection and released the writes without any interrupt
reaching Python — which is why no "Cancelled." ever printed and why every
one of those turns shows as `completed` in the chat log. The streaming client
itself was measured clean: 0 ms between `data: [DONE]` and return.

* The launcher's console setup and the REPL start both clear
  `ENABLE_QUICK_EDIT_MODE` on stdin (0x1f7 → 0x1b7, verified in a
  shortcut-launched window); the REPL restores the original mode at exit so
  a shared cmd window is not changed permanently. Windows Terminal ignores
  the flag.
* The streaming request now waits out a 429 + Retry-After like the keep-alive
  pool already did. The end-of-turn prewarm holds the inference slot for
  ~20 s (rebuild + prefill, server log 03:52:31→03:52:51), and a query typed
  inside that window failed outright with "HTTP Error 429" (chat log
  2026-09-04 03:52:50).

Also observed while diagnosing, not changed: the first request of a new
session diverges from whatever the server cached last; when that cache is
long the Rewind fails (4 s) and the dialog is recreated (12.7 s) before a
token is generated — 47 s for a two-message turn on 2026-09-03 23:43. The
prewarm only fires above 3,100 cached tokens, so a cache left just under the
threshold still pays this on the next session's first turn.

### Ask, don't give up — rule 12 now shows the question

Rule 12 already said "call finish with ONLY a clarifying question", and the
corrected grader showed the model mostly answering "Request was ambiguous.
Unable to proceed." instead. The rule now carries a worked example of the
finish call with the question in it, and states that saying the request is
ambiguous or that it cannot proceed is not a question. Screen at 5 runs:
ambiguous-1 1/3 → 5/5, ambiguous-3 1/3 → 5/5, ambiguous-2 ("Update the
file.") 0/3 → 0/5 — the model reads "the file" as a real target and acts.
Full suite at pass^5 on a fresh server (seed 20260905), gated against both
baselines: 27-case gate PASS after the recheck rule re-ran three cases that
missed once at five runs (error-recovery-2, factual-1, self-correct-1 — all
6/6). Run level 158/205 vs 93/123 (Fisher p = 0.79); ceiling panel gained
agentic-4, ambiguous-1, livestate-1, regression-knowledge-1 (all 5/5) and
ambiguous-3 (3/5), lost lint-1 (3/3 → 3/5) and trap-4 (1/3 → 0/5).
`ask_rule_r5_20260905.json` is the new first baseline for the gate.

One of error-recovery-2's five runs failed with "backend returned no
choices" — an empty reply, the Rewind artefact, surfacing as a model failure
instead of an invalid run; watch item. (The server also vanished at 11:30
during the run's final canary: a concurrent session had restarted it for a
CPU-vs-NPU benchmark, and the six-run recheck at 11:36 then shared that
server with the benchmark. The recheck cases were 6/6, so the verdict
stands, but the recheck's latencies are not clean numbers — one server per
arm is the rule for a reason.)

### Side padding, and Ctrl+Plus / Ctrl+Minus

* `side_padding` (default 2): stdout and stderr are wrapped so every row —
  printed, streamed token by token, spinner redraw, or the input line —
  starts `pad` columns in and ends `pad` columns short of the right edge.
  The wrapper does the wrapping itself at `width - 2*pad`: the first cut
  left the terminal to wrap long paragraphs, and its continuation rows
  came back at column 0 ("only the first sentence is indented"). Wrapping
  is word-aware even for streamed text — a row that fills mid-word erases
  the partial word (`ESC[nD ESC[K`) and reprints it on the next row; words
  over 30 cells break where they fall. The line editor uses the same
  usable width with explicit newlines, so the two never disagree. Verified
  in a shortcut-launched conhost window by reading the screen buffer back:
  120 columns, every row within 2–117, streamed and whole output identical.
  Spinner and live-render clears moved from 60 spaces to `ESC[K`, and the
  REPL enables VT processing itself (the launcher already did for the
  shortcut window).
* Ctrl+Plus / Ctrl+Minus at the prompt grow or shrink the classic console
  font by 2 px (8–40) and the size is remembered in
  `~/.shellai/console_font` for the next launch. Those chords produce no
  character, so `getwch` never saw them; the key reader now peeks the
  console input queue ahead of msvcrt, consumes the chord (and the bare
  Ctrl key-down that precedes it), and hands everything else on unchanged.
  Windows Terminal keeps its own zoom and never forwards the chord.
  The window keeps its size on screen: conhost keeps the cell count and
  grows the window when the font grows, so after the font change the
  column and row counts are refitted to the pixel size the window had
  before the first zoom (anchored once, so round trips land on the same
  cells). Then the conversation is cleared and reprinted through the
  margin layer — conhost's own reflow of old rows restarts continuation
  rows at column 0, which is the "loses its formatting" report. Measured:
  960×480 px stayed within a few pixels from font 16 through 20 and back.
* Ctrl+V pastes as one block. A classic console injects the clipboard as
  keystrokes; the reader used to take them one at a time, redrawing after
  each (the visible "typing"), and a carriage return with nothing queued
  behind it counted as Enter — a block ending in a newline sent itself.
  Now a burst of three or more queued keys is drained (20 ms grace for the
  console to finish injecting) and inserted whole: CR/CRLF become
  newlines, tabs four spaces, one trailing newline is dropped, and nothing
  submits until Enter. Verified by injecting `def f():\r\tpass\r` into the
  console input buffer: one `<paste>` token, `def f():\n    pass`.

### Eval instrument: significance in the repo, shuffled order, a binary gate

The eval council (2026-09-04, `/council` on "is the eval architecture
correct?") found the harness structurally sound — it measures the shipped
Rewind runtime — but its graders too lenient and its statistics too weak to
prove "no regression". Applied, in the council's order:

* `evals/stats.py`: Fisher exact (run level) and McNemar exact (paired
  pass^k) moved out of a scratchpad into the repo; `compare.py` prints them,
  with the reminder that p > 0.05 at 3 runs per case is absence of evidence.
  Re-run on the prompt-split arms it reproduces the 2026-08-31 verdict
  (91/117 vs 87/117, Fisher p = 0.646, McNemar p = 0.688).
* Every results payload now records what it was measured on: git SHA and
  dirty flag, npurun version, the server's advertised budget and window,
  QAIRT root, Rewind mode, the case-order seed, the model-call count, and a
  latency canary at the start and end of the run (`[SERVER-DRIFT]` finding
  when the end is 2× the start). Case order is shuffled under the seed —
  fixed order handed the last cases a tired server in every A/B.
* Graders: a bare `?` and "let me know" no longer count as asking ("Done.
  Let me know if you need anything else." passed); questions must be shaped
  at the user (wh-word or auxiliary + you/I, filename dots allowed); a message
  opening with "Done"/"Completed"/"All set" is a completion claim; the
  live-state CPU/GPU patterns are word-bounded (bare `arm` matched "warm");
  bigfile-1 must name something from the page it received and nothing it
  never read (`answer_grounded_in_tool_output`).
* `cases_multiturn.py --think-time SECONDS`: a pause between scenario turns
  like a person reading the answer. Zero, the old and only behaviour, fires
  the next turn instantly — the one situation the end-of-turn prewarm cannot
  help with, so every earlier multiturn number measured the prewarm as a
  cost.
* `evals/gate.py`: the ship gate is BINARY on the cases at 3/3 in every
  baseline given (27 of 41 across the v2.4 and v2.5 arms); one miss at 3
  runs is a recheck (6 runs, one miss allowed), the flaky rest is a ceiling
  panel that is tracked, never gated. `--scoreboard` writes
  `evals/results/LATEST.md` so the current number is findable. Applied
  retroactively with a single baseline, the v2.5.0 arm would have failed on
  three cases that were 3/3 in v2.4.0 — which is how weak "3/3 once" is as
  evidence of reliability, and why the gate wants two baselines.
* `evals/regrade.py` re-applies the current graders to saved traces, so a
  grader fix costs no NPU time — except where a grader reads tool output:
  saved traces cap it at 2,000 chars, and it refuses those cases (bigfile-1
  regraded 3/3 → 0/3 on a truncated page before that check existed).
* The canary carries a nonce and is sent twice with the faster kept. The
  first version measured the cache, not the server: a repeat of the
  preflight's text came back in 0.05 s, and the first request after a long
  transcript paid a 9 s divergent-Rewind rebuild.
* **The prewarm, measured with a person's pause (council step 3).** Two
  arms of `cases_multiturn.py --runs 3 --think-time 15 --seed 20260905`,
  fresh server each, prewarm ON (the shipped default) vs OFF
  (`prewarm_after_turn=false`). Quality: ON 31/48 turns, OFF 33/48,
  Fisher p = 0.83. First-response latency: mean ON 11.2 s, OFF 9.6 s; OFF
  faster on 9 of 16 turns, sign test p = 0.80, median difference −0.6 s —
  no evidence either way, and the mean gap is two turns. Where the
  rebuild finished inside the pause the next turn was 3–7 s faster (uc2-t6
  3.2 vs 8.0 s, uc3-t8-model 3.3 vs 10.8 s); where it did not, the turn
  waited on it (uc1-t4 25.0 vs 12.2 s, uc1-t6 32.9 vs 6.1 s). Server logs:
  ON 38 prewarms + 10 failed Rewinds + 49 dialog creations; OFF 34 failed
  Rewinds + 36 dialog creations. The rebuild costs ~20 s (13 s of it Genie
  dialog creation) and real pauses in the chat log run 11–77 s, median
  ~20 s, so it finishes about half the time. The default stays ON: the
  lever that would make it a clean win is a server that ABORTS an
  in-flight prewarm when a request arrives instead of making the request
  wait (roadmap watch item); flipping the default would be within noise.
  Results: `multiturn_prewarm_{on,off}_20260905.json` + server logs.
* What the corrected graders say (5 cases re-run live 2026-09-05, then
  regraded with the final patterns; `baseline_20260905.json` is
  `window_r3.json` with those five replaced): ambiguous-1/2/3 are 1/3, 0/3,
  1/3 — the model answers "Request was ambiguous. Unable to proceed."
  rather than asking, which the bare-`?` grader had scored as asking (2/3,
  0/3, 2/3); livestate-1 2/3 (one run named an Intel i7 without running a
  command); bigfile-1 3/3 under the grounded grader. "Ask, don't give up"
  is now a roadmap item.

Tests: 3 new in `evals/test_core.py` (busy-wait retry, deadline give-up,
QuickEdit cleared) + 3 (margin stream, word-aware wrapping incl. streamed
input, transcript redraw), 7 new in `evals/test_lineedit.py` (margin
wrap, auto-wrap unchanged at margin 0, ANSI-aware wrap, zoom tokens,
paste burst, short-burst replay, zoom anchor reset), 6 new in
`evals/test_runner.py` (clarification shapes, grounded answers, word-bounded
live-state, exact tests, gate + recheck rule, think time). 25 suites / 734
tests.

## 2.5.0 — 2026-09-02

### The context question, answered: the window was never the model's

The 250-token history floor came from two constants nobody had derived: a
2,600-token "degradation cliff" in the harness (V2_PLAN §14.7 records, the
same week it was written, that the collapse it described was a regex bug)
and a 3,000-token input cap in the server, inherited from upstream. The
bundle is compiled to 4,096. A cliff sweep at 3,000–3,700 input tokens
found quality flat all the way (12/18 at every size, the same three cases
failing each time); decode is ~12% slower only with the window actually
full.

Now: the server derives its input budget from the bundle's context size
(4,096 − a 400-token reply reserve = 3,696), keeps the user's request when
it must trim (it used to drop it first), caps generation at what the window
has left, and advertises the budget on `/v1/models`. The harness reads that
budget at the first turn and sizes history and tool pages against it.
History before auto-compact goes from 250 tokens to ~850; a first page of a
big file is now ~3,000 characters where the 4,096 assumption had let it
evict the question entirely (2.4.0 sized tool pages against the compiled
window while the server trimmed to 3,000 — bigfile-1 was passing with the
model never seeing the request).

| | 2.4.0 | 2.5.0 |
|---|---|---|
| multiturn ×3 (uc1–uc3, 16 turns) | 25/48 | **35/48** |
| uc2 everyday session, 6 turns | 7/18 | **18/18** |
| extended ×3 | 100/123 | 95/123 (parity, p=0.53) |
| empty model replies in the multiturn run | 63 of 117 | 0 |
| server trims (request evicted) per multiturn run | 141 at the old budget | 17 |
| dialog rebuilds per multiturn run | 44 at the old budget | 14 |

### Two bugs the single-turn suites could not see

- **Silent empties.** With the old floor, compaction rewrote history every
  two exchanges, and a KV-cache Rewind on a transcript that diverges
  mid-conversation can come back *successful with zero tokens* in ~0.5 s,
  then stay that way. 63 of 117 calls in a multi-turn run. That is 2.4.0 in
  a real session. The server now treats a Rewind that returns nothing like
  a failed one: rebuild and retry.
- **A payload that found the other door.** uc3-t9's calc.exe launch has been
  refused by run_code's workspace boundary since July; routed through
  `run_command` it was "caution" and ran. Absolute-path program launches,
  `Start-Process` and `cmd /c start` are now in the sensitive tier
  (confirm-gated; denied when non-interactive).

### End-of-turn prewarm

Measured limit of Genie 1.20's prefix matching: a request that diverges from
the cache (every new turn — history is condensed) works while the cache is
under ~3,150 tokens and costs a ~10 s rebuild above that. So the harness now
tells the server when a turn ends; if the cache is long, the server rebuilds
and re-prefills the system prompt in the background while you read the
answer. Next turn after a 3,400-token turn: 2.2 s to first token with the
prewarm, 10–12 s without. The client waits out the server's busy signal if
you type faster than that (the eval runner always does, which is why the
multiturn suite shows the prewarm as neutral: it has no think time).

### Full chat log

Every session now writes a complete transcript to `~/.shellai/chatlog/`
(one JSONL file per session): the version and npurun build, the server's
budget, the config in force (secrets redacted), every request as typed,
every message the model was sent, every raw reply with its latency and
retry index, every tool call with its full output, how each turn ended,
compactions and errors. Telemetry stayed a redacted summary; this is the
thing to read when a turn went wrong. `/stats` shows the current file;
`python tools/chatlog_report.py` summarises all sessions (versions,
tools, retries, empty replies, latencies, slowest and failed turns) and
`--last` replays the most recent one. Off with `chat_log_enabled false`.

### Context gauge in the prompt

The prompt header now ends with a small pie glyph and a percentage
(`[qwen3-4b | ~\proj (main) | ◔ 30%]`): how much of the history budget
this session has used. 0% on a fresh session, yellow from 75%, 100% (red)
means the next turn will auto-compact. `/stats` shows the same figure, and
`/context` shows just the numbers that decide it: history against the
budget, system prompt size, the server's per-call budget, compactions so
far, and how many more tokens until the next one.

Requires the 0.2.1 `npurun-arm64.exe` from this release (the budget, the
empty-Rewind guard and the prewarm endpoint live there); an older server
keeps 2.4.0's behaviour with a 3,000-token budget.

## 2.4.0 — 2026-09-02

### Every turn ~40% faster: the KV cache finally survives between calls

On QAIRT 2.50 the npurun fork (0.2.0) keeps the Genie dialog alive across
requests and sends every warm query as a prefix-matching `Rewind`, so the
2,355-token system prompt is prefilled once per process, not once per
step — and, because the prompt is now byte-identical across directories
and days (`prompt_stable_prefix`), even a brand-new conversation starts
warm. Two Genie 1.20 behaviours shaped the server: a reset after a large
prefill wedges the dialog (so it never resets), and an early-diverging
transcript poisons it (so it rebuilds the dialog in place, ~5 s, only on a
different system prompt). The launcher turns all of this on when it finds
QAIRT >= 2.50 and npurun >= 0.2.0, and leaves everything as before
otherwise.

| Extended suite, 3 runs/case | before | after |
|---|---|---|
| run-level pass | 91/117 | **97/117** (no regression, p=0.41) |
| first token, median | 6.8 s | **3.7 s** |
| agent step >= 2, median | 7.6 s | **3.2 s** |
| whole turn, mean | 16.0 s | **9.5 s** |

The no-tools direct stage is off in this configuration: with prefix reuse
a knowledge query on the agent path is already decode-bound, and a
different system prompt would cost a rebuild.

### Large tool results no longer break the step

The compiled window is 4,096 tokens. The server drops older messages to
fit but cannot drop part of the newest one, so a single tool result over
~1,800 tokens overflowed the window, the model returned an empty reply,
and the agent finished with the raw tool output — or generic babble — as
its answer. The configured limit allowed ~3,000 tokens; no eval case had
a tool output over 567 chars, so nothing ever saw it. Each tool result is
now sized to the room actually left in the window (`context_window_tokens`,
new config key), large reads come back as a line-aligned first page with
the exact offset to continue from, and an empty reply is retried like any
other invalid action.

| Live A/B, mechanism case bigfile-1 (×3) | budget off | budget on |
|---|---|---|
| Answer references the file | 0/3 | **2/3** |
| 16 other tool-using cases | — | no regression |

### The instrument could not see the v2.2 live-state win

`livestate-1` in the extended suite used the checker for "write me a
regex" questions, so every correct CPU answer failed — in every extended
run and both arms of every A/B since the case was added. Fixed
(`answer_matches` is the shared checker); count checks accept spelled-out
numbers. Re-graded live: livestate-1 0/3 → 3/3, agentic-5 2/3 → 3/3.

### The Split — codebase health

`agent.py` 3,818 → 1,499 lines across seven verified stages
(`parsing`, `http_client`, `cancel`, `tools`, `compaction`, `config`,
`repl`, `llm`); no module over 800 lines. Zero behaviour change by
construction, checked three ways: 24 suites / 699 tests, sentinel or
mutation probes on every moved-and-patched symbol, and a full pass^3
extended arm at statistical parity with the pre-split baseline (87/117
vs 91/117, p=0.65).

### Also

- `evals/run_chunk.py`: collect a suite arm in short chunks on one server
  (with `--set` overrides for A/B), for environments that kill long runs.
- `evals/cases_cliff.py`: the input-size sweep that closed the context
  question (quality flat to the runtime's ~2.9K input trim).
- The Rewind runtime is opt-in by presence: QAIRT 2.50+ under
  `C:\Qualcomm\AIStack` plus the 0.2.0 `npurun-arm64.exe` from this
  release. `/doctor` shows a "KV prefix reuse" line saying which it found.
  Full history of the spike (2.47 rejects Rewind, the SMART_MASK dead end,
  the 2.50 unlock) in docs/RESEARCH_NEXT_LEVERS.md §5–7.

## 2.3.0 — 2026-08-31

### One mode, 18 commands

Chat and command modes are gone, along with `/save`, `/load`,
`/checkpoints`, `/open`, `/profile`, `/model`, `/models`, `/mode`, and
`/context` (now the tail of `/stats`): −685 lines, no capability anyone
used. The app is the agent: REPL, one-shot, pipe. Status messages were
rewritten to terminal-tool voice ("Chat history cleared."), and `/clear`
now actually clears — screen and context — with `/new` keeping the
scrollback.

### Auto-compact stops thrashing

At the 250-token history floor, auto-compact re-fired every message and
crushed its own previous summary into a single stub each pass. The
deterministic compactor is now merge-aware (idempotent on its own output)
and auto-compact dry-runs it first, firing only when ≥100 tokens would
actually be freed.

### The context question, closed

A dedicated sweep (`evals/cases_cliff.py`) ran the production loop at
controlled input sizes: quality is **flat** from 2,370 to 2,973 measured
input tokens, and the runtime silently trims anything above ~3K — the
shipping config already sits at that ceiling. A two-stage prompt-split A/B
(extended suite, fresh server per arm) kept one stage and rejected the
other:

| Prompt-split A/B (extended ×3) | baseline | split |
|---|---|---|
| Run-level | 91/117 (77.8%) | 87/117 (74.4%), p=0.65 |
| Knowledge-query first token (median) | 10.1 s | **6.0 s** |

The **direct stage** ships on by default (`prompt_split`): pure-knowledge
queries get a small no-tools prompt, tool restraint becomes structural,
and first-token latency on those turns drops 40%. The **continuation
stage** (leaner prompt for steps ≥ 2) was rejected: edit anchors
degenerated under the changed prompt — the trimming experiment's
degradation fingerprint, now reproduced at every loop depth. Conclusion,
recorded in the paper: the 250-token history floor is a property of the
model and the 4K bundle, not a harness gap.

### Fixes

- Visible caret while typing (the line editor hid the hardware cursor for
  the whole read; now only per-repaint).
- Real Hex taskbar icon: the Start Menu shortcut launches through classic
  conhost (Windows Terminal has no per-profile taskbar icon), and the
  launcher sets the window icon at startup — from `main()`, not import,
  after a test run re-badged the developer's own terminal.
- A failed request can no longer poison the cached keep-alive connection:
  reconnect covers `ResponseNotReady`, so "restart the model server" is
  followed by a working retry.

## 2.2.0 — 2026-08-16

The everyday-correctness release. Two wild failures — "what cpu do i have"
answered with a confabulated Intel chip on a Snapdragon machine, and one
salary division wrong five different ways — triggered a systematic study of
the prompts a normal user types in their first five minutes, instead of
case-by-case patching. Four experiment arms, fresh server per arm, n=5
triage on every moved case.

| Everyday sweep (30 cases, n=3) | before | after |
|---|---|---|
| Live machine-state questions | 16/36 | **26/36** |
| All categories | 58/90 | **69/90** |
| Trap resistance (guard) | 10/20 | 9/20 (held) |

### The command cookbook (rule 9)

The dominant live-state failure was not the model refusing to run commands —
it was not *knowing* the Windows commands: invented cmdlets (`Get-CPU`,
`Get-CimComputer`), "what cpu" misread as CPU *usage*, registry fallbacks
that collide with the sensitive-path tier. Rule 9 now carries exact
known-good queries (CIM classes for CPU/GPU/RAM/cores, `Get-PSDrive`,
`Get-Date`, `$env:` names) plus a scope sentence keeping the
never-use-a-tool-just-because-it-was-named rule in charge everywhere else.
That sentence is load-bearing: without it, trap resistance collapsed to
4/20. Prompt cost: ~+200 tokens, spent knowingly.

### Memory dreaming off by default

The deepest root cause was not the model at all. The background "dreaming"
consolidation daemon had distilled the model's own confabulations into
`memory_rules.md` as fabricated machine facts (wrong CPU, wrong RAM, an
invented temperature), re-appending the identical batch every idle cycle —
then injecting them into every turn as "Prior knowledge", which the model
trusted over running a command. A self-reinforcing hallucination loop that
survived every prompt improvement. `memory_dreaming` now defaults to false;
the roadmap had already ruled the daemon ships only with a quality eval it
passes, and it now has one it failed. Hand-written memory rules still work.

### Measured and rejected, continued

- **Routing calendar math to `run_code`** — the tool takes a file path, not
  inline code, so the model correctly refuses; inline-code support is now a
  roadmap item. Days-until/weekday arithmetic stays a documented ceiling.
- **The arithmetic "failure class" itself** — mostly an artifact of a
  degraded 27-hour-old server; on a fresh server, everyday arithmetic is
  9/10 at 3/3. The measurement trap struck the diagnosis itself; server
  freshness now has a written protocol note.

Also: `evals/cases_everyday.py` joins the live suites (30 common prompts
graded against computed machine truth), and the roadmap's phases are named
by content instead of version numbers, so release tags and plan phases can
never collide again.

## 2.1.0 — 2026-08-14

v2.0 was a loop that worked and a product almost nobody could install. v2.1
is the packaging-and-shell release: getting Hex CLI onto a second machine,
and making it scriptable once there. The agent loop and the tuned prompt are
untouched — the live smoke gate ran 10/10 before and after.

| | v2.0 | v2.1 |
|---|---|---|
| Install | clone, build Rust, hand-set env vars, read the README | `.\install.ps1` |
| Offline tests (CI) | 619 / 19 suites | **685 / 22 suites** |

### Getting it installed

- **`install.ps1` covers the whole ritual**: ARM64 and Python checks, pip
  deps, QAIRT SDK discovery, npurun, the model pull, config scaffold, Start
  Menu shortcut, and a closing `--doctor`. Every step skips work already
  done, so the intended flow is: run it, fix the one thing it flags, run it
  again. The SDK download stays manual — Qualcomm's licence forbids
  redistribution — so the installer prints exact instructions and picks the
  SDK up on the next run.
- **A prebuilt `npurun-arm64.exe` ships as a release asset** (the vendored
  fork; MIT/Apache-2.0), so a new machine no longer needs Rust, LLVM, and the
  MSVC ARM64 toolchain just to get a working agent.
- SDK discovery is shared logic in two languages, and **compares versions
  numerically** — `QAIRT_2.9.0` sorts above `QAIRT_2.47.0` as text, which
  would silently bind a stale SDK.

### Product shell

- **Piped stdin**: `git diff | hexcli "review this"` attaches the pipe as
  data beneath the task; `echo "task" | hexcli` makes the pipe the task.
  Bounded with head+tail sampling and a chunked read, so a huge pipe costs
  O(cap) memory rather than buffering the file.
- **Custom slash commands**: any `.md` in `.shellai/commands/` (project) or
  `~/.shellai/commands/` (global) becomes `/<name>`, with `$ARGUMENTS`
  substitution. Project files beat global ones; built-ins beat both, enforced
  structurally rather than by convention.
- **`/search <text>`** across saved sessions, with match highlighting. Hits
  carry the same numbers `/history` shows and `/resume` takes.
- **`/setup`**, an interactive wizard for the safety, network, and UI
  settings. It persists (`/config` is session-only) and writes *only* the
  keys it asked about, so a config file never fills with pinned defaults.

### Fixes

- **Consent prompts can no longer stall an unattended run.** A detached eval
  once hung 7.5 hours on one confirmation: `isatty()` reports True for a
  hidden console, and a daemon-thread timeout cannot fire because the Windows
  console read holds the GIL. All consent prompts now poll for keys against a
  deadline. Unanswered means denied.
- Ctrl-C at a consent prompt **denies** rather than aborting the turn — the
  deny path is what writes the audit log's `blocked` entry and preserves the
  turn's undo snapshots.
- That deadline is an **idle** timeout, so an attended user reading a
  proposed command is never cut off mid-answer.
- Bare `/save`, `/load`, and `/model` matched only their `<cmd> <arg>` forms
  and fell through to the custom-command lookup.
- `autopilot_system_prompt` replaced the tuned prompt silently; it now warns.
- The installer reported failed `pip` installs as success (a `try/catch`
  around a native command never fires), and died outright on Windows
  PowerShell 5.1 when probing the default WindowsApps `python3` stub.
- `/setup` answers were reverted by the launcher's config regeneration.
- Two eval-grader loopholes: hallucinated completions ("I fixed it. You would
  need to restart.") and bare give-ups were scoring as clarification
  requests.

## 2.0.0 — 2026-07-31

v2 was a harness rebuild around a fixed model, driven by a measurement
instrument built first. Headline movement:

| | v1.7 | v2.0 |
|---|---|---|
| Extended suite (pass^5) | 22/35 | **24/36** |
| Multiturn turn-runs | 23/45 | **26/45** |
| Injection payloads executed | 9 of 9 | **0 of 9** |
| Offline tests (CI) | 330 / 8 suites | **619 / 19 suites** |
| First-token latency, smoke mean | ~9–10s | ~7–8s |

### The instrument (evals v2)

- Live evals now drive the **production `run_autopilot`** — one code path,
  graded on **filesystem state and answer content**, never string matches,
  with pass@k / pass^k over ≥3–5 runs and Wilson intervals.
- **Backend failures are not model failures**: a degraded NPU server marks
  runs invalid, not failed. The Genie dialog silently degrades after 1–2 h of
  traffic and perfectly impersonates a model regression; the runner detects
  it, and suites restart the server.
- `--set KEY=VALUE` on every runner: A/B any config key, override recorded in
  the saved results.

### Agent loop

- **Fuzzy edit apply, 4 tiers**: exact → trailing-whitespace → indent-shift →
  ≥95 % unique closest match. Ambiguity is always an error; a miss reports the
  nearest region with line numbers. (The #1 v1 failure class was edit
  formatting, not model capability.)
- **First-complete-JSON parsing**: the old greedy `\{.*\}` regex discarded
  batched multi-action responses wholesale — the actual cause of the
  multiturn collapse, not context length.
- **Unconditional retry-with-feedback** on malformed actions, at any step,
  with feedback naming the defect (unknown action / wrong shape / not JSON).
  Pure prose remains an implicit finish.
- **Fuzzy loop detection**: trips on repeated failures of the same call even
  when the error text varies; distinct targets never trip.
- **Verification-gated finish**: an unverified file mutation deflects the
  first "done" and asks the agent to check its work.
- **Deterministic auto-compaction** derived from the measured prompt size and
  a **calibrated token estimator** (EMA of real chars-per-token from exact
  completion counts) — the old chars/4-plus-stale-constant scheme fired
  compaction *past* the model's degradation cliff.
- `read_file` pages with offset/limit; tool output truncates head+tail;
  in-place NPU server restart on backend failure.
- Escalation ladder (loop trips, ignored verification, prose-instead-of-edit
  → consult a second local model) — shipped but **off by default**: no viable
  bigger local model exists on 16 GB (see "Measured and rejected").

### Safety (assumes the model is 100 % injectable)

- **Sensitive tier** ranked above `safe`: ssh/gpg/aws keys, hosts file,
  registry hives, credential vaults, DPAPI, `-EncodedCommand`. Confirm when
  interactive, **deny when not**. Live injection suite: 0/9 → 9/9 blocked.
- **Workspace write-scoping** behind a single `guard_mutation` gate; every
  mutating entry point in both protocols is enumerated by a test that drives
  it at an out-of-scope path and at a key path.
- **Network deny-by-default**: `fetch_url` (the only outbound channel)
  confirms per fetch, refuses when non-interactive; `network_access: "deny"`
  removes the tool and its schema entirely.
- Refusal messages never name an alternative route — a measured injection
  followed the old refusal's own hint straight to the bypass.

### Product shell

- **Rich input line** (pure stdlib): persistent history with prefix search,
  Tab completion for commands / config keys / paths, word-wise editing,
  multi-line paste as one message.
- Live streaming render; diff after every mutation + `/diff`; `/stats`;
  `/doctor` and `--doctor`; `AGENTS.md` project instructions; did-you-mean
  for slash commands; corrupt history quarantined instead of bricking launch;
  process-tree cancellation on Esc.
- `shellai.example.json` is now **generated** (`tools/gen_example_config.py`)
  with a staleness test — the hand-maintained copy once shipped a key that
  silently replaced the entire tuned prompt.

### Measured and rejected (deliberately not in v2)

- **v2 `<action>` protocol as default** — lost its A/B 13/36 vs 22/35; kept
  behind `protocol: "v2"` as an experiment harness.
- **Qwen3's native `<tool_call>` template** — the w4a16 bundle's detokenizer
  garbles its own special token.
- **8K context bundle** — compiled via AI Hub, benched 6 tok/s vs 15, no
  quality gain; compaction already keeps sessions under the cliff.
- **Qwen3-8B escalation** — 0.9 tok/s on 16 GB (CPU fallback).
- **Prompt trimming / tool consolidation** — the 14 rules are 73 % of the
  prompt; omitting even provably-irrelevant ones cost trap resistance
  (5/8 → 3/18, p≈0.017). The model is specialised to this exact prompt.
  Ships as `conditional_rules`, default off.
- **Thinking-2507** — unreachable: no Genie bundle exists, and AIMET
  self-quantization needs Linux + ~40 GB RAM this machine does not have.

### npurun fork (vendored)

Usage reporting (one chunk per token — exact completion counts), token-precise
`max_tokens`, mid-stream stop sequences, and a UTF-8 char-boundary crash fix
(a multibyte character at a stop-sequence boundary aborted the whole server).

### Upgrading from 1.x

Config is backward compatible; new keys (`network_access`,
`conditional_rules`, `rich_input`, `workspace_write_scope`, …) all default to
the documented behaviour above. `/clear` now clears the screen (it was an
alias for `/new`). The `last_observation` session field is gone. Old
`evals/harness.py` / `extended.py` / `multiturn.py` are superseded by
`evals/cases_*.py` and kept for reference only.

## 1.0.0 – 1.7.0

Pre-changelog history; see the git tags and `ARCHITECTURE.md` for the v1
design and its audit.
