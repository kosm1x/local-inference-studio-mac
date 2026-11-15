#!/bin/bash
# claude-local.sh - run Claude Code against this checkout's llama-server (127.0.0.1).
#
# macOS port of scripts/claude-local.ps1 from the Windows reference (local-inference-studio,
# commit cafc34b), lean default path and --full only. Usage:
#
#   scripts/claude-local.sh [WRAPPER OPTIONS] [CLAUDE ARGUMENTS...] [-- CLAUDE ARGUMENTS...]
#
# Wrapper options (taken out of the argument list wherever they stand before a `--`;
# each may be given once). Everything else goes to claude in order, but only what the
# pass-through allow-list below names (print mode and its formats, --no-session-
# persistence, --continue, --resume, --name, --verbose, --permission-mode manual|acceptEdits|plan, --allowedTools/--disallowedTools,
# --max-turns N, the prompt); a `--` and the words after it go verbatim, and none of them
# may start with '-':
#   --full             the full tool set: no --tools list, no policy text, user scope on;
#                      MCP stays off (--strict-mcp-config with an empty config).
#   --start-server     when GET /health is not ok, run scripts/serve.sh --detach (it
#                      returns once /health answers) before the key is read.
#   --no-guard         no hooks (lis-guard, loop-guard, repeat-guard, verify-doc).
#   --no-loop-guard    lis-guard (and verify-doc) only.
#   --strict-slot      refuse when a server slot is busy (default: warn and queue).
#   --user-scope       keep ~/.claude/settings.json (drops --setting-sources).
#   --context-tokens N the window Claude Code is told about (an integer >= 1024; default
#                      CLIENT_CTX from config/lis.conf). Keep it <= CTX - 16384.
#   --print-args       print the claude argument list and the settings JSON on stdout and
#                      exit 0: no server contact, no key read, nothing written, claude not
#                      started.
#
# Turn cap (a backstop for unattended runs: a headless run once repeated one refused Bash
# call 219 times in 45 minutes; the hooks can refuse a call but cannot end the turn):
# --max-turns N (N from 1 to 10000, digits only, no leading zero; once) goes to claude as
# given. In print mode (-p/--print) without it, the launcher adds --max-turns 60 ahead of
# the caller's arguments and says so on stderr; interactive sessions get none. The option
# is hidden from `claude --help` of 2.1.325 but parsed (`--max-turns <turns>`, a non-number
# refused) and enforced there: a print run that reaches the cap ends with the JSON result
# subtype error_max_turns. It caps the number of turns, not the time one turn takes.
#
# Order (ledger: a server-starting launcher runs after every refusal and before any
# per-start secret read, because serve.sh writes a new key at every start):
#   1. every refusal and validation (options against the allow-list, config/lis.conf,
#      the loopback URL, tools, the working directory's .claude/settings*.json);
#   2. only with --start-server and only when /health is not ok: scripts/serve.sh --detach;
#   3. only then logs/api-key is read;
#   4. the server preflight (/health ok; /slots with the key: busy warns, or refuses with
#      --strict-slot; a refused /slots refuses the launch);
#   5. a new per-run dir (one mkdir -m 700, never an existing path) named
#      lis-hooks-<stamp>-<pid>-<random digits from /dev/urandom> in $TMPDIR, holding
#      read-only (444) copies of the hooks and the per-run settings
#      (lis-settings-<pid>.json, built from config/claude-settings.json) and the empty
#      MCP config (lis-mcp-<pid>.json); the two JSON files are removed on exit, the hook
#      copies are kept (as the reference keeps them);
#   6. the env block below, then claude runs as this script's child; its exit code is
#      this script's.
#
# Env given to claude: ANTHROPIC_BASE_URL=http://127.0.0.1:<PORT>, ANTHROPIC_AUTH_TOKEN=
# <key>, ANTHROPIC_MODEL and ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL=local,
# CLAUDE_CODE_MAX_CONTEXT_TOKENS and CLAUDE_CODE_AUTO_COMPACT_WINDOW=<context tokens>,
# CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384, API_TIMEOUT_MS=1800000,
# CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1,
# DISABLE_PROMPT_CACHING=1, ENABLE_TOOL_SEARCH=false, CLAUDE_CODE_ATTRIBUTION_HEADER=0,
# LIS_SESSION_START (ISO 8601 UTC, 6 fraction digits), and CLAUDE_CODE_SHELL=/bin/bash
# and SHELL=/bin/bash (the session shell: the Bash tool runs bash, the syntax lis-guard
# models, not the zsh of $SHELL; seen on 2.1.325 in print mode on 2026-11-15 via `echo =ls`
# and `shopt -p extglob`; the version string was not observed, as the client refuses a
# command with a `$` variable there). Removed from the inherited env at
# the very top, before serve.sh or anything else runs (the environment step lists them):
# every inherited ANTHROPIC_*, CLAUDE_CODE_* and LIS_* variable (ANTHROPIC_API_KEY,
# CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CODE_SUBAGENT_MODEL(_FORCE), CLAUDE_CODE_USE_BEDROCK/
# VERTEX/FOUNDRY, ANTHROPIC_CUSTOM_HEADERS, CLAUDE_CODE_SHELL(_PREFIX), LIS_RELAUNCH(_RC),
# LIS_TEST_* among them), CLAUDE_ENV_FILE,
# credentials and routing, proxies, node/bun code loading and TLS trust overrides, shell
# and Python start-up files, DYLD_*, another config dir and parent-session markers; an
# inherited SHELLOPTS is switched off (set +xv) and unexported. The process env
# then points claude at the loopback server only; settings files can still set env
# (the per-run file pins ANTHROPIC_BASE_URL to the same loopback URL and the session
# shell, and a project file that sets ANTHROPIC_*, CLAUDE_CODE_*, SHELL, CLAUDE_ENV_FILE,
# a bash start-up name (see PROJECT_PY) or a proxy variable refuses the launch), and
# whether a flag-level value beats a project one is UNVERIFIED (see below). The key is
# never in an argument list (curl gets its header from stdin with -K -, printf being a
# builtin), never in a settings file, never logged and never printed.
#
# Isolation from ~/.claude/settings.json: every mode except --full and --user-scope passes
# --setting-sources project,local (user scope off), as the reference does. --settings
# <per-run file> pins model local, the ANTHROPIC_*_MODEL and CLAUDE_CODE_SUBAGENT_MODEL
# pins, ANTHROPIC_BASE_URL, CLAUDE_CODE_SHELL and SHELL, and disableAllHooks false. That
# these flag-level values win
# over the project and local files of the working directory is UNVERIFIED on this client
# (2.1.322 when written, 2.1.325 now): the launcher therefore refuses to start when <cwd>/.claude/settings.json or
# settings.local.json holds apiKeyHelper, disableAllHooks, hooks, a permissions.defaultMode
# other than manual/acceptEdits/plan/default, or an env name starting ANTHROPIC_ or
# CLAUDE_CODE_, SHELL, CLAUDE_ENV_FILE, BASH_ENV, ENV, HOME, POSIXLY_CORRECT, INPUTRC,
# SHELLOPTS, BASHOPTS, PS1, PS2, PS4, PROMPT_COMMAND, a name starting BASH_FUNC_, or a
# proxy variable (or cannot be parsed); env.PATH is not refused (open). Under
# --user-scope and --full the user settings load too and are never read here: an env
# entry there (SHELL, CLAUDE_CODE_SHELL_PREFIX, CLAUDE_ENV_FILE, ...) applies; for the
# two shell names the per-run file's pin is documented to outrank it (UNVERIFIED), for
# the prefix and the env file nothing here overrides it. Whether claude reads the git
# top level's .claude instead of the cwd's when started in a subdirectory is UNVERIFIED.
# The user-scope CLAUDE.md still loads under --setting-sources (reference finding on
# Windows; UNVERIFIED on macOS). That this claude build honours --setting-sources the same
# way is UNVERIFIED: real runs on 2026-11-14 were one session of 237 requests (client
# version not recorded) and a print run capped at --max-turns 2 (2.1.325), but neither
# checked whether ~/.claude/settings.json stayed out or the user-scope CLAUDE.md loaded.
#
# Out of scope for this port: -LisWeb/-LisWebAuto/-LisTerminal/-LisKeepState (the lis-web
# container), -LisTask profiles and the longrun relaunch supervisor (so facts-inject.py is
# never registered), LIS_CLAUDE_EXE (the tests put stubs first on PATH instead).
set +xv    # an inherited SHELLOPTS=xtrace or verbose would print the key
# Exported functions (BASH_FUNC_<name>%% in the env): bash imports each as a function and
# exports it again to every child, so the client's Bash tool (bash, see SESSION_SHELL)
# and serve.sh would run it in place of the command it names (BASH_FUNC_ls%% replaces
# ls). No function is defined here yet, so every one listed is an imported one: all are
# removed before die and say are defined (a redefined function keeps its export flag).
# Outside posix mode bash 3.2 imports names that are no identifier too (-x, --, a-b,
# a.b), hence the -- before the name. The loop condition is a keyword test, so the loop
# ends whatever the caller exported. If a function is still listed afterwards, the launch
# fails closed. A BASH_FUNC_ entry that is no function definition is not imported, stays
# in the env as it is and is inert in bash 3.2.
# Limit: the launcher does not defend itself against the shell that starts it (seen with
# echo-only stand-ins on bash 3.2): a caller BASH_ENV is sourced by this bash before its
# first line; a caller function named builtin replaces every `builtin X` here, so it
# defeats the removal and the check and reaches the client; a caller function named set
# runs once, in place of the first line, outside posix mode, and is then removed.
fns=$(builtin declare -F)
while [[ -n $fns ]]; do
    fn=${fns%%$'\n'*}
    case $fns in *$'\n'*) fns=${fns#*$'\n'} ;; *) fns= ;; esac
    case $fn in 'declare -f'*' '*) builtin unset -f -- "${fn#declare -f* }" ;; esac
