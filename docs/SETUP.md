# Setup from scratch (Apple silicon Mac)

What was done on this machine (MacBook Air, Apple M4, 32 GB, macOS 26.5.2) to get from an
empty disk to a Claude Code session on a local model, in order. Commands run from the repo
root unless a step says otherwise. Anything not done here is marked UNVERIFIED.

## 0. Before you start

- Apple silicon, with Homebrew under `/opt/homebrew` (Homebrew 7.0.13 here). Installing
  Homebrew itself is not covered here.
- Disk: 15.44 GB for the default model, 10.38 GB for the second one.
- Memory: the default model's bench peaked at 27,256 MiB system memory in use (README).
  Judge a fit against memory actually free at start, not the 32 GB on the box.
- Pre-flight before any start or bench:

  ```bash
  sysctl vm.swapusage   # swap in use; a large figure means the machine is not ready
  pmset -g | grep -i lowpowermode   # 0 = Low Power Mode off
  vm_stat               # serve.sh also logs "memory free: <N> MiB available" at start
  ```

  Close large apps first. The 2026-11-14 benches ran with a browser open, on AC power.

## 1. The repo

The repo is a public GitHub repository. `git clone` it, or copy or clone an existing
local checkout, to a directory you own. Below, `<repo>` stands for that path. Nothing in it needs root.

## 2. llama.cpp and Python (Homebrew)

```bash
brew install llama.cpp python@3.13
/opt/homebrew/bin/llama-server --version   # version: 1.0.0 (build 12359, commit b1b770d90)
/opt/homebrew/bin/python3.13 --version     # Python 3.13.18
```

- `serve.sh` and `stop.sh` take `llama-server` from `/opt/homebrew/bin` (else
  `/usr/local/bin`), never from `PATH`. `serve.sh` and `claude-local.sh` take
  `python3.13` the same way. Both refuse a program whose real file or directory is owned
  by anyone but root and you, or writable by others (`serve.sh` checks every directory up
  to the prefix). `smoke.sh` and `bench/run.sh` use the `python3.13` on `PATH`.
- llama.cpp is never built from source or vendored here (`bin/` is gitignored).
- The extra-flag allow-list in `scripts/serve.sh` (`ALLOWED_FLAGS`) is pinned to build
  12359. After `brew upgrade llama.cpp`, re-read `llama-server --help` against it
  before trusting a start. Stop the server before upgrading: `stop.sh` checks the
  running executable against the installed one and refuses a server started from the
  old file (how `lsof` names the replaced file is UNVERIFIED).

## 3. Claude Code CLI

This machine runs Claude Code 2.1.325 as the native build at `~/.local/bin/claude` (a
link into `~/.local/share/claude/versions/`). Install it as Anthropic's Claude Code
documentation describes; how it was installed here is not recorded. If an installer
script is involved, download it to a file and read it before running it; never pipe a
download into a shell.

```bash
claude --version   # 2.1.325 (Claude Code)
```

`claude-local.sh` finds `claude` on `PATH`. Its pass-through allow-list (`PASS_LIST`) was
read from `claude --help` of 2.1.322; re-read it after every client upgrade.

## 4. Download a model (by hand)

No script downloads anything. Pick a file, take the commit it should come from (the
revision), and fetch it at that revision into its own directory under `models/`. The
default model:

```bash
mkdir -p models/qwen3.6-35b-a3b-heretic
curl -L --fail -C - \
  -o models/qwen3.6-35b-a3b-heretic/Qwen3.6-35B-A3B-uncensored-heretic.i1-IQ3_M.gguf \
  https://huggingface.co/mradermacher/Qwen3.6-35B-A3B-uncensored-heretic-i1-GGUF/resolve/97c91a931dbfd582487e8866bd129a2e8765051d/Qwen3.6-35B-A3B-uncensored-heretic.i1-IQ3_M.gguf
```

The second model (dense 27B):

```bash
mkdir -p models/qwen3.8-27b-orcarouter-gsq-rco
curl -L --fail -C - \
  -o models/qwen3.8-27b-orcarouter-gsq-rco/Qwen3.8-27B-OrcaRouter-GSQ-RCO-IQ3_XXS-v2.1.gguf \
  https://huggingface.co/RentedNoodle/Qwen3.8-27B-OrcaRouter-GSQ-RCO-IQ3_XXS-Uncensored/resolve/2113bb22c43635fcdaad4b728c914db4c2ec1d3b/Qwen3.8-27B-OrcaRouter-GSQ-RCO-IQ3_XXS-v2.1.gguf
```

