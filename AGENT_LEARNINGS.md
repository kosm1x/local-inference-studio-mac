# Agent learnings — local-inference-studio-mac

## Standing rules
- A gate over someone else's command line is an ALLOW-LIST, never a deny-list. Two deny-lists lost on day one: llama-server rewrites `_` to `-` in long flags, so every hyphenated refused flag passed spelled with underscores; and the Claude Code client grew flags (`--bare`, `--safe-mode`, `--plugin-url`) that the launcher's refused set did not name.
- Re-read `llama-server --help` and `claude --help` after every upgrade of either; both allow-lists are pinned to the versions named in the scripts.
- Anything not run on this machine is UNVERIFIED. A bench number without a saved log is UNVERIFIED; tee every run.
- Discard the first (cold) request before taking a median.
- Judge fit against memory actually free at start, not the headline 32 GB.
- No model download before the operator picks; model payloads are never committed.
- Every built change gets an independent audit before the commit; the auditor gets the spec and the code, not the builder's conclusions.
- Scripts and any text with quotes or backslashes go through the Write tool, never a Bash heredoc or inline `python -c` (the Bash tool mangles escapes; hit again on 2026-11-10).
- macOS ships bash 3.2 and BSD tools: no `timeout`, no GNU `sed`/`stat`/`date` extensions, no associative arrays.
- macOS `ps -o comm=` follows argv[0]; it is a name check, not an identity check.
- `/tmp` and `/var` are system links to `/private/...`; a real-path check must accept exactly those and nothing planted.
- Every added line passes the identity grep before commit (no user name, home path, host name, session URL). This repo is local only until the operator says otherwise.
- Kill rules: one validated numeric pid, quoted, TERM. Never a group, a broadcast, a pattern or a computed list. Signal and trap tests run in a throwaway container, not on the host.

## 2026-11-10 — first build (server half, client half, two audits)
- **Mistake:** the first server brief said "port the refusal list exactly"; the port was faithful and inherited the reference's hole. -> When porting a security list, ask the builder to attack it against the real binary's parser, not only to copy it.
- **Mistake:** inline Python inside a Bash command to list model files failed on escaped quotes. -> Write the script to the scratchpad and run it.
- **Better:** two builders on disjoint file sets with a fixed interface (`config/lis.conf` keys, `serve.sh --detach`, `logs/api-key`) finished in parallel without a conflict.
- **Better:** both auditors reproduced their findings against the installed binaries (`llama-server ... --version`, `security list-keych`) instead of reasoning from the docs.
- **Mistake:** the client rework brief asked one agent for guard deny rules plus long lists of hostile test inputs; a safety classifier stopped the response and the round produced nothing. -> Split launcher hardening from guard-rule work, keep guard changes narrow, and let the operator decide who writes them.
- **Avoid:** caching a check's result inside `$(...)`: the subshell throws the cache away (a 0.3 s group-membership check became 2.7 s per run).
- **Better:** a process-identity check on macOS compares the pid's first `txt` entry from `lsof` with the expected real binary path; `exec -a` and symlink spoofs both fail it.
- **Mistake:** `--detach` cleaned up its pid file and lock from an EXIT trap while the child kept running, so an interrupted start left a server stop.sh could not see. -> For every cleanup trap, ask what is still alive when it fires and who can stop it afterwards.
- **Mistake:** the launcher scrubbed the environment after it had already called the server start. -> Scrub first; every later step, helpers included, inherits what is left.
- **Avoid:** a "stale" judgement and the identity it is checked against being read at different moments; capture the identity before the liveness check.
- **Better:** the re-audit was given the builders' claims as numbered items to disprove against the real binaries; it confirmed 58 flag arities and still found the one defect the builders' own tests could not see.
- **Avoid:** timing assertions at 200 KB when production caps input at 32 KB; they flake under load and prove nothing about the real path.

## 2026-11-10 — machine cleanup before the model download, session wrap
- **Mistake:** a mutation-test command started by an audit agent kept running after the agent reported; it sat blocked for over two hours in a scratch copy and the operator saw it as a live agent. -> Every brief that starts background or mutation runs ends with "list what you left running and stop it"; the orchestrator checks `ps` before reporting idle.
- **Avoid:** judging a model fit before the machine is ready: at survey time swap held 10.8 GB, 232 MB of RAM was unused and Low Power Mode was on. A first bench in that state would have measured the laptop, not the model.
- **Better:** pre-flight before any serve or bench: uptime, swap, free memory, Low Power Mode off, browser closed; record the numbers next to the bench log.