done
case $(builtin declare -F) in
    '') ;;
    *) builtin printf 'claude-local: %s\n' 'an exported shell function survived the removal; refusing to start (unexport it in the calling shell).' >&2
       builtin exit 1 ;;
esac
set +xv    # again: a caller function named set (removed above) replaced the first one
unset fns fn
set -u
# An inherited POSIXLY_CORRECT starts bash in posix mode, before any line here runs. The
# environment scrub below unsets the variable, which also leaves posix mode in 3.2.57;
# this line leaves it before that, for the lines in between.
set +o posix
umask 077
# SHELLOPTS stays exported after set +x when it came from the env (every child bash, the
# client's Bash tool included, would inherit nounset); BASHOPTS likewise for bash >= 4.
export -n SHELLOPTS
unset BASHOPTS

die() { printf 'claude-local: %s\n' "$*" >&2; exit 1; }
say() { printf 'claude-local: %s\n' "$*" >&2; }

# ---------------------------------------------------------------- environment
# Scrubbed before anything runs (before any refusal, before serve.sh --detach, before the
# key read), so neither serve.sh nor claude nor any tool run here inherits:
#   ANTHROPIC_*, CLAUDE_CODE_*, LIS_* (every one; the launch block below sets its own,
#     and LIS_TEST_* are the server's and this script's test overrides),
#     AWS_BEARER_TOKEN_BEDROCK, CLOUD_ML_REGION: credentials and routing;
#   the proxy variables (http, https, all, no; either case);
#   code loaded into node/bun and TLS trust overrides: NODE_OPTIONS, NODE_EXTRA_CA_CERTS,
#     every BUN_*, SSL_CERT_FILE, SSL_CERT_DIR, CURL_CA_BUNDLE, REQUESTS_CA_BUNDLE;
#   shell and Python start-up: BASH_ENV, ENV, every PYTHON* (the hooks run python3.13 -I,
#     which needs none); the dynamic loader: every DYLD_* (SIP already strips these when
#     /bin/bash starts);
#   what bash runs or reads at start-up or around a command, beyond BASH_ENV and ENV (man
#     bash of 3.2): PS4 (expanded, $( ) included, before each traced command), PS1, PS2
#     and PROMPT_COMMAND (interactive shells only), POSIXLY_CORRECT (posix mode: other
#     start-up files, other parsing), INPUTRC (readline's start-up file); SHELLOPTS is
#     unexported and BASHOPTS unset above, exported functions (BASH_FUNC_*) removed above;
#   another config dir and the markers of a parent agent session: CLAUDE_CONFIG_DIR,
#     CLAUDECODE, CLAUDE_EFFORT, CLAUDE_PID, AI_AGENT;
#   what the client runs around or before each shell command: CLAUDE_ENV_FILE (a script
#     run before every Bash command) and, inside CLAUDE_CODE_*, CLAUDE_CODE_SHELL and
#     CLAUDE_CODE_SHELL_PREFIX (a wrapper of every Bash and hook command). The session
#     shell is pinned in the launch block instead (see SESSION_SHELL); an inherited SHELL
#     is left alone until then and overwritten there.
# The test-only tool override (LIS_TEST_CLIENT_BIN, see the tools step) is read first
# into an unexported variable, then scrubbed with the rest.
TEST_BIN=${LIS_TEST_CLIENT_BIN-}
for name in $(compgen -e); do
    case $name in
        ANTHROPIC_* | CLAUDE_CODE_* | LIS_* | AWS_BEARER_TOKEN_BEDROCK | CLOUD_ML_REGION | \
            http_proxy | HTTP_PROXY | https_proxy | HTTPS_PROXY | all_proxy | ALL_PROXY | \
            no_proxy | NO_PROXY | \
            NODE_OPTIONS | NODE_EXTRA_CA_CERTS | BUN_* | SSL_CERT_FILE | SSL_CERT_DIR | \
            CURL_CA_BUNDLE | REQUESTS_CA_BUNDLE | \
            BASH_ENV | ENV | PYTHON* | \
            PS1 | PS2 | PS4 | PROMPT_COMMAND | POSIXLY_CORRECT | INPUTRC | \
            DYLD_* | \
            CLAUDE_CONFIG_DIR | CLAUDECODE | CLAUDE_EFFORT | CLAUDE_PID | AI_AGENT | \
            CLAUDE_ENV_FILE)
            unset "$name" ;;
    esac
