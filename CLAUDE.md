# CLAUDE.md — local-inference-studio-mac

Rules for agents working in this repo (macOS port of the Windows `local-inference-studio`;
the reference checkout, where present, is read-only).

## Backend and payloads

- No Ollama and no LM Studio, not even as a downloader or a model cache. The backend is
  Homebrew `llama.cpp` (`llama-server`); llama.cpp is never built or vendored here.
- Never commit binaries or models: `bin/`, `*.gguf` and `*.zip` anywhere, and everything
  under `models/` except `models/manifest.json` and `models/README.md`, are gitignored. So are `logs/` and
  `bench/results/`.
- No model download without the operator's pick. A download is done by hand
  (`curl -L --fail -C -` at a pinned revision, docs/SETUP.md); no script downloads
  anything, and `serve.sh` always passes `--offline`.
- Scripts never pipe to a shell (`curl | bash`, `curl | sh`, `eval "$(curl ...)"`).
- Never read or print `logs/api-key`; scripts pass the key by file or stdin, never argv.

## Evidence

- Mark any claim not verified on this machine as UNVERIFIED.
- A number goes into a doc only together with the saved log that holds it
  (`bench/run.sh` tees every run to `bench/results/`). Server logs under `logs/` are not
  bench logs: a figure read from one is labelled as such. An estimate stays labelled an
  estimate.
- A model is not "served" until `smoke.sh`, `chat_template_check.py` and one real
  `claude-local.sh` session have run on it.

## Gates over other programs' command lines

- A gate over llama-server's or claude's command line is an ALLOW-LIST, never a
  deny-list (`ALLOWED_FLAGS` in `scripts/serve.sh`, `PASS_LIST` in
  `scripts/claude-local.sh`). llama-server rewrites `_` to `-` in long flags, so
  `--some_flag` is `--some-flag`; the client adds flags between versions.
- Re-read `llama-server --help` and `claude --help` after every upgrade of either; both
  lists are pinned to the versions named in the scripts.

## Git

- The repo is local only: no remote, no push, until the owner says otherwise.
- Commit only when asked. Messages say why. They end with a `Co-Authored-By:` trailer
  and carry no session URL.
- No user names, home paths (write `~/...` or repo-relative paths), host names, email
  addresses, session URLs or secrets in tracked files. Grep every added line before a
  commit.
- Every built change gets an independent audit before the commit.

## macOS

- `/bin/bash` is 3.2: no associative arrays, no `${var,,}`, no `mapfile`.
- BSD tools: `sed -E` (no GNU extensions; `sed -i ''`), `stat -f %z`, BSD `date`; no
  `timeout` and no `flock`. Digests are `shasum -a 256`, as in the scripts and the
  manifest.
- `ps -o comm=` follows argv[0]: a name check, not an identity check (`stop.sh` also
  compares `lsof`'s first `txt` entry with the installed binary).
- `/tmp` and `/var` are links to `/private/...`.
- Sessions started by `scripts/claude-local.sh` run their Bash tool in `/bin/bash`, not
  the login zsh: the launcher pins `CLAUDE_CODE_SHELL=/bin/bash` and `SHELL=/bin/bash`
  (process env and `--settings` file), because `lis-guard.py` models bash syntax only.
  `~/.zshrc` aliases and functions are not applied there (`~/.bashrc` is, if present).
  UNVERIFIED on client 2.1.325 until a live session shows `$BASH_VERSION`.
- Python is `/opt/homebrew/bin/python3.13`, run with `-I` for anything that reads
  untrusted input.
- Text with quotes or backslashes goes through the Write tool, not a Bash heredoc or
  inline `python -c`.

## Processes

- Stop the server with `scripts/stop.sh` only (or Ctrl-C in its foreground
  `serve.sh` terminal). Never `pkill`/`killall`, a process-group or broadcast kill, or a
  kill whose pid is computed. One validated numeric pid, quoted, TERM.
- New signal, trap or kill-harness experiments run in a throwaway container, not on the
  host. The repo's suites signal only the stand-in processes they started themselves.
- Give every unattended `claude-local.sh -p` session a wall-clock bound and read its
  transcript after a few minutes. List and stop anything you started in the background
  before reporting.
- Before a serve or bench: swap, memory free, Low Power Mode off, large apps closed.

## Tests

Run from the repo root exactly as written:

- `/opt/homebrew/bin/python3.13 scripts/tests/serve_test.py` (about 2 minutes)
- `/opt/homebrew/bin/python3.13 scripts/tests/stop_test.py`
- `/opt/homebrew/bin/python3.13 scripts/tests/claude_local_test.py`
- `/opt/homebrew/bin/python3.13 -I scripts/hooks/tests/lis-guard_test.py`
- `/opt/homebrew/bin/python3.13 -I scripts/hooks/tests/lis-guard_property_test.py` (about 80 s)
- `/opt/homebrew/bin/python3.13 -I scripts/hooks/tests/loop-guard_test.py`
- `/opt/homebrew/bin/python3.13 -I scripts/hooks/tests/repeat-guard_test.py`
- `/opt/homebrew/bin/python3.13 -I scripts/hooks/tests/facts-inject_test.py`
- `/opt/homebrew/bin/python3.13 -I scripts/tests/chat_template_check.py --self-test`
  (offline: no server, no key file)
- With a server running only: `/opt/homebrew/bin/python3.13 -I scripts/tests/chat_template_check.py`,
  `scripts/smoke.sh`, `bench/run.sh --label NAME`.