## 2026-11-14 — first live run and benches (llama-server 1.0.0, both models)
- **Avoid:** `--reasoning off` as a default for every model. It is right for the Qwen3.8-27B file (20/20 tools) and wrong for the Qwen3.6-35B-A3B file (14/20, the misses are invented answers with stop_reason end_turn; 20/20 with `--reasoning auto`).
- **Better:** when the bench reports `no_call`, replay those prompts and read the reply text before blaming the template; a fabricated answer points at the model's thinking setting, a tool call printed as text points at the template.
- **Better:** settings that differ by model (KV type, sampling, reasoning, chat template) belong in the manifest entry, so `MODEL_ID` is the only line to change.
- **Mistake:** the 2026-11-10 tier quoted 14-20 tok/s decode for the MoE file; measured 37.88. The prefill estimate held (264.6 tok/s). Estimates in docs stay marked as estimates until a tee'd bench log exists.
- **Mistake:** wrote "20/20 with `--reasoning auto`" from an ad-hoc probe; the saved bench run (`bench/results/20261114-164939-*`) scored 19/20, the miss being a 136 s thinking runaway that hit max_tokens → a result goes into a doc only with the log file that holds it. Combined: 39 of 40 with thinking on, 14 of 20 off.
- **Mistake:** smoke and bench passed on the GGUF's embedded template, so it was made the default; the first real Claude Code session then got HTTP 500 "System message must be at the beginning." (Claude Code sends a later system message). The reference repo documents this exact failure for its own model → smoke must include a `[system, user, system]` request, and a model is not "served" until one real `claude-local.sh` session has run.
- **Mistake:** started a headless `claude-local.sh -p` session in the background and waited for its exit; the session ran 47 minutes, the last 45 of them (23:12:18Z to 23:57:56Z) looping on one denied command (219 identical `python3 -c` calls in a row, each refused by lis-guard) → give every unattended session a wall-clock bound and look at its transcript after a few minutes, not at the end.
- **Avoid:** assuming the guards bound a repeat. Diagnosis (read-only agent, reproduced offline, same result on the reference's hooks at cafc34b): all three ran as written. lis-guard's W-23 suffix appeared from the 3rd deny ("denied 3 times" up to "denied 219 times") and loop-guard denied from the 4th call (216 denies, its reason never shown: Claude Code surfaces one deny reason), but every escalation is text only and the model's thinking was byte-identical each turn. repeat-guard only compares text blocks and the turns were thinking + tool_use. Nothing ends the turn or the session, and at the time the launcher refused `--max-turns` (it now accepts it and adds a default of 60 in print mode). Design gap shared with the reference, not a port gap.
- **Mistake:** told the user the refusal message was identical on every call and the count was 221, from a digest that cut each result at 200 characters → read the full record before describing what a guard said.
- **Avoid:** quoting one decode figure for this model. Measured in that session on tier S (f16 KV, `--swa-full`): 37.5 tok/s near empty context, 31.9 at 11K, 21.2 at 28K, 13.9 at 50K, 9.9 at 71K tokens. The bench's 256-token run on a 200-token prompt only shows the top of that curve.
- **Better:** in print mode nobody can answer a permission prompt, so `python3 -m unittest` came back "This command requires approval" and the model never ran a test, although its code fix was right after 76 s. A headless run needs the test command pre-approved, or the session must be interactive.
- **Mistake:** committed 4d43030 after running only serve_test and stop_test; adding `scripts/tests/chat_template_check.py` turned `lis-guard_test.py` red (3518/3520: its repo-script deny table lists every script lis-guard refuses, and the new file was not in it) and a later agent found it → run all eight test files before every commit, not only the ones for the files touched; a new file under `scripts/` changes the guard's tables.

## 2026-11-14 — audit fixes (docs counts, gitignore, chat_template_check hardening)
- **Mistake:** the audit finding said urllib honours `all_proxy` for http; on Python 3.13 it does not (a fake loopback proxy got the request with `http_proxy` and `HTTP_PROXY`, nothing with `all_proxy`) → prove a leak path with a fake listener that records what it receives, not from the variable list.
- **Avoid:** proving "no proxy used" with `http_proxy=http://127.0.0.1:9` against a closed port: the old and new code print the same "no server at ... Connection refused" line, because both connections are refused.
- **Better:** offline checks for a server-only script go behind a `--self-test` flag in the same file; a new file under `scripts/` changes lis-guard's repo-script deny table.
- **Avoid:** running `scripts/hooks/tests/lis-guard_test.py` with an open, silent stdin (a background shell, a pipe that never closes). Its CASES row `python /dev/stdin` makes the hook's `check_script` open `/dev/stdin` and block: two agents lost a run to this at 0% CPU. Always run the suite with `< /dev/null`. The hook opening a device or FIFO a command names is a defect of its own (it predates W-25 and exists in the reference); not fixed yet.
- **Better:** for a guard port, ask the auditor for its own corpus and its own mutants, not a re-run of the builder's: on W-25 the builder's 18 mutants were all killed, while 3 of the auditor's 6 survived and showed untested bounds (20 words, 3 words) that the README had described as unbounded.
- **Avoid:** putting a grep line-number flag in the same Bash command as a git subcommand: a pre-tool hook on this machine read it as git's skip-hooks flag and refused the whole command (W-19 builder, a merge-file + diff + grep call). Run git and grep as separate commands, or spell the grep flag as `--line-number`.
- **Better:** when a reference commit mixes a tightening and a loosening (W-19: stricter hook rule, softer settings rows), port the tightening first as its own audited commit; the loosening then lands on a hook that has already been proven.

## 2026-11-14 — W-19 settings half
- **Mistake:** told the operator that a settings row "refuses any curl command containing `@`"; `Bash(curl *@*)` is an ask row, and the same wrong claim went into a builder brief → open `config/claude-settings.json` and name the list (allow / ask / deny) before saying what a row does.
- **Mistake:** corrected that with a second unchecked claim, "a hook allow overrides an ask row" (the model the reference and this repo's notes carried). The Claude Code documentation says the opposite: deny, then ask, then allow; an ask row prompts even when a PreToolUse hook returns allow; ask and deny rows hold in every mode → read the client documentation before stating a precedence, and write "documented, UNVERIFIED on the installed client" until a live run shows it.
- **Avoid:** judging a settings change only on the launcher's default path. The launcher accepts `--allowedTools`, `--user-scope` / `--full` and `--no-guard`; the audit found shapes that went from refused to allowed only under those (the removed `curl * -F*` and `curl *--form*` deny rows had no ask counterpart, here or in the reference).
- **Better:** a settings test needs a check that fails when an allow row for the guarded program appears, and cases for each removed row's own spelling; the first version let an added `Bash(curl *)` allow row and a re-spelled deny row pass.