done

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd -P) || exit 1
ROOT=$(cd "$SCRIPT_DIR/.." && pwd -P) || exit 1
CONF="$ROOT/config/lis.conf"
SETTINGS_SRC="$ROOT/config/claude-settings.json"
HOOKS_SRC="$ROOT/scripts/hooks"
VERIFY_DOC="$ROOT/scripts/verify-doc.py"
SERVE="$ROOT/scripts/serve.sh"
KEY_FILE="$ROOT/logs/api-key"
NL=$'\n'
# The shell of the session's Bash tool. lis-guard models bash syntax; Claude Code takes the
# tool's shell from CLAUDE_CODE_SHELL, else from $SHELL (zsh on this Mac), and zsh has
# syntax bash lacks (=( ), =name, glob qualifiers that run code). Fixed path, no PATH
# lookup, no Homebrew bash: the system /bin/bash (3.2). Given to claude as CLAUDE_CODE_SHELL
# and SHELL (so a silent fallback to auto-detection, documented for a value that is not a
# working bash or zsh, still lands on bash) in the process env and in the --settings file.
SESSION_SHELL=/bin/bash

# ---------------------------------------------------------------- 1. refusals
FULL=0; START_SERVER=0; NO_GUARD=0; NO_LOOP_GUARD=0; STRICT_SLOT=0; USER_SCOPE=0
PRINT_ARGS=0; CTX_TOKENS=
claude_args=()

twice() { die "$1 was given more than once."; }
set_ctx() {
    [ -z "$CTX_TOKENS" ] || twice --context-tokens
    case $1 in
        '' | *[!0-9]*) die "--context-tokens value '$1' is not an integer." ;;
    esac
    [ "${#1}" -le 9 ] || die "--context-tokens value '$1' is too large."
    [ "$((10#$1))" -ge 1024 ] || die "--context-tokens value $1 is below the minimum of 1024."
    CTX_TOKENS=$((10#$1))
}

passthru=0
while [ $# -gt 0 ]; do
    a=$1
    shift
    if [ "$passthru" = 1 ]; then claude_args+=("$a"); continue; fi
    case $a in
        --) passthru=1; claude_args+=("$a") ;;
        --full) [ "$FULL" = 0 ] || twice "$a"; FULL=1 ;;
        --start-server) [ "$START_SERVER" = 0 ] || twice "$a"; START_SERVER=1 ;;
        --no-guard) [ "$NO_GUARD" = 0 ] || twice "$a"; NO_GUARD=1 ;;
        --no-loop-guard) [ "$NO_LOOP_GUARD" = 0 ] || twice "$a"; NO_LOOP_GUARD=1 ;;
        --strict-slot) [ "$STRICT_SLOT" = 0 ] || twice "$a"; STRICT_SLOT=1 ;;
        --user-scope) [ "$USER_SCOPE" = 0 ] || twice "$a"; USER_SCOPE=1 ;;
        --print-args) [ "$PRINT_ARGS" = 0 ] || twice "$a"; PRINT_ARGS=1 ;;
        --context-tokens)
            [ $# -gt 0 ] || die "--context-tokens needs a value (an integer >= 1024)."
            set_ctx "$1"
            shift ;;
        --context-tokens=*) set_ctx "${a#*=}" ;;
        *) claude_args+=("$a") ;;
    esac
done

lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }

# The pass-through allow-list. Read from `claude --help` of client 2.1.322 (Commander
# parsing: a <value> option takes the next word whatever it is, a [value] option takes the
# next word only when it does not start with '-', a <values...> option takes every
# following word up to the next one that starts with '-', and the first operand, before or
# after a `--`, is matched against the subcommand names). RE-READ IT ON EVERY CLIENT
# UPGRADE: a new option or subcommand upstream is refused, or worse slips through as a
# value, until this list is checked again. Everything else is refused by name: an option
# not listed, --flag=value for a flag not listed, a short-option cluster (-pc), a first
# operand that is a subcommand name, and any word starting with '-' after a bare `--`.
# --max-turns <turns> is not in that help text (hidden in 2.1.325) but is parsed and
# enforced by that client; it is checked here as an integer from 1 to 10000, given once.
PASS_LIST='-p/--print, --output-format text|json|stream-json, --input-format text|stream-json, --include-partial-messages, --no-session-persistence, -c/--continue, -r/--resume [id], -n/--name <name>, --verbose, --permission-mode manual|acceptEdits|plan, --allowedTools/--allowed-tools <tools...>, --disallowedTools/--disallowed-tools <tools...>, --max-turns <1-10000>, and the prompt'
# Subcommand names and aliases of 2.1.322, plus Commander's implicit `help`.
SUBCOMMANDS=' agents attach auth auto-mode doctor gateway help import install kill logs mcp plugin plugins purge respawn rm setup-token stop ultrareview update upgrade '
# One tool rule: a capitalised tool name or an mcp__ name, with an optional (specifier)
# (Bash, Bash(git log:*), WebFetch(domain:x), mcp__s__t); a value holds rules split by
# commas or spaces. A word that is no rule list (most often a prompt swallowed by the
# list, as "fix the bug") is refused.
# Which rules --allowedTools may grant (and project permissions.allow) is left as it is on
# purpose: an open design question for the operator; do not tighten or widen it here.
TOOL_RULE='([A-Z][A-Za-z0-9]*|mcp__[A-Za-z0-9_-]+)(\([^()]*\))?'
TOOL_LIST_RE="^${TOOL_RULE}([ ,]+${TOOL_RULE})*[ ,]*\$"
RESUME_RE='^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
not_listed() { die "'$1' is not on the launcher's pass-through list ($PASS_LIST); leave it out."; }
choice() {    # $1 = option, $2 = value
    case "$1 $2" in
        '--output-format text' | '--output-format json' | '--output-format stream-json') ;;
        '--input-format text' | '--input-format stream-json') ;;
        '--permission-mode manual' | '--permission-mode acceptEdits' | '--permission-mode plan') ;;
        *) die "$1 '$2' is not allowed here ($PASS_LIST)." ;;
    esac
}
tool_value() {    # $1 = option, $2 = one word of its value list
    [[ $2 =~ $TOOL_LIST_RE ]] ||
        die "$1 value '$2' is not a tool rule list (a prompt after $1 is read as tool names: put the prompt first or after --)."
}
resume_value() {    # $1 = value, $2 = option (default --resume)
    [[ $1 =~ $RESUME_RE ]] || die "${2:---resume} value '$1' is not a session id or name (letters, digits, '.', '_', '-')."
}
MAX_TURNS=; MAX_TURNS_DEFAULT=60; PRINT_MODE=0
max_turns_value() {    # $1 = value: an integer from 1 to 10000, digits only, given once
    [ -z "$MAX_TURNS" ] || twice --max-turns
    [[ $1 =~ ^[1-9][0-9]{0,4}$ ]] && [ "$1" -le 10000 ] ||
        die "--max-turns value '$1' is not an integer from 1 to 10000 ($PASS_LIST)."
    MAX_TURNS=$1
}
operand=0
first_operand() {
    [ "$operand" = 0 ] || return 0
    operand=1
    case $SUBCOMMANDS in
        *" $(lower "$1") "*) die "'$1' as the first word is a claude subcommand name; the launcher starts sessions only (reword the prompt)." ;;
    esac
}
n_args=${#claude_args[@]}
i=0; after_dd=0; multi=
while [ "$i" -lt "$n_args" ]; do
    a=${claude_args[$i]}
    i=$((i + 1))
    if [ "$after_dd" = 1 ]; then
        case $a in -*) die "'$a' after '--' looks like an option; the launcher passes only prompt text there." ;; esac
        first_operand "$a"
        continue
    fi
    if [ -n "$multi" ]; then
        case $a in
            -*) multi= ;;
            *) tool_value "$multi" "$a"; continue ;;
        esac
    fi
    case $a in
        --) after_dd=1 ;;
        -p | --print) PRINT_MODE=1 ;;
        -c | --continue | --verbose | --include-partial-messages | --no-session-persistence) ;;
        --max-turns)    # <turns>: the next word whatever it is, validated
            [ "$i" -lt "$n_args" ] || die "--max-turns needs a value (an integer from 1 to 10000)."
            max_turns_value "${claude_args[$i]}"
            i=$((i + 1)) ;;
        --max-turns=*) max_turns_value "${a#*=}" ;;
        -n | --name)    # <name>: the next word whatever it is, validated as a resume id
            [ "$i" -lt "$n_args" ] || die "$a needs a value (a session name)."
            resume_value "${claude_args[$i]}" "$a"
            i=$((i + 1)) ;;
        --name=*) resume_value "${a#*=}" --name ;;
        --output-format | --input-format | --permission-mode)
            [ "$i" -lt "$n_args" ] || die "$a needs a value ($PASS_LIST)."
            choice "$a" "${claude_args[$i]}"
            i=$((i + 1)) ;;
        --output-format=* | --input-format=* | --permission-mode=*) choice "${a%%=*}" "${a#*=}" ;;
        -r | --resume)
            if [ "$i" -lt "$n_args" ]; then
                case ${claude_args[$i]} in
                    -*) ;;
                    *) resume_value "${claude_args[$i]}"; i=$((i + 1)) ;;
                esac
            fi ;;
        --resume=*) resume_value "${a#*=}" ;;
        --allowedTools | --allowed-tools | --disallowedTools | --disallowed-tools)
            [ "$i" -lt "$n_args" ] && case ${claude_args[$i]} in -*) false ;; *) true ;; esac ||
                die "$a needs at least one tool rule."
            multi=$a ;;
        --allowedTools=* | --allowed-tools=* | --disallowedTools=* | --disallowed-tools=*)
            tool_value "${a%%=*}" "${a#*=}" ;;
        --*=*) not_listed "${a%%=*}" ;;
        -*) not_listed "$a" ;;
        *) first_operand "$a" ;;
    esac
done

# Second layer, every mode (reference ledger S-B5 audit W2): the launcher owns settings
# and hooks, so no caller option may replace the settings, drop a hook or bypass
# permissions. Matched without case, as PowerShell's -match does; a prompt is checked too,
# as there. The allow-list above already refuses all of these; this stays in case the
# list is widened by mistake.
OWNED_RE='^--(settings|setting-sources|plugin-dir|(allow-)?dangerously-skip-permissions)(=|$)'
BYPASS_RE='^(--permission-mode=)?bypasspermissions$'
ALLOWED_RE='^--allowed-?tools(=(.*))?$'
WEB_RE='webfetch|websearch|mcp__'
in_allow=0
for a in ${claude_args[@]+"${claude_args[@]}"}; do
    l=$(lower "$a")
    if [[ $l =~ $OWNED_RE ]] || [[ $l =~ $BYPASS_RE ]]; then
        die "'$a' would replace the launcher's settings, hooks or permissions (ledger S-B5); leave it out."
    fi
    # --full with web access (reference ledger S-B5): --full drops the lean tool list and
    # keeps the empty MCP config, so nothing beside it may arm a web or MCP tool.
    if [ "$FULL" = 1 ]; then
        web=0
        case $l in --mcp-config | --mcp-config=*) web=1 ;; esac
        s=$l
        if [[ $l =~ $ALLOWED_RE ]]; then
            in_allow=1
            s=${BASH_REMATCH[2]}
        elif [ "${l:0:1}" = - ]; then
            in_allow=0
        fi
        if [ "$web" = 1 ] || { [ "$in_allow" = 1 ] && [[ $s =~ $WEB_RE ]]; }; then
            die "--full cannot be combined with web access ('$a'): --full drops the lean tool list (ledger S-B5). Leave out --full for a web run."
        fi
    fi
done

# config/lis.conf: parsed line by line, never sourced.
[ -f "$CONF" ] || die "config/lis.conf is missing."
CFG_PORT=; CFG_CTX=; CFG_CLIENT_CTX=
while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    case $line in '' | '#'*) continue ;; esac
    case $line in *=*) ;; *) continue ;; esac
    k=${line%%=*}
    v=${line#*=}
    case $k in
        PORT) CFG_PORT=$v ;;
        CTX) CFG_CTX=$v ;;
        CLIENT_CTX) CFG_CLIENT_CTX=$v ;;
    esac
done < "$CONF"

# The base URL is loopback by construction: a fixed http://127.0.0.1: and a PORT of 1 to
# 5 digits, no leading zero, at most 65535 (the only input, so no second URL check).
case $CFG_PORT in
    [1-9] | [1-9][0-9] | [1-9][0-9][0-9] | [1-9][0-9][0-9][0-9] | [1-9][0-9][0-9][0-9][0-9]) ;;
    *) die "config/lis.conf PORT '$CFG_PORT' is not a port number; http://127.0.0.1:<PORT> would not be a loopback URL." ;;