- `-L` follows the redirect to the file store, `--fail` stops on an HTTP error instead
  of saving the error page as the model, `-C -` resumes a partial file: re-run the same
  command after an interruption.
- The revision in the URL pins the bytes; a branch name (`main`) would not.
- Model files are gitignored (`.gitignore`: everything under `models/` except
  `models/manifest.json` and `models/README.md`, plus `*.gguf` and `*.zip` anywhere);
  never commit one.

## 5. Check the file and record it in the manifest

```bash
stat -f %z models/qwen3.6-35b-a3b-heretic/Qwen3.6-35B-A3B-uncensored-heretic.i1-IQ3_M.gguf
shasum -a 256 models/qwen3.6-35b-a3b-heretic/Qwen3.6-35B-A3B-uncensored-heretic.i1-IQ3_M.gguf
```

Compare the sha256 with the publisher's: on Hugging Face, the LFS SHA256 shown on the
file's page at the same revision. If they differ, delete the file. Then record both
values in the file's `models/manifest.json` entry (`size` as an integer, `sha256` as 64
lowercase hex characters). The two shipped entries already hold the values measured
here, which matched the publisher's:

- `qwen3.6-35b-a3b-heretic-iq3m`: size 15440520352, sha256
  `18f3d1853052d31fd1012291768daa6dcd81479e10b92436e2f2b04426ea36e0`
- `qwen3.8-27b-orcarouter-iq3xxs`: size 10382717184, sha256
  `ab955b5083d9cdf0bf55c37acdcae359b78756c4544d97960c23d8fca98feb9b`

A new entry's fields and rules are in
[models/README.md](../models/README.md). The first start after each boot hashes the file
again and refuses on any difference (marker `logs/model-verify.txt`).

## 6. Chat template

Claude Code sends system messages after the first one: llama-server converts a Claude
Code request into a message list like `[system, user, system, ...]` before it applies
the chat template. A template that raises on a later system message turns every such
turn into HTTP 500. The default model's embedded template does exactly that (first real
session, 2026-11-14: "System message must be at the beginning."), although smoke and
bench passed on it, because neither sends that shape.

- Both manifest entries point at a patched template in `config/chat-templates/` (the
  `chat_template` field), so switching `MODEL_ID` switches the template too. Each file's
  header says where it came from and what was changed.
- For a new model: the embedded template is the `chat_template` field of `GET /props`
  (the shipped Qwen3.6 file was extracted that way); send the key from a file, as
  `smoke.sh` does, never on a command line. Start the server on the embedded template
  and save a baseline, then patch, point the entry at the patched file, restart, and
  compare:

  ```bash
  mkdir -p bench/results   # gitignored, so absent on a fresh clone
  /opt/homebrew/bin/python3.13 -I scripts/tests/chat_template_check.py --save-baseline bench/results/<name>-baseline.json
  # write config/chat-templates/<name>.jinja, add "chat_template" to the entry, restart
  /opt/homebrew/bin/python3.13 -I scripts/tests/chat_template_check.py --baseline bench/results/<name>-baseline.json
  ```

  The check renders the shapes Claude Code sends through `POST /apply-template`, makes
  one real `/v1/chat/completions` call, and ends with `PASS` (exit 0). With
  `--baseline` it also requires every single-system conversation to render byte for byte
  as the unpatched template did.

## 7. First start

```bash
scripts/serve.sh --dry-run             # the exact llama-server command line; starts nothing
scripts/serve.sh --detach --wait 300   # returns 0 once GET /health answers 200
```

- The dry run for the default model shows `-ctk f16 -ctv f16`, `--reasoning auto`,
  `--chat-template-file .../qwen3.6-35b-a3b.jinja` and `--swa-full`.
- Each start writes a new random key to `logs/api-key` (mode 600) and a server log
  `logs/server-<UTC stamp>.log` whose first line is the command line. The server binds
  127.0.0.1 only and runs `--offline`.
- Without `--detach`, llama-server runs in the foreground; Ctrl-C stops it.

## 8. Smoke and template check

```bash
scripts/smoke.sh
/opt/homebrew/bin/python3.13 -I scripts/tests/chat_template_check.py
```

`smoke.sh` checks health, a text reply, a `tool_use` and a `tool_result` round trip and
ends with `PASS: health, text, tool_use and tool_result round trip`. The template check
ends with `PASS`.

## 9. Bench

```bash
bench/run.sh --label <name> --quant IQ3_M --ctx 131072
```

