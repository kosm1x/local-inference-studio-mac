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