esac
[ "$CFG_PORT" -le 65535 ] || die "config/lis.conf PORT $CFG_PORT is above 65535."
BASE_URL="http://127.0.0.1:$CFG_PORT"
case $CFG_CTX in '' | *[!0-9]*) die "config/lis.conf CTX '$CFG_CTX' is not an integer." ;; esac
if [ -z "$CTX_TOKENS" ]; then
    case $CFG_CLIENT_CTX in '' | *[!0-9]*) die "config/lis.conf CLIENT_CTX '$CFG_CLIENT_CTX' is not an integer." ;; esac
    [ "${#CFG_CLIENT_CTX}" -le 9 ] && [ "$((10#$CFG_CLIENT_CTX))" -ge 1024 ] ||
        die "config/lis.conf CLIENT_CTX $CFG_CLIENT_CTX is not an integer from 1024."
    CTX_TOKENS=$((10#$CFG_CLIENT_CTX))
fi
if [ "${#CFG_CTX}" -le 9 ] && [ "$CTX_TOKENS" -gt $((10#$CFG_CTX - 16384)) ]; then
    say "warning: context tokens $CTX_TOKENS > CTX $CFG_CTX - 16384: a full prompt plus a full reply may not fit the server's window."
fi

# Tools (W-24b: a hook interpreter is never a PATH lookup at hook time). python3.13 from
# /opt/homebrew/bin, else /usr/local/bin (as serve.sh), curl from /usr/bin, claude from
# PATH. Each is used by its real file (symlinks resolved), and refused when the path as
# found, its directory, the real file or the real file's directory is writable by
# everyone or owned by anyone but root and the current user (a lighter check than
# serve.sh's: no walk to the prefix, no group-writable test).
# Test-only override (scripts/tests/claude_local_test.py; never set it otherwise):
#   LIS_TEST_CLIENT_BIN=<absolute dir>  python3.13, curl and claude are taken from that
#   dir instead (same checks), with a TEST OVERRIDE line on stderr. It was read into an
#   unexported variable and scrubbed at the top, so serve.sh and claude never see it.
MY_UID=$(id -u)
real_path() {    # $1 with every symlink resolved (at most 32 hops), as serve.sh does
    local p=$1 t d n=0
    while [ -L "$p" ]; do
        n=$((n + 1))
        [ "$n" -le 32 ] || return 1
        t=$(readlink "$p") || return 1
        case $t in /*) p=$t ;; *) p="${p%/*}/$t" ;; esac
    done
    d=${p%/*}
    [ -n "$d" ] || d=/
    d=$(cd -P "$d" && pwd -P) || return 1
    printf '%s/%s\n' "${d%/}" "${p##*/}"
}
trusted() {    # $1 = a path (not followed): owner root or me, not writable by everyone
    local info uid mode
    info=$(/usr/bin/stat -f '%u %Lp' "$1") || { WHY="cannot stat $1"; return 1; }
    uid=${info%% *}
    mode=${info##* }
    [ "$uid" = 0 ] || [ "$uid" = "$MY_UID" ] || { WHY="$1 is owned by uid $uid, not root or the current user"; return 1; }
    case ${mode: -1} in 2 | 3 | 6 | 7) WHY="$1 is writable by everyone (mode $mode)"; return 1 ;; esac
    return 0
}
TOOL=
check_tool() {    # $1 = name, $2 = path as found; sets TOOL to the checked real file
    local real p d
    case $2 in /*) ;; *) die "$1 resolves to '$2', not an absolute path." ;; esac
    [ -f "$2" ] && [ -x "$2" ] || die "$1 '$2' is not an executable file."
    real=$(real_path "$2") || die "cannot resolve $1 '$2'."
    for p in "$2" "$real"; do
        d=${p%/*}
        [ -n "$d" ] || d=/
        trusted "$p" && trusted "$d" || die "$1 refused: $WHY."
    done
    TOOL=$real
}
if [ -n "$TEST_BIN" ]; then
    case $TEST_BIN in /*) ;; *) die "LIS_TEST_CLIENT_BIN '$TEST_BIN' is not an absolute path." ;; esac
    say "TEST OVERRIDE: LIS_TEST_CLIENT_BIN=$TEST_BIN supplies python3.13, curl and claude (tests only; never set it otherwise)"
    PY_FOUND=$TEST_BIN/python3.13
    CURL_FOUND=$TEST_BIN/curl
    CLAUDE_FOUND=$TEST_BIN/claude
else
    PY_FOUND=
    for d in /opt/homebrew/bin /usr/local/bin; do
        if [ -e "$d/python3.13" ]; then PY_FOUND=$d/python3.13; break; fi
    done
    [ -n "$PY_FOUND" ] || die "python3.13 is not in /opt/homebrew/bin or /usr/local/bin; the hooks and the settings builder need it."
    CURL_FOUND=/usr/bin/curl
    CLAUDE_FOUND=
    [ "$PRINT_ARGS" = 1 ] || CLAUDE_FOUND=$(command -v claude) || die "claude is not on PATH."
fi
check_tool python3.13 "$PY_FOUND"; PY=$TOOL
check_tool curl "$CURL_FOUND"; CURL=$TOOL
# The session shell gets the same checks (absolute, a regular executable file, the path,
# its real file and both directories owned by root or me and not writable by everyone).
# Not taken from the test override: always the fixed path.
check_tool 'bash (the session shell)' "$SESSION_SHELL"
CLAUDE=
if [ "$PRINT_ARGS" = 0 ]; then
    check_tool claude "$CLAUDE_FOUND"; CLAUDE=$TOOL
fi

# The working directory's project settings (claude reads <cwd>/.claude/settings.json and
# settings.local.json in every mode here). Whether the --settings file outranks them key
# by key is UNVERIFIED on this client, so a project file that could re-route the key,
# switch hooks off, add hooks, widen the permission mode, change the session shell (env
# CLAUDE_CODE_SHELL, SHELL, CLAUDE_CODE_SHELL_PREFIX, CLAUDE_ENV_FILE) or plant code in
# its start-up (SHELL_ENV below, any BASH_FUNC_ name) refuses the launch. Parsed as
# JSON; an unreadable or unparseable file refuses too. Names only, never a value.
IFS= read -r -d '' PROJECT_PY <<'PYEOF'
import json
import os

MODES = {'manual', 'acceptEdits', 'plan', 'default'}
PROXY = {'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'}
# The session shell, what the client runs around or before each shell command, and what
# bash 3.2 runs or reads at start-up or around a command (man bash): start-up files
# (BASH_ENV, ENV, HOME for ~/.bash_profile and ~/.bashrc, POSIXLY_CORRECT, INPUTRC),
# options (SHELLOPTS, BASHOPTS), code in prompts and traces (PS1, PS2, PS4,
# PROMPT_COMMAND) and exported functions (any BASH_FUNC_ name).
SHELL_ENV = {'CLAUDE_CODE_SHELL', 'SHELL', 'CLAUDE_CODE_SHELL_PREFIX', 'CLAUDE_ENV_FILE',
             'BASH_ENV', 'ENV', 'HOME', 'POSIXLY_CORRECT', 'INPUTRC', 'SHELLOPTS', 'BASHOPTS',
             'PS1', 'PS2', 'PS4', 'PROMPT_COMMAND'}
found = []
for name in ('settings.json', 'settings.local.json'):
    path = os.path.join(os.getcwd(), '.claude', name)
    rel = '.claude/' + name
    if not os.path.lexists(path):
        continue
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        found.append(f'{rel}: cannot be read as JSON ({type(e).__name__})')
        continue
    if not isinstance(d, dict):
        found.append(f'{rel}: the top level is not an object')
        continue
    for key in ('apiKeyHelper', 'disableAllHooks', 'hooks'):
        if key in d:
            found.append(f'{rel}: key {key}')
    perms = d.get('permissions')
    if isinstance(perms, dict) and 'defaultMode' in perms:
        m = perms['defaultMode']
        if not (isinstance(m, str) and m in MODES):
            found.append(f'{rel}: key permissions.defaultMode (only manual, acceptEdits, plan, default)')
    elif 'permissions' in d and not isinstance(perms, dict):
        found.append(f'{rel}: key permissions is not an object')
    env = d.get('env')
    if isinstance(env, dict):
        for k in env:
            # compared without surrounding whitespace (whether the client trims a key is
            # UNVERIFIED) and without case; the message shows the key as written
            n = k.strip().upper()
            if (n.startswith(('ANTHROPIC_', 'CLAUDE_CODE_', 'BASH_FUNC_')) or n.lower() in PROXY
                    or n in SHELL_ENV):
                found.append(f'{rel}: key env.{json.dumps(k)[1:-1]}')
    elif 'env' in d:
        found.append(f'{rel}: key env is not an object')
for line in found:
    print(line)
PYEOF
project=$("$PY" -I -c "$PROJECT_PY") || die "python3.13 could not check the working directory's .claude settings."
if [ -n "$project" ]; then
    die "the working directory's project settings would override the launcher (${project//$NL/; }); run from another directory or remove those keys."
fi
[ -f "$SETTINGS_SRC" ] || die "config/claude-settings.json is missing."
HOOKS=()
if [ "$NO_GUARD" = 0 ]; then
    HOOKS+=("$HOOKS_SRC/lis-guard.py")
    [ "$NO_LOOP_GUARD" = 1 ] || HOOKS+=("$HOOKS_SRC/loop-guard.py" "$HOOKS_SRC/repeat-guard.py")
    # verify-doc is registered only where the file exists (not yet in this port).
    [ -f "$VERIFY_DOC" ] && HOOKS+=("$VERIFY_DOC")
fi
for h in ${HOOKS[@]+"${HOOKS[@]}"}; do
    [ -f "$h" ] || die "hook '$h' is missing."
done
TMP_ROOT=${TMPDIR:-/tmp}
TMP_ROOT=${TMP_ROOT%/}
[ -d "$TMP_ROOT" ] || die "the temp dir '$TMP_ROOT' does not exist."
# The hook commands are shell lines: a path with a quote, $, backquote, backslash or
# newline in it would change them.
for p in "$ROOT" "$TMP_ROOT" "$PY" "$HOME"; do
    case $p in *'"'* | *'$'* | *'`'* | *'\'* | *"$NL"*)
        die "the path '$p' holds a quote, \$, backquote, backslash or newline; refusing to build hook commands from it." ;;
    esac
done
CTX_NOTE=
[ "$CTX_TOKENS" = "$CFG_CLIENT_CTX" ] || CTX_NOTE=" (--context-tokens)"

# The policy text (reference ruling 13 and 15, TEST 6): the one-model and loop-guard
# blocks verbatim; the evidence block without its Windows parts (PowerShell quoting,
# Measure-Object, the nvidia-smi/wsl/tasklist machine survey).
commas() {
    local n=$1 out=
    while [ "${#n}" -gt 3 ]; do
        out=",${n: -3}$out"
        n=${n:0:${#n}-3}
    done
    printf '%s%s' "$n" "$out"
}
TOOLS='Bash,Read,Edit,Write,Glob,Grep,Skill'
POLICY="This session runs on one local model served by llama-server on this machine. You are the only agent: do all work directly with your own tools and never spawn subagents, agent teams or workflows. Ignore every instruction about Fable, Opus, Sonnet or other model roles, orchestrator and subagent splits, or delegating implementation, tests or audits to a subagent; those rules apply to cloud sessions only. Sessions here may run long and unsupervised, so before any destructive or irreversible action (deleting files, git reset or force-push, stopping processes, editing config outside the working repo) verify the exact target first and prefer the reversible option. Keep answers short: the context window is $(commas "$((10#$CFG_CTX))") tokens."
POLICY+=" Evidence: copy every number in a report from a tool output; get counts with wc -l, never by counting a listing yourself. Read a script before citing its output, and prefer a direct check over a helper script. Never append 2>/dev/null to a probe: a hidden error becomes a false claim. Mark anything you did not verify as UNVERIFIED."
POLICY+=" Plain reads may run in parallel; per Bash batch run at most one command that executes a program, writes, or changes state. If a command is rejected because its permission check timed out, retry it once; if that also fails, mark the figure UNVERIFIED and move on. Do no checks the task does not need: no re-runs on other platforms, no side investigations. For a file over 300 lines, grep it or read a line range, not the whole file."

# The settings builder (reference: the $cfg block of claude-local.ps1). Arguments, in
# order: source, output ('-' = stdout), context tokens, session start, home, repo, run
# dir as created, run dir resolved, python, guard on (1/0), loop guards on (1/0), the
# base URL, the session shell, the hook copy paths (name=path ...). Nothing secret is passed.
IFS= read -r -d '' OVERLAY_PY <<'PYEOF'
import json
import os
import sys

(src, out, ctx, start, home, repo, run_given, run_real, py, guard, loop, base, shell) = sys.argv[1:14]
copies = dict(a.split('=', 1) for a in sys.argv[14:])
with open(src, encoding='utf-8') as f:
    cfg = json.load(f)


def rule_root(p):
    """An absolute path as a permission-rule root: /Users/x -> //Users/x."""
    return '/' + p.rstrip('/')


# Pinned at flag level (C): the loopback base URL (the process env's value), the model
# pins and hooks on. The key is never written here; it stays in the process env only.
cfg.setdefault('env', {})
cfg['env']['ANTHROPIC_BASE_URL'] = base
for k in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
          'ANTHROPIC_DEFAULT_HAIKU_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL'):
    cfg['env'][k] = 'local'
cfg['model'] = 'local'
cfg['disableAllHooks'] = False
cfg.pop('apiKeyHelper', None)
cfg['env']['CLAUDE_CODE_AUTO_COMPACT_WINDOW'] = ctx
cfg['env']['LIS_SESSION_START'] = start
# The session shell, as in the process env (a settings env value replaces the process one,
# so this file holds the same value; documented to outrank user-scope settings, UNVERIFIED).
cfg['env']['CLAUDE_CODE_SHELL'] = shell
cfg['env']['SHELL'] = shell
perms = cfg.setdefault('permissions', {})
hr = rule_root(home)
for kind in ('allow', 'ask', 'deny'):
    if perms.get(kind):
        perms[kind] = [r.replace('Edit(~/', f'Edit({hr}/').replace('Read(~/', f'Read({hr}/')
                       for r in perms[kind]]
rr = rule_root(repo)
deny = list(perms.get('deny') or [])
deny += [f'Edit({rr}/{g})' for g in (
    'scripts/hooks/**', 'scripts/verify-doc.py', 'scripts/serve.sh', 'scripts/stop.sh',
    'scripts/smoke.sh', 'scripts/claude-local.sh', 'bench/run.sh', 'config/**', 'bin/**',
    '.gitignore', '.gitattributes', 'logs/api-key', 'logs/.stop-requested*',
    'logs/llama-server.pid', 'logs/serve.lock/**', 'logs/model-verify.txt',
    'scripts/tests/claude_local_test.py', 'scripts/tests/serve_test.py',
    'scripts/tests/stop_test.py', 'scripts/statusline.mjs')]
deny += [f'Edit({rr}/models/manifest.json)']
deny += ['Edit(**/.claude/settings*.json)']
hooks = cfg.get('hooks') or {}


def add(event, entry):
    hooks[event] = [e for e in (hooks.get(event) or []) if e] + [entry]


if guard == '1':
    add('PreToolUse', {'matcher': 'Bash', 'hooks': [{
        'type': 'command',
        'command': f'"{py}" -I "{copies["lis-guard.py"]}" --hooks-dir "{repo}/scripts/hooks"',
        'timeout': 10, 'onFailure': 'block'}]})
    if loop == '1':
        add('PreToolUse', {'matcher': '*', 'hooks': [{
            'type': 'command', 'command': f'"{py}" -I "{copies["loop-guard.py"]}"', 'timeout': 5}]})
        for ev in ('PostToolUse', 'PostToolUseFailure', 'Stop', 'SubagentStop'):
            add(ev, {'hooks': [{'type': 'command',
                                'command': f'"{py}" -I "{copies["repeat-guard.py"]}"',
                                'timeout': 10}]})
    if 'verify-doc.py' in copies:
        add('PostToolUse', {'matcher': 'Write|Edit', 'hooks': [{
            'type': 'command',
            'command': f'"{py}" -I "{copies["verify-doc.py"]}" --hook --script "{repo}/scripts/verify-doc.py"',
            'timeout': 30}]})
if hooks:
    cfg['hooks'] = hooks
# The run dir (hook copies and these settings): no Edit (Write included), by its path as
# created and as resolved (/var is a link to /private/var), or at any depth.
for d in dict.fromkeys((run_given, run_real)):
    deny.append(f'Edit({rule_root(d)}/**)')
deny.append('Edit(//**/lis-hooks-*/**)')
perms['deny'] = deny
text = json.dumps(cfg, indent=2) + '\n'
if out == '-':
    sys.stdout.write(text)
else:
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
PYEOF

SESSION_START=$("$PY" -I -c 'import datetime; print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))') ||
    die "python3.13 could not produce the session start time."
[[ $SESSION_START =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$ ]] ||
    die "unexpected session start '$SESSION_START'."
STAMP=$(date +%Y%m%d-%H%M%S)

# The lean arguments. --tools and --mcp-config take several values, so each is followed by
# another option, never by the caller's arguments (also under --full, where the
# reference puts --mcp-config last).
# The default turn cap goes last in this block: after the multi-value options, before
# the caller's arguments (so never after a caller's `--`).
build_lean() {    # $1 = MCP config path
    if [ "$FULL" = 1 ]; then
        LEAN=(--mcp-config "$1" --strict-mcp-config)
    else
        LEAN=(--tools "$TOOLS" --strict-mcp-config --mcp-config "$1" --append-system-prompt "$POLICY")
    fi
    if [ "$FULL" = 0 ] && [ "$USER_SCOPE" = 0 ]; then
        LEAN=(--setting-sources project,local "${LEAN[@]}")
    fi
    if [ "$ADD_MAX_TURNS" = 1 ]; then
        LEAN+=(--max-turns "$MAX_TURNS_DEFAULT")
    fi
}
ADD_MAX_TURNS=0
TURNS_NOTE=
if [ "$PRINT_MODE" = 1 ] && [ -z "$MAX_TURNS" ]; then
    ADD_MAX_TURNS=1
    TURNS_NOTE="print mode without --max-turns: added --max-turns $MAX_TURNS_DEFAULT (caps turns, not the time one turn takes)"
fi
if [ "$FULL" = 1 ]; then SCOPE_NOTE='on (--full)'
elif [ "$USER_SCOPE" = 1 ]; then SCOPE_NOTE='on (--user-scope)'
else SCOPE_NOTE='off (S-O3)'; fi
if [ "$NO_GUARD" = 1 ]; then GUARD_NOTE=off
elif [ "$NO_LOOP_GUARD" = 1 ]; then GUARD_NOTE='on, loop-guard+repeat-guard off'
else GUARD_NOTE=on; fi
SHELL_NOTE="session shell: $SESSION_SHELL (pinned as CLAUDE_CODE_SHELL and SHELL)"

copy_args() {    # $1 = run dir: name=path for each hook copy
    COPY_ARGS=()
    for h in ${HOOKS[@]+"${HOOKS[@]}"}; do
        COPY_ARGS+=("$(basename "$h")=$1/$(basename "$h")")
    done
}

if [ "$PRINT_ARGS" = 1 ]; then
    run="$TMP_ROOT/lis-hooks-$STAMP-$$-0"    # placeholder: a real run adds random digits
    build_lean "$run/lis-mcp-$$.json"
    copy_args "$run"
    say "print only: no server contact, no key read, nothing written, claude not started"
    say "user scope: $SCOPE_NOTE   guard: $GUARD_NOTE   context tokens: $CTX_TOKENS$CTX_NOTE"
    say "$SHELL_NOTE"
    [ -z "$TURNS_NOTE" ] || say "$TURNS_NOTE"
    printf '%s\n' 'claude arguments (JSON array, in order):'
    "$PY" -I -c 'import json, sys; print(json.dumps(sys.argv[1:], indent=2))' \
        --settings "$run/lis-settings-$$.json" "${LEAN[@]}" ${claude_args[@]+"${claude_args[@]}"} ||
        die "could not print the arguments."
    printf '%s\n' 'settings JSON (the --settings file):'
    "$PY" -I -c "$OVERLAY_PY" "$SETTINGS_SRC" - "$CTX_TOKENS" "$SESSION_START" "$HOME" "$ROOT" \
        "$run" "$run" "$PY" "$([ "$NO_GUARD" = 0 ] && echo 1 || echo 0)" \
        "$([ "$NO_LOOP_GUARD" = 0 ] && echo 1 || echo 0)" "$BASE_URL" "$SESSION_SHELL" \
        ${COPY_ARGS[@]+"${COPY_ARGS[@]}"} ||
        die "could not build the settings."
    printf '%s\n' 'MCP config JSON (the --mcp-config file):' '{"mcpServers":{}}'
    exit 0
fi

# ---------------------------------------------------------------- 2. server start
HEALTH_ERR=
health_ok() {
    local body re='"status"[[:space:]]*:[[:space:]]*"ok"'
    if body=$("$CURL" -q -sS --max-time 10 --noproxy '*' "$BASE_URL/health" 2>&1); then
        [[ $body =~ $re ]] && return 0
        HEALTH_ERR="answer: ${body:0:200}"
    else
        HEALTH_ERR=${body:0:200}
    fi
    return 1
}
if [ "$START_SERVER" = 1 ] && ! health_ok; then
    [ -x "$SERVE" ] || die "llama-server at $BASE_URL is not answering /health and scripts/serve.sh is missing or not executable."
    say "server down ($HEALTH_ERR); starting scripts/serve.sh --detach"
    "$SERVE" --detach >&2 || die "scripts/serve.sh --detach failed (see its output above and logs/)."
fi

# ---------------------------------------------------------------- 3. key read
# The per-start key from scripts/serve.sh, else the reference's old fixed key 'local'.
KEY=
if [ -f "$KEY_FILE" ]; then
    IFS= read -r KEY < "$KEY_FILE" || [ -n "$KEY" ] || KEY=
    KEY=${KEY//[[:space:]]/}
fi
[ -n "$KEY" ] || KEY=local
# The key goes into a curl config line on stdin: only characters a key has.
[[ $KEY =~ ^[A-Za-z0-9._~+/=-]+$ ]] || die "logs/api-key holds characters a server key never has; restart the server with scripts/stop.sh and scripts/serve.sh."

# ---------------------------------------------------------------- 4. preflight
health_ok || die "llama-server at $BASE_URL is not answering /health with status ok ($HEALTH_ERR). Start it: scripts/serve.sh --detach (or pass --start-server)."
slots=$(printf 'header = "Authorization: Bearer %s"\n' "$KEY" |
    "$CURL" -q -sS --fail --max-time 10 --noproxy '*' -K - "$BASE_URL/slots" 2>&1) ||
    die "llama-server at $BASE_URL refused /slots (${slots:0:200}); a 401 means logs/api-key does not match the running server: restart it with scripts/stop.sh and scripts/serve.sh."
SLOTS_PY='import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(2)
if not isinstance(d, list):
    sys.exit(2)
busy = [s for s in d if isinstance(s, dict) and s.get("is_processing")]
if busy:
    print(busy[0].get("id", "?"))'
busy=$(printf '%s' "$slots" | "$PY" -I -c "$SLOTS_PY") || die "llama-server at $BASE_URL answered /slots with something that is not a slot list."
if [ -n "$busy" ]; then
    msg="server slot $busy is processing: another session is using the server"
    [ "$STRICT_SLOT" = 0 ] || die "$msg (--strict-slot)."
    say "warning: $msg; this session queues behind it."
fi

# ---------------------------------------------------------------- 5. per-run files
# lis-hooks-<stamp>-<pid>-<random digits>: digits and dashes only after lis-hooks-, the
# shape lis-guard's per-run rules expect. One mkdir -m 700 that fails when the path
# exists (no -p), so a planted dir or link is never reused; a new suffix on a collision.
RUN_DIR=
mk_err=
for try in 1 2 3 4 5; do
    rnd=$(od -An -N8 -tu8 /dev/urandom | tr -d ' \n')
    [[ $rnd =~ ^[0-9]+$ ]] || die "could not read random digits from /dev/urandom."
    cand="$TMP_ROOT/lis-hooks-$STAMP-$$-$rnd"
    if mk_err=$(mkdir -m 700 "$cand" 2>&1); then
        RUN_DIR=$cand
        break
    fi
done
[ -n "$RUN_DIR" ] || die "could not create a new run dir in '$TMP_ROOT' after 5 tries (${mk_err:0:200})."
RUN_REAL=$(cd "$RUN_DIR" && pwd -P) || die "cannot resolve '$RUN_DIR'."
case $RUN_REAL in *'"'* | *'$'* | *'`'* | *'\'* | *"$NL"*) die "the run dir '$RUN_REAL' holds a character a hook command cannot carry." ;; esac
chmod 700 "$RUN_DIR" || die "chmod 700 '$RUN_DIR' failed."
SETTINGS="$RUN_REAL/lis-settings-$$.json"
MCP="$RUN_REAL/lis-mcp-$$.json"
cleanup() { rm -f "$SETTINGS" "$MCP"; }
trap cleanup EXIT
for h in ${HOOKS[@]+"${HOOKS[@]}"}; do
    dst="$RUN_REAL/$(basename "$h")"
    cp "$h" "$dst" && chmod 444 "$dst" || die "copying '$h' to the run dir failed."
done
copy_args "$RUN_REAL"
"$PY" -I -c "$OVERLAY_PY" "$SETTINGS_SRC" "$SETTINGS" "$CTX_TOKENS" "$SESSION_START" "$HOME" \
    "$ROOT" "$RUN_DIR" "$RUN_REAL" "$PY" "$([ "$NO_GUARD" = 0 ] && echo 1 || echo 0)" \
    "$([ "$NO_LOOP_GUARD" = 0 ] && echo 1 || echo 0)" "$BASE_URL" "$SESSION_SHELL" \
    ${COPY_ARGS[@]+"${COPY_ARGS[@]}"} ||
    die "could not build the per-run settings."
printf '%s\n' '{"mcpServers":{}}' > "$MCP" || die "could not write the MCP config."
chmod 400 "$SETTINGS" "$MCP" || die "chmod 400 of the per-run settings failed."
build_lean "$MCP"

say "hook copies: $RUN_REAL (read-only, kept after exit)"
say "user scope: $SCOPE_NOTE"
say "profile: none   guard: $GUARD_NOTE   context tokens: $CTX_TOKENS$CTX_NOTE"
say "$SHELL_NOTE"
[ -z "$TURNS_NOTE" ] || say "$TURNS_NOTE"

# ---------------------------------------------------------------- 6. env and launch
# The inherited env was scrubbed at the top (the environment step); only the block is set.
export ANTHROPIC_BASE_URL="$BASE_URL"
export ANTHROPIC_AUTH_TOKEN="$KEY"
export ANTHROPIC_MODEL=local
export ANTHROPIC_DEFAULT_OPUS_MODEL=local
export ANTHROPIC_DEFAULT_SONNET_MODEL=local
export ANTHROPIC_DEFAULT_HAIKU_MODEL=local
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="$CTX_TOKENS"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="$CTX_TOKENS"
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384
export API_TIMEOUT_MS=1800000
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export DISABLE_PROMPT_CACHING=1
export ENABLE_TOOL_SEARCH=false
export CLAUDE_CODE_ATTRIBUTION_HEADER=0
export LIS_SESSION_START="$SESSION_START"
export CLAUDE_CODE_SHELL="$SESSION_SHELL"
export SHELL="$SESSION_SHELL"
KEY=

# A child, not exec, so the EXIT trap removes the settings and MCP files afterwards.
"$CLAUDE" --settings "$SETTINGS" "${LEAN[@]}" ${claude_args[@]+"${claude_args[@]}"}
exit $?