It benches the running server (it never starts or stops one) and writes
`bench/results/<stamp>-<label>.json` plus the tee'd console log `<stamp>-<label>.log`.
A run took about 5 minutes on the default model with reasoning off, about 9 with
reasoning auto and about 33 on the 27B (start and end stamps in the saved files). Results
are in the README. To bench a reasoning setting other than the manifest's, restart with
`scripts/serve.sh --detach --wait 300 --reasoning off` (or `on`, `auto`).

## 10. First session

From the project directory you want to work in:

```bash
<repo>/scripts/claude-local.sh                  # interactive
<repo>/scripts/claude-local.sh --print-args     # show the arguments only
<repo>/scripts/claude-local.sh --start-server   # start the server first if /health is not ok
<repo>/scripts/claude-local.sh -p "<task>" --permission-mode acceptEdits
```

- After the hook-copies line, the launcher prints three status lines on stderr; on
  2026-11-15 they were (`bench/results/20261115-live-guard-check/s1.err`):

  ```
  claude-local: user scope: off (S-O3)
  claude-local: profile: none   guard: on   context tokens: 114688
  claude-local: session shell: /bin/bash (pinned as CLAUDE_CODE_SHELL and SHELL)
  ```

  The first two date from the port's first commit; the session shell line came with the
  pin. The client then printed a note that claude.ai connectors are disabled and
  `[claude-code:unrecognized_model]`, as in the first working session
  (`bench/results/20261114-session2.stderr.log`).
- The session's Bash tool runs `/bin/bash` 3.2 rather than your login zsh, because the
  guard hook reads commands as bash syntax: the aliases, functions and options of
  `~/.zshrc` are not applied to tool commands, and per Claude Code's documentation
  `~/.bashrc` is sourced instead if present (UNVERIFIED). The launcher refuses to start
  if `/bin/bash` is missing, not an executable regular file, or owned by anyone but root
  and you or writable by everyone, and removes an inherited `CLAUDE_CODE_SHELL_PREFIX` or
  `CLAUDE_ENV_FILE`, the bash functions exported from your shell (it refuses to start if
  one survives; a malformed `BASH_FUNC_` entry, which bash does not import, passes through
  inert) and bash start-up variables such as `PS4` and `PROMPT_COMMAND` (README, Known
  limits, has the list and the limit: the launcher does not defend itself against the
  shell that starts it).
- Seen live on client 2.1.325 in print mode (2026-11-15, `s6` and `s7` in
  `bench/results/20261115-live-guard-check/`): `echo =ls` returned `=ls` (zsh would print
  a path) and `shopt -p extglob` returned `shopt -u extglob` with exit 1 (bash's normal
  status for an unset option; zsh has no `shopt`), so the Bash tool ran bash. The version
  string was not observed; 3.2 follows from the pinned path. Use these variable-free
  probes: in print mode the client itself, not the hook, refused `echo "BV=$BASH_VERSION
  ZV=$ZSH_VERSION S=$SHELL"` with "A variable in this command can't be checked before it
  runs" (`s1`). `shopt -p extglob` ran under `--allowedTools 'Bash(shopt -p
  extglob)'` (as reported in `NOTES.txt`; the `s7` log does not record its arguments;
  without a grant it was not tried); `echo =ls` ran without a grant. Re-check after a
  client upgrade.
- In print mode (`-p`) nobody can answer a permission prompt: a command that needs one
  comes back "This command requires approval" (first working session, read in its
  transcript; live, `s9`) or,
  for a command an ask row matches, "Claude requested permissions to use Bash, but you
  haven't granted it yet." (live, 2026-11-15, `s4b`, `s10`). The first working session
  (237 requests, per the commit that added the patched template) fixed its fixture bug
  correctly but never ran the tests for that reason (both read in the session transcript,
  which was not saved under `bench/results/`), and the guard also refuses inline
  `python3 -c` and running a file written in the same session. Pre-approve the test
  command with `--allowedTools` or work interactively. Tried live: the glob grant
  `Bash(curl *)` let `curl https://example.invalid/x` run, which was refused without it
  (`s8` against `s9`; no settings row matches that command and the hook does not decide
  it, `step0.out`); the exact grant `Bash(shopt -p extglob)` ran that command (`s7`, grant
  as reported in `NOTES.txt`, not recorded in the log; no row, no hook decision). A grant did not get `curl -d x http://127.0.0.1:9/x` past the
  ask row `Bash(curl -d*)` (`s4b`, `s10`; limits in README, Known limits).
- Judge a session by its tool results, not by the model's closing text: in the live
  check the closing prose called a hook deny, a client refusal and failed curls
  "executed" or "successfully executed" (`s2b`, `s4b`, `s5b`, `s8`) and a refusal
  "running" (`s10`), and three first prompts got a text reply with no tool call (`s2`,
  `s4`, `s5`).
- Give a headless session a time bound and read its transcript after a few minutes. That
  session sent one refused command 219 times in a row (read in the session transcript,
  which was not saved under `bench/results/`) until it was stopped by hand.
- In print mode the launcher adds `--max-turns 60` (stderr: `print mode without
  --max-turns: added --max-turns 60`); pass `--max-turns N` (1 to 10000) to change it. At
  the cap the JSON result has `"subtype":"error_max_turns"` and `"is_error":true` (live
  run with `--max-turns 2`, 2026-11-14: `bench/results/20261114-max-turns-2-live.json`,
  gitignored). It
  caps turns, not the time one turn takes, so the time bound above still applies.
- A model counts as served only after one real session like this has run. On this Mac
  only the default model has had one; the README section "What has and has not been
  tested" lists the sessions and what was never tried.

## 11. Stop

```bash
scripts/stop.sh   # stopping llama-server pid <N>, then: stopped pid <N>
```

It stops the one pid in `logs/llama-server.pid`, after checking that it runs the
installed llama-server, with SIGTERM and a wait of up to 30 s. Never stop the server
with `pkill`, `killall` or a computed kill.

## 12. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Every Claude Code turn: HTTP 500 "System message must be at the beginning." | The template raises on a later system message. Give the entry a patched `chat_template` (step 6) and run `chat_template_check.py`. |
| `serve.sh: refusing to start: ... sha256 ..., ... says ...` (or `... bytes`) | The file differs from the manifest. Delete it and download again at the pinned revision (steps 4-5). |
| `serve.sh: model file not found: ...` | The path in the entry does not match the download location. |
| `serve.sh: another server holds logs/serve.lock ...` or `port 8080 is already in use` | A server is running. `scripts/stop.sh`, or change `PORT` in `config/lis.conf`. |
| `serve.sh: removing stale lock .../logs/serve.lock` on the first start after `scripts/stop.sh` | Expected and harmless. A `--detach` start leaves the lock in place while its server runs (the lock also records the server log that `stop.sh` appends to), and `stop.sh` removes only the pid file. The next start finds neither the lock's `serve.sh` nor a llama-server alive, removes the lock and takes a new one. |
| `serve.sh: ... is not on serve.sh's allow-list of extra llama-server flags` | The flag in `server_args` or after `--` is not allowed; see the table in models/README.md. |
| `stop.sh: refusing: pid ... runs ..., not the installed llama-server ...` | Often a server started before `brew upgrade llama.cpp`. Stop it with Ctrl-C in its `serve.sh` terminal. |
| `smoke.sh` / `bench/run.sh`: `no key file ...` | No server has started since `logs/` was cleared. Start one. |
| `chat_template_check.py`: `FAIL: no server at ...` | Start the server (step 7). |
| Bench `tools` low, misses are `no_call` | Replay those prompts and read the replies: an invented answer points at the reasoning setting (the default model needs `auto`); a tool call printed as text points at the template. |
| `-p` session: "This command requires approval" or "Claude requested permissions to use Bash, but you haven't granted it yet." | Print mode cannot answer prompts; pre-approve the command (`--allowedTools`) or run interactively. The second text came for a command that only an ask row matches (this repo's model of the client's matching), under a `Bash(curl *)` grant that did let an unmatched curl run: the grant did not override the ask row (live, 2026-11-15, `s8`-`s10`; the client does not name the rule). |
| `-p` session: "A variable in this command can't be checked before it runs" | The client itself, not the hook, refused a command with a variable expansion (seen once, in print mode on 2.1.325, for `echo "BV=$BASH_VERSION ..."`). Use a command without `$`. |
| The model repeats one refused command | Known limit. A print-mode run stops at its turn cap (`--max-turns`, default 60); an interactive one must be stopped by hand. |
| Replies slow down as a session grows | Expected: decode falls with context depth (README). |
| `claude-local: bash (the session shell) ...` | `/bin/bash` failed the launcher's tool check (missing, not an executable file, foreign owner or writable by everyone). The launcher uses that fixed path only, never a Homebrew bash or `PATH`. |
| An alias or function from `~/.zshrc` is missing in a session | Expected: sessions run `/bin/bash`, not zsh (step 10). Put what tool commands need in `~/.bashrc`, or call the program by name. |
