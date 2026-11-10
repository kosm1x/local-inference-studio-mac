#!/bin/bash
set +xv  # first: an inherited SHELLOPTS=xtrace/verbose would print the API key to stderr
# serve.sh - run llama-server (Anthropic /v1/messages on 127.0.0.1) for this checkout.
#
# macOS port of scripts/serve.ps1 from the Windows reference (local-inference-studio,
# commit cafc34b). Usage:
#
#   scripts/serve.sh [--detach | --dry-run] [--wait SECONDS] [--reasoning off|on|auto]
#                    [-- EXTRA_LLAMA_SERVER_ARGS...]
#
#   (default)   foreground: llama-server runs as this script's child; Ctrl-C (forwarded
#               as SIGTERM) or scripts/stop.sh stops it. The exit status is llama-server's
#               own, also after a forwarded signal: the child is reaped and its status
#               returned (128+N when a signal N ended it).
#   --detach    background: returns 0 only after GET /health answers 200, non-zero with
#               the log tail otherwise (a server still loading after --wait seconds is
#               left running and reported; scripts/stop.sh stops it). INT, TERM or HUP
#               to serve.sh while it waits for /health stops the start: SIGTERM is
#               forwarded to the llama-server child (one validated pid), the child is
#               waited for and reaped, the pid file and lock are removed, and serve.sh
#               exits 128+N (130, 143 or 129) with a line saying so. An interrupted start
#               leaves no server behind; only a child that ignores SIGTERM for 30 s is
#               left running, with its pid file and lock kept for scripts/stop.sh.
#   --dry-run   prints the exact llama-server command line on stdout and exits 0. Reads
#               config/lis.conf and models/manifest.json; starts nothing, writes nothing,
#               takes no lock and does not need the model file to exist.
#   --wait N    --detach health timeout in seconds (default 300).
#   --reasoning passed as --reasoning (default off, as in serve.ps1: the 2026-10-26
#               compaction bench on the reference machine got a real summary on every
#               compaction request only with server thinking off).
#   -- ...      extra llama-server arguments, one per argv element, appended after the
#               fixed ones. Each must pass the ALLOW-LIST below (check_args).
#
# Settings come from config/lis.conf (PORT, CTX, KV_TYPE, MODEL_ID, CHAT_TEMPLATE),
# which is parsed line by line and never sourced. The model is the models/manifest.json
# entry whose "id" equals MODEL_ID; its path is repo-relative. The entry may carry
# per-model settings (server_args, sampling, kv_type; see PY_MANIFEST and
# models/README.md); server_args pass the same allow-list as the -- arguments.
#
# Safety properties (the first five kept from serve.ps1):
#   - bind 127.0.0.1 only (--host is fixed; no extra argument can set it);
#   - a fresh random key per start (32 random bytes as 64 hex characters) written to
#     logs/api-key (mode 600, logs/ mode 700) before llama-server starts, passed with
#     --api-key-file, so it is in neither the command line nor the log;
#   - extra arguments (-- and server_args) pass an allow-list of performance, memory,
#     fit and log-level flags with validated values; the serve.ps1 refusal list
#     (FIXED_FLAG_RE) and this build's additions (MAC_FLAG_RE) still apply after it;
#   - one server per checkout: an mkdir lock (logs/serve.lock) plus the pid file
#     logs/llama-server.pid (macOS has no flock(1)); a stale lock is reclaimed;
#   - the model's size and sha256 (shasum -a 256) are checked against the manifest at
#     most once per boot (marker logs/model-verify.txt keyed on kern.boottime, path,
#     size, mtime and the manifest sha256), as serve.ps1 did with model-verify.json;
#   - inherited LLAMA_*, GGML_*, DYLD_*, HF_*, HUGGING_FACE_*, MODEL_ENDPOINT, PERL5*,
#     MTL_*, Malloc* and proxy variables are removed before anything runs (serve.ps1
#     removed a named LLAMA_ARG_ subset);
#   - --offline is always passed (its --help: "prevents network access"; see CMD);
#   - PATH is reset to the system directories; llama-server and python3.13 are taken from
#     /opt/homebrew/bin, else /usr/local/bin, never from PATH, and refused unless their
#     real file and every directory up to that prefix is owned by root or the current
#     user and not writable by anyone else, by mode or by ACL (check_trusted);
#   - the model and the chat template must resolve physically inside models/ and
#     config/ (no symlinked final component, no symlink leading out);
#   - free memory is logged before the start (vm_stat + sysctl), the way serve.ps1 logged
#     free VRAM, because a fit is judged against what is actually free.
# The server log is logs/server-<UTC stamp>.log; its first line is the full command line.
#
# Test-only overrides (scripts/tests/serve_test.py; never set them otherwise). Each one
# prints a "TEST OVERRIDE" line on stderr and into the server log when used:
#   LIS_TEST_LLAMA_SERVER  absolute path of the program to run instead of the installed
#                          llama-server (it must still be owned by root or the current
#                          user and not writable by others, as must its directory);
#   LIS_TEST_LLAMA_EXE     the executable path the process check expects (default: the
#                          real path of the program run), for tests whose stand-in
#                          process runs another executable (a script's interpreter).
#
# UNVERIFIED on this machine (no model file exists yet): a real llama-server start with
# these flags and a model, and a clean exit on SIGTERM after the model has loaded (a
# SIGTERM during the load did end it, measured 2026-11-10).
set -u
umask 077
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd -P) || exit 1
ROOT=$(cd "$SCRIPT_DIR/.." && pwd -P) || exit 1
CONF="$ROOT/config/lis.conf"
MANIFEST="$ROOT/models/manifest.json"
LOGS="$ROOT/logs"
KEYFILE="$LOGS/api-key"
PIDFILE="$LOGS/llama-server.pid"
LOCKDIR="$LOGS/serve.lock"
MARKER="$LOGS/model-verify.txt"

say() { printf 'serve.sh: %s\n' "$*" >&2; }
die() { say "$*"; exit 1; }
usage() {
    say "usage: scripts/serve.sh [--detach | --dry-run] [--wait SECONDS] [--reasoning off|on|auto] [-- EXTRA_ARGS...]"
    exit 2
}

# ---------------------------------------------------------------- environment
# Cleared first, before any program runs:
#   LLAMA_*        llama-server reads every flag from LLAMA_ARG_* (which would get round
#                  the allow-list) and LLAMA_API_KEY / LLAMA_CACHE / LLAMA_* settings;
#   GGML_*         ggml backend selection and loading (GGML_BACKEND_PATH loads a library);
#   DYLD_*         dynamic-loader injection (SIP already strips these when /bin/bash
#                  starts; cleared here for any other bash that runs this file);
#   HF_*, HUGGING_FACE_*, MODEL_ENDPOINT   Hugging Face endpoints, tokens and caches;
#   *_proxy, *_PROXY   network proxies (http, https, all, no, ftp, socks...);
#   PERL5*, PERLLIB    shasum (the model check) is a perl script;
#   Malloc*        libmalloc debugging, documented in man malloc(3) on this machine:
#                  MallocStackLogging/MallocStackLoggingDirectory record stack logs to
#                  files, MallocDebugReport redirects malloc's messages, MallocScribble,
#                  MallocGuardEdges and MallocCheckHeap* change allocator behaviour;
#   MTL_*          Metal runtime switches. The names MTL_LOG_TO_STDERR, MTL_DEBUG_LAYER,
#                  MTL_SHADER_VALIDATION, MTL_CAPTURE_ENABLED and MTL_HUD_ENABLED are in
#                  this machine's system dyld shared cache (the Metal framework); there is
#                  no local man page, and what each does (stderr logging, validation
#                  layers, GPU trace capture, the HUD) is from Apple's developer
#                  documentation: UNVERIFIED here. llama.cpp's own libggml-metal reads no
#                  MTL_ name (its GGML_METAL_* switches are cleared with GGML_*).
# A name the shell cannot unset (not a valid identifier) survives and the post-check
# refuses the start.
cleared=""
set -f
for v in $(compgen -e); do
    case $v in
        LLAMA_*|GGML_*|DYLD_*|HF_*|HUGGING_FACE_*|MODEL_ENDPOINT|PERL5*|PERLLIB|MTL_*|Malloc*|*_proxy|*_PROXY)
            unset "$v" 2>/dev/null
            cleared="$cleared${cleared:+, }$v"
            ;;
    esac
done
set +f
if env | grep -Eq '^(LLAMA_|GGML_|DYLD_|HF_|HUGGING_FACE_|MODEL_ENDPOINT=|PERL5|PERLLIB=|MTL_|Malloc|[^=]*_(proxy|PROXY)=)'; then
    die "an inherited LLAMA_/GGML_/DYLD_/HF_/MTL_/Malloc/proxy variable survived clearing; refusing to start"
fi
n_cleared=0
[ -n "$cleared" ] && n_cleared=$(printf '%s\n' "$cleared" | tr ',' '\n' | grep -c .)
CLEARED_MSG=""
[ -n "$cleared" ] && CLEARED_MSG="cleared $n_cleared inherited environment variables ($cleared)"

# ---------------------------------------------------------------- arguments
MODE=foreground
WAIT_SEC=300
REASONING=off
EXTRA=()
while [ $# -gt 0 ]; do
    case $1 in
        --detach) [ "$MODE" = foreground ] || usage; MODE=detach ;;
        --dry-run) [ "$MODE" = foreground ] || usage; MODE=dry-run ;;
        --wait)
            [ $# -ge 2 ] || usage
            WAIT_SEC=$2
            shift
            ;;
        --reasoning)
            [ $# -ge 2 ] || usage
            REASONING=$2
            shift
            ;;
        --) shift; EXTRA=("$@"); break ;;
        -h|--help) usage ;;
        *) say "unknown argument: $1"; usage ;;
    esac
    shift
done
re_wait='^[1-9][0-9]{0,4}$'
[[ $WAIT_SEC =~ $re_wait ]] || die "--wait must be a whole number of seconds (1-99999), got '$WAIT_SEC'"
case $REASONING in off|on|auto) ;; *) die "--reasoning must be off, on or auto, got '$REASONING'" ;; esac

# ---------------------------------------------------------------- extra arguments
# ALLOW-LIST. Extra arguments (the -- ones and a manifest entry's server_args) may only
# be these flags of this build (llama-server 0.9.0, build 12110, commit cd959bae9; each
# spelling and alias read from its --help on 2026-11-10 and parsed by the binary with
# --version), each with the value kind it takes:
#   none       no value;
#   count      a decimal integer 0-9999999 (no sign, no leading zero);
#   layers     a count, auto or all;
#   on-off     on or off (--fit has an optional value in the binary; here it is required,
#              so it can never swallow the next argument);
#   mib-list   counts separated by commas (1 to 16 of them);
#   load-mode  auto, none, mmap, mlock, mmap+mlock or dio;
#   percent    0-100;
#   verbosity  0-3 (generic, error, warning, info; 4 trace and 5 debug are refused, as
#              are -v/--verbose/--log-verbose).
# No value kind admits a leading "-", so a value can never be read as a flag. Left out on
# purpose: anything that loads weights or other files (-m, -md, --lora, --mmproj,
# --control-vector, -lcs, --grammar-file, -jf...), anything network, TLS or CORS,
# anything that writes a path, anything that runs a program, the fixed flags of the
# command line below, and the sampling and template flags (the manifest sets those).
# ---- ALLOWED_FLAGS (begin) ----
allowed_kind() {
    case $1 in
        -t|--threads|-tb|--threads-batch|-b|--batch-size|-ub|--ubatch-size) echo count ;;
        --cache-reuse|-ctxcp|--ctx-checkpoints|--swa-checkpoints|-cms|--checkpoint-min-step|-cram|--cache-ram) echo count ;;
        -ncmoe|--n-cpu-moe|-fitc|--fit-ctx|--threads-http) echo count ;;
        -ngl|--gpu-layers|--n-gpu-layers) echo layers ;;
        -fit|--fit) echo on-off ;;
        -fitt|--fit-target) echo mib-list ;;
        -lm|--load-mode) echo load-mode ;;
        --poll) echo percent ;;
        -lv|--verbosity|--log-verbosity) echo verbosity ;;
        --swa-full|-cmoe|--cpu-moe|--warmup|--no-warmup) echo none ;;
        -kvu|--kv-unified|-no-kvu|--no-kv-unified|-kvo|--kv-offload|-nkvo|--no-kv-offload) echo none ;;
        --repack|-nr|--no-repack|--op-offload|--no-op-offload|--cache-idle-slots|--no-cache-idle-slots) echo none ;;
        --log-timestamps|--no-log-timestamps|--log-prefix|--no-log-prefix) echo none ;;
        *) return 1 ;;
    esac
}
# ---- ALLOWED_FLAGS (end) ----
value_ok() {  # value_ok KIND VALUE
    local re_count='^(0|[1-9][0-9]{0,6})$' re_list='^(0|[1-9][0-9]{0,6})(,(0|[1-9][0-9]{0,6})){0,15}$'
    local re_pct='^(100|[1-9]?[0-9])$' re_lv='^[0-3]$'
    case $1 in
        count) [[ $2 =~ $re_count ]] ;;
        layers) case $2 in auto|all) return 0 ;; esac; [[ $2 =~ $re_count ]] ;;
        on-off) case $2 in on|off) return 0 ;; *) return 1 ;; esac ;;
        mib-list) [[ $2 =~ $re_list ]] ;;
        load-mode) case $2 in auto|none|mmap|mlock|mmap+mlock|dio) return 0 ;; *) return 1 ;; esac ;;
        percent) [[ $2 =~ $re_pct ]] ;;
        verbosity) [[ $2 =~ $re_lv ]] ;;
        *) return 1 ;;
    esac
}
kind_text() {
    case $1 in
        count) echo "a whole number 0-9999999" ;;
        layers) echo "a whole number, auto or all" ;;
        on-off) echo "on or off" ;;
        mib-list) echo "whole numbers separated by commas" ;;
        load-mode) echo "auto, none, mmap, mlock, mmap+mlock or dio" ;;
        percent) echo "a whole number 0-100" ;;
        verbosity) echo "0, 1, 2 or 3" ;;
    esac
}

# REFUSAL LISTS, kept after the allow-list as a second, independent gate (a flag on them
# is refused even if it is ever added to the allow-list), and used to say why a flag is
# refused. Matched case-insensitively against the flag name after normalisation.
#
# Ported verbatim from serve.ps1 ($fixedFlag, reference commit cafc34b, lines 183-185;
# security audit B6/B7): the loopback bind, the port, the key, the disabled web UI, tools
# and MCP and the server's file paths are fixed here. Every flag on it exists in this
# llama-server build's --help (checked 2026-11-10, version 0.9.0, build 12110, commit
# cd959bae9).
FIXED_FLAG_RE='^(--(host|port|api-key|api-key-file|connect|connect-code|tools|agent|reuse-port|webui|ui|mcp-servers-config|mcp-servers-json|ui-mcp-proxy|webui-mcp-proxy|path|media-path|hf-token|slot-save-path|log-prompts-dir|props|models-dir|models-preset)|-ag|-hft)(=|$)'
# macOS build additions (NOT in serve.ps1): flags of this build (0.9.0, build 12110,
# commit cd959bae9) that the Windows list does not name and that widen exposure, read
# from its --help on 2026-11-10. Same matching rules as the block above.
#   - files the server reads and serves, or writes: --ui-config-file/--webui-config-file
#     (JSON served to clients), --log-file, -lcd/--lookup-cache-dynamic (updated by
#     generation);
#   - commands: --tools-runtime (starts docker/podman containers for tools),
#     --video-ffmpeg-dir (runs ffmpeg/ffprobe from that directory);
#   - network egress: --rpc (offloads work to remote RPC servers);
#   - remote models or URLs: -mu/--model-url, -dr/--docker-repo, -hf/-hfr/--hf-repo,
#     -hff/--hf-file, -hfd/-hfrd/--spec-draft-hf/--hf-repo-draft, -mmu/--mmproj-url, and
#     the presets that "can download weights from the internet" (--*-default, --*-spec);
#   - model integrity: -m/--model would replace the manifest-verified model.
# Everything else this build accepts (--api-prefix, --ssl-*, --cors-origins, --lora,
# -md, --mmproj, ...) is refused by the allow-list above.
MAC_FLAG_RE='^(--(ui-config-file|webui-config-file|log-file|lookup-cache-dynamic|tools-runtime|video-ffmpeg-dir|rpc|model|model-url|docker-repo|hf-repo|hf-file|spec-draft-hf|hf-repo-draft|mmproj-url|embd-gemma-default|fim-qwen-1\.5b-default|fim-qwen-3b-default|fim-qwen-7b-default|fim-qwen-7b-spec|fim-qwen-14b-spec|fim-qwen-30b-default|gpt-oss-20b-default|gpt-oss-120b-default|vision-gemma-4b-default|vision-gemma-12b-default)|-(lcd|m|mu|dr|hf|hfr|hff|hfd|hfrd|mmu))(=|$)'
REFUSAL_WHY="serve.sh fixes --host, --port, the API key and the model, and keeps the web UI, tools, MCP, remote models and the server's file paths off."
denied() {  # denied NORMALISED_FLAG: 0 when either refusal list names it
    local low
    low=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')
    [[ $low =~ $FIXED_FLAG_RE ]] || [[ $low =~ $MAC_FLAG_RE ]]
}

# check_args LABEL ARG... : dies naming the first argument that does not pass; on success
# CHECKED holds the arguments with every "--" flag in its normalised spelling.
# Normalisation: this build's parser rewrites "_" to "-" in an argument that starts with
# "--" (measured: --reuse_port parses as --reuse-port, --bogus_flag is reported as
# --bogus-flag); single-dash arguments are not rewritten (-no_kvu is rejected). The
# check runs on the normalised spelling, so "--reuse_port" is refused like
# "--reuse-port". The Windows original matches its refusal list against the raw token,
# so it has the same underscore hole on this parser (UNVERIFIED on the Windows build).
# "--flag=value" is refused outright: this build rejects that form for every flag tried
# (--threads=4, --n-gpu-layers=99, -fa=on: "invalid argument").
CHECKED=()
check_args() {
    local label=$1 a flag kind
    shift
    CHECKED=()
    while [ $# -gt 0 ]; do
        a=$1
        shift
        case $a in
            *[[:space:]]*|*[[:cntrl:]]*)
                die "$label: $(printf '%q' "$a") holds whitespace or a control character; give one argument per element" ;;
        esac
        case $a in
            --*) flag=$(printf '%s' "$a" | tr '_' '-') ;;
            -*) flag=$a ;;
            *) die "$label: '$a' is not a flag (a value must directly follow the flag that takes it)" ;;
        esac
        case $flag in
            *=*)
                denied "${flag%%=*}" && die "$label may not set ${a%%=*}: $REFUSAL_WHY"
                die "$label: '$a': this llama-server build does not accept --flag=value; give the value as the next argument" ;;
        esac
        if ! kind=$(allowed_kind "$flag"); then
            denied "$flag" && die "$label may not set $a: $REFUSAL_WHY"
            die "$label: $a is not on serve.sh's allow-list of extra llama-server flags (ALLOWED_FLAGS in scripts/serve.sh; models/README.md)"
        fi
        denied "$flag" && die "$label may not set $a: $REFUSAL_WHY"
        CHECKED+=("$flag")
        [ "$kind" = none ] && continue
        [ $# -gt 0 ] || die "$label: $a needs a value ($(kind_text "$kind"))"
        value_ok "$kind" "$1" || die "$label: $a $(printf '%q' "$1"): the value must be $(kind_text "$kind")"
        CHECKED+=("$1")
        shift
    done
}
check_args "extra arguments" ${EXTRA[@]+"${EXTRA[@]}"}
EXTRA=(${CHECKED[@]+"${CHECKED[@]}"})

# ---------------------------------------------------------------- config
[ -f "$CONF" ] || die "config not found: $CONF"
CFG_PORT=""; CFG_CTX=""; CFG_KV=""; CFG_MODEL_ID=""; CFG_TEMPLATE=""
seen=" "
lineno=0
re_key='^[A-Z_][A-Z0-9_]*$'
while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    line=${line%$'\r'}
    case $line in ''|'#'*) continue ;; esac
    case $line in *=*) ;; *) die "$CONF:$lineno: not a KEY=VALUE line" ;; esac
    key=${line%%=*}
    val=${line#*=}
    [[ $key =~ $re_key ]] || die "$CONF:$lineno: bad key '$key'"
    case $seen in *" $key "*) die "$CONF:$lineno: $key is set twice" ;; esac
    seen="$seen$key "
    case $key in
        PORT) CFG_PORT=$val ;;
        CTX) CFG_CTX=$val ;;
        KV_TYPE) CFG_KV=$val ;;
        MODEL_ID) CFG_MODEL_ID=$val ;;
        CHAT_TEMPLATE) CFG_TEMPLATE=$val ;;
        CLIENT_CTX) ;;  # read by the client side only
        *) say "WARNING $CONF:$lineno: unknown key $key ignored" ;;
    esac
done < "$CONF"

re_port='^[1-9][0-9]{0,4}$'
[[ $CFG_PORT =~ $re_port ]] && [ "$CFG_PORT" -le 65535 ] || die "PORT in $CONF must be 1-65535, got '$CFG_PORT'"
re_ctx='^[1-9][0-9]{0,7}$'
[[ $CFG_CTX =~ $re_ctx ]] || die "CTX in $CONF must be a positive integer, got '$CFG_CTX'"
# Allowed values from this build's --help for -ctk/-ctv ("allowed values: f32, f16, bf16,
# q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1", "(default: f16)"). f16 is the unquantized
# default, so KV_TYPE=f16 means "do not quantize the KV cache"; it is still passed
# explicitly as -ctk f16 -ctv f16 (the help lists it as a value, and an explicit flag
# does not depend on the default staying f16).
KV_ALLOWED="f32 f16 bf16 q8_0 q4_0 q4_1 iq4_nl q5_0 q5_1"
re_kv='^[a-z0-9_]+$'
case " $KV_ALLOWED " in
    *" $CFG_KV "*) [[ $CFG_KV =~ $re_kv ]] || die "KV_TYPE in $CONF must be one of $KV_ALLOWED, got '$CFG_KV'" ;;
    *) die "KV_TYPE in $CONF must be one of $KV_ALLOWED, got '$CFG_KV'" ;;
esac
[ -n "$CFG_MODEL_ID" ] || die "MODEL_ID is empty in $CONF: no model chosen yet (see models/README.md)"
re_id='^[A-Za-z0-9._-]+$'
[[ $CFG_MODEL_ID =~ $re_id ]] || die "MODEL_ID in $CONF may hold only letters, digits, '.', '_' and '-', got '$CFG_MODEL_ID'"

# ---------------------------------------------------------------- paths
# phys_path PATH: the physical path of PATH. Its deepest existing ancestor directory is
# resolved with cd -P (every symlink on the way followed); the components below it that
# do not exist yet are appended as written (callers have already refused "..", "." and
# empty components).
phys_path() {
    local d=${1%/*} tail="/${1##*/}"
    [ -n "$d" ] || d=/
    while [ ! -d "$d" ]; do
        tail="/${d##*/}$tail"
        d=${d%/*}
        [ -n "$d" ] || d=/
    done
    d=$(cd -P "$d" 2>/dev/null && pwd -P) || return 1
    printf '%s%s\n' "${d%/}" "$tail"
}
# contained LABEL PATH DIR: dies unless PATH is not itself a symlink and resolves
# physically inside DIR (a physical directory: ROOT is resolved with pwd -P).
contained() {
    local phys
    [ -L "$2" ] && die "$1 $2 is a symlink; refusing (it must be a regular file inside $3)"
    phys=$(phys_path "$2") || die "cannot resolve the $1 path $2"
    case $phys in
        "$3"/*) ;;
        *) die "$1 $2 resolves to $phys, outside $3; refusing" ;;
    esac
}
TEMPLATE=""
if [ -n "$CFG_TEMPLATE" ]; then
    re_rel='^[A-Za-z0-9._/-]+$'
    [[ $CFG_TEMPLATE =~ $re_rel ]] || die "CHAT_TEMPLATE in $CONF has characters outside [A-Za-z0-9._/-]: '$CFG_TEMPLATE'"
    case "/$CFG_TEMPLATE/" in
        //*|*/../*|*/./*|*//*) die "CHAT_TEMPLATE in $CONF must be a plain repo-relative path, got '$CFG_TEMPLATE'" ;;
    esac
    TEMPLATE="$ROOT/$CFG_TEMPLATE"
    contained "chat template (CHAT_TEMPLATE in $CONF)" "$TEMPLATE" "$ROOT/config"
    [ -f "$TEMPLATE" ] || die "chat template not found: $TEMPLATE"
fi

# ---------------------------------------------------------------- tools
# Never from PATH. A tool is taken from the first of /opt/homebrew/bin and /usr/local/bin
# that has it, resolved to its real file, and refused unless that file and every
# directory from it up to the prefix (and the prefix's bin/) is owned by root or the
# current user and not writable by anyone else. A group-writable directory is accepted
# only when no local user but root and the current user is in its group (Homebrew makes
# Cellar/ and bin/ group-writable by "admin"); users of a network directory are not
# enumerated (UNVERIFIED: none here). An ACL entry that lets anyone but the owner change
# a node refuses too (acl_ok). The real file is what runs, so the file that was checked
# is the file that is executed.
MY_UID=$(id -u)
MY_USER=$(id -un)
real_path() {  # real_path PATH: PATH with every symlink resolved (at most 32 hops)
    local p=$1 t d n=0
    while [ -L "$p" ]; do
        n=$((n + 1))
        [ "$n" -le 32 ] || return 1
        t=$(readlink "$p") || return 1
        case $t in /*) p=$t ;; *) p="${p%/*}/$t" ;; esac
    done
    d=${p%/*}
    [ -n "$d" ] || d=/
    d=$(cd -P "$d" 2>/dev/null && pwd -P) || return 1
    printf '%s/%s\n' "${d%/}" "${p##*/}"
}
# group_private NAME GID: 0 when no local user other than root and the current user is a
# member of the group, asking opendirectoryd for each local user (dsmemberutil counts
# primary groups, nested groups and computed groups such as "everyone"; 8 at a time,
# about 0.15 s for 133 users, once per group per run). Every asked user must get the
# exact "not a member" answer; any other answer (an error included) refuses.
GROUP_OK=" "
group_private() {
    local g=$1 gid=$2 users u list="" n=0 out
    case $GROUP_OK in *" $gid "*) return 0 ;; esac
    [[ $g =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
    users=$(dscl . -list /Users 2>/dev/null) || return 1
    [ -n "$users" ] || return 1
    set -f
    for u in $users; do
        [ "$u" = root ] && continue
        [ "$u" = "$MY_USER" ] && continue
        [[ $u =~ ^[A-Za-z0-9_.-]+$ ]] || { set +f; return 1; }
        list="$list$u"$'\n'
        n=$((n + 1))
    done
    set +f
    if [ "$n" -gt 0 ]; then
        out=$(printf '%s' "$list" | xargs -P 8 -I{} dsmemberutil checkmembership -U {} -G "$g" 2>&1)
        [ "$(printf '%s\n' "$out" | grep -cx 'user is not a member of the group')" = "$n" ] || return 1
        [ "$(printf '%s\n' "$out" | grep -c .)" = "$n" ] || return 1
    fi
    GROUP_OK="$GROUP_OK$gid "
}
# These run in this shell, not in $(...), so the group_private cache lasts the run. On
# failure they set WHY; find_tool sets FOUND on success.
WHY=""
FOUND=""
# acl_ok PATH OWNER: 0 when the ACL that `ls -led` lists for PATH grants nobody but the
# owner (user:OWNER) a right beyond reading, listing and executing. An allow entry for any
# other user or group (inherited entries included) with any other right (write, append,
# add_file, add_subdirectory, delete, delete_child, writeattr, writeextattr,
# writesecurity, chown, or a name not known here) refuses; deny entries only take rights
# away and are ignored. Output that does not have the expected shape refuses (fail closed).
# Formats measured with chmod +a on 2026-11-10: "drwxr-xr-x+ ..." then " 0: group:everyone
# allow add_file,file_inherit" or " 1: group:everyone inherited allow write"; a node with
# extended attributes shows "@" instead of "+" and still lists its entries.
acl_ok() {
    local out first line rights perm
    local re_head='^[-dl][-rwxsStT]{9}[@+]? ' re_entry='^ +[0-9]+: (user|group):([^ ]+) (inherited )?(allow|deny) ([a-z_,]+)$'
    out=$(ls -led "$1" 2>/dev/null) || { WHY="cannot list the ACL of $1"; return 1; }
    first=${out%%$'\n'*}
    [[ $first =~ $re_head ]] || { WHY="cannot parse the ls -led listing of $1; refusing"; return 1; }
    [ "$first" = "$out" ] && return 0
    while IFS= read -r line; do
        [[ $line =~ $re_entry ]] || { WHY="cannot parse an ACL entry of $1 ($line); refusing"; return 1; }
        [ "${BASH_REMATCH[4]}" = deny ] && continue
        [ "${BASH_REMATCH[1]}:${BASH_REMATCH[2]}" = "user:$2" ] && continue
        rights=${BASH_REMATCH[5]}
        for perm in ${rights//,/ }; do
            case $perm in
                read|execute|list|search|readattr|readextattr|readsecurity) ;;
                file_inherit|directory_inherit|limit_inherit|only_inherit) ;;
                *) WHY="$1 has an ACL entry that allows ${BASH_REMATCH[1]}:${BASH_REMATCH[2]} $perm"; return 1 ;;
            esac
        done
    done <<< "${out#*$'\n'}"
    return 0
}
trusted_node() {  # trusted_node PATH
    local info uid gid gname mode owner
    info=$(stat -f '%u %g %Sg %Lp %Su' "$1" 2>/dev/null) || { WHY="cannot stat $1"; return 1; }
    set -- "$1" $info
    uid=$2; gid=$3; gname=$4; mode=$5; owner=${6:-}
    while [ ${#mode} -lt 3 ]; do mode="0$mode"; done
    if [ "$uid" != 0 ] && [ "$uid" != "$MY_UID" ]; then
        WHY="$1 is owned by uid $uid, not root or the current user"; return 1
    fi
    acl_ok "$1" "$owner" || return 1
    case ${mode: -1} in 2|3|6|7) WHY="$1 is writable by everyone (mode $mode)"; return 1 ;; esac
    case ${mode: -2:1} in
        2|3|6|7)
            group_private "$gname" "$gid" || {
                WHY="$1 is writable by group $gname, which can hold users other than root and the current user"; return 1; } ;;
    esac
    return 0
}
check_trusted() {  # check_trusted REAL STOP: REAL and each directory above it up to STOP
    local p=$1
    while :; do
        trusted_node "$p" || return 1
        [ "$p" = "$2" ] && return 0
        case $p in "$2"/*) ;; *) WHY="$1 is outside $2"; return 1 ;; esac
        p=${p%/*}
    done
}
find_tool() {  # find_tool NAME
    local name=$1 prefix pp cand real
    FOUND=""
    for prefix in /opt/homebrew /usr/local; do
        cand="$prefix/bin/$name"
        [ -e "$cand" ] || continue
        pp=$(cd -P "$prefix" 2>/dev/null && pwd -P) || { WHY="cannot resolve $prefix"; return 1; }
        real=$(real_path "$cand") || { WHY="cannot resolve $cand"; return 1; }
        case $real in "$pp"/*) ;; *) WHY="$cand resolves to $real, outside $pp"; return 1 ;; esac
        [ -f "$real" ] && [ -x "$real" ] || { WHY="$real is not an executable file"; return 1; }
        check_trusted "$real" "$pp" || return 1
        trusted_node "$pp/bin" || return 1
        FOUND=$real
        return 0
    done
    WHY="not found in /opt/homebrew/bin or /usr/local/bin"
    return 1
}
OVERRIDE_MSGS=()
override_note() { say "$*"; OVERRIDE_MSGS+=("serve.sh: $*"); }
find_tool python3.13 || die "python3.13 (it reads models/manifest.json): $WHY"
PY=$FOUND
if [ -n "${LIS_TEST_LLAMA_SERVER:-}" ]; then
    case $LIS_TEST_LLAMA_SERVER in /*) ;; *) die "LIS_TEST_LLAMA_SERVER must be an absolute path" ;; esac
    LLAMA=$(real_path "$LIS_TEST_LLAMA_SERVER") || die "cannot resolve LIS_TEST_LLAMA_SERVER=$LIS_TEST_LLAMA_SERVER"
    [ -f "$LLAMA" ] && [ -x "$LLAMA" ] || die "LIS_TEST_LLAMA_SERVER=$LIS_TEST_LLAMA_SERVER is not an executable file"
    trusted_node "$LLAMA" && trusted_node "${LLAMA%/*}" || die "LIS_TEST_LLAMA_SERVER: $WHY"
    override_note "TEST OVERRIDE: LIS_TEST_LLAMA_SERVER=$LIS_TEST_LLAMA_SERVER runs instead of the installed llama-server (tests only)"
else
    find_tool llama-server || die "llama-server (brew install llama.cpp): $WHY"
    LLAMA=$FOUND
fi
LLAMA_EXE=$LLAMA
if [ -n "${LIS_TEST_LLAMA_EXE:-}" ]; then
    case $LIS_TEST_LLAMA_EXE in /*) ;; *) die "LIS_TEST_LLAMA_EXE must be an absolute path" ;; esac
    LLAMA_EXE=$LIS_TEST_LLAMA_EXE
    override_note "TEST OVERRIDE: LIS_TEST_LLAMA_EXE=$LIS_TEST_LLAMA_EXE is the executable the process check expects (tests only)"
fi

# ---------------------------------------------------------------- manifest
# For the single entry whose id is argv[2], prints one value per line: path, size,
# sha256, kv_type, temp, top_p, top_k (empty line = absent), then one line per
# server_args element. argv[3] is the space-separated list of allowed KV types.
# Entry fields (strict: an unknown field, a wrong type or a value out of range refuses):
#   id, path, size, sha256 (required); repo, revision, file (strings), default (bool);
#   server_args: list of non-empty strings without control characters, appended to the
#     command line (bash then runs them through check_args, the same allow-list as the
#     -- arguments);
#   sampling: object with any of temp (number >= 0), top_p (number 0..1), top_k
#     (integer >= 0); finite numbers only, booleans rejected;
#   kv_type: one of the allowed KV types; overrides KV_TYPE from config/lis.conf.
PY_MANIFEST='
import json, math, re, sys
path, want, kv_allowed = sys.argv[1], sys.argv[2], sys.argv[3].split()
def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)
def reject_constant(name):
    raise ValueError("non-finite number %s" % name)
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f, parse_constant=reject_constant)
except (OSError, ValueError) as e:
    fail("cannot read %s: %s" % (path, e))
files = data.get("files") if isinstance(data, dict) else None
if not isinstance(files, list):
    fail("%s has no \"files\" list" % path)
hits = [e for e in files if isinstance(e, dict) and e.get("id") == want]
if len(hits) != 1:
    fail("%s has %d entries with id %r (need exactly 1)" % (path, len(hits), want))
e = hits[0]
p, size, sha = e.get("path"), e.get("size"), e.get("sha256")
parts = p.split("/") if isinstance(p, str) else []
if (not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9._+-]+(/[A-Za-z0-9._+-]+)*\.gguf", p)
        or parts[0] != "models" or ".." in parts or "." in parts):
    fail("entry %r: path must be a repo-relative models/... .gguf path, got %r" % (want, p))
if type(size) is not int or size <= 0:
    fail("entry %r: size must be a positive integer, got %r" % (want, size))
if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
    fail("entry %r: sha256 must be 64 lowercase hex characters, got %r" % (want, sha))
known = {"id", "path", "size", "sha256", "repo", "revision", "file", "default", "server_args", "sampling", "kv_type"}
unknown = sorted(set(e) - known)
if unknown:
    fail("entry %r: unknown field(s) %s (allowed: %s)" % (want, ", ".join(unknown), ", ".join(sorted(known))))
for k in ("repo", "revision", "file"):
    if k in e and not isinstance(e[k], str):
        fail("entry %r: %s must be a string, got %r" % (want, k, e[k]))
if "default" in e and not isinstance(e["default"], bool):
    fail("entry %r: default must be true or false, got %r" % (want, e["default"]))
args = e.get("server_args", [])
if not isinstance(args, list):
    fail("entry %r: server_args must be a list of strings, got %r" % (want, args))
for a in args:
    if not isinstance(a, str) or not a or any(ord(c) < 32 or ord(c) == 127 for c in a):
        fail("entry %r: server_args elements must be non-empty strings without control characters, got %r" % (want, a))
kv = e.get("kv_type", "")
if "kv_type" in e and (not isinstance(kv, str) or kv not in kv_allowed):
    fail("entry %r: kv_type must be one of %s, got %r" % (want, " ".join(kv_allowed), kv))
samp = e.get("sampling", {})
if not isinstance(samp, dict):
    fail("entry %r: sampling must be an object, got %r" % (want, samp))
extra = sorted(set(samp) - {"temp", "top_p", "top_k"})
if extra:
    fail("entry %r: sampling may hold only temp, top_p, top_k; got %s" % (want, ", ".join(extra)))
def num(k, v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        fail("entry %r: sampling.%s must be a finite number, got %r" % (want, k, v))
    return v
out = {}
if "temp" in samp:
    v = num("temp", samp["temp"])
    if v < 0:
        fail("entry %r: sampling.temp must be >= 0, got %r" % (want, v))
    out["temp"] = repr(float(v))
if "top_p" in samp:
    v = num("top_p", samp["top_p"])
    if not 0 <= v <= 1:
        fail("entry %r: sampling.top_p must be between 0 and 1, got %r" % (want, v))
    out["top_p"] = repr(float(v))
if "top_k" in samp:
    v = num("top_k", samp["top_k"])
    if not isinstance(v, int) or v < 0:
        fail("entry %r: sampling.top_k must be an integer >= 0, got %r" % (want, v))
    out["top_k"] = str(v)
for line in [p, str(size), sha, kv, out.get("temp", ""), out.get("top_p", ""), out.get("top_k", "")] + args:
    print(line)
'
entry=$("$PY" -I -c "$PY_MANIFEST" "$MANIFEST" "$CFG_MODEL_ID" "$KV_ALLOWED") || die "model lookup failed (MODEL_ID=$CFG_MODEL_ID)"
SERVER_ARGS=()
{
    IFS= read -r MODEL_REL
    IFS= read -r MODEL_SIZE
    IFS= read -r MODEL_SHA
    IFS= read -r M_KV
    IFS= read -r M_TEMP
    IFS= read -r M_TOP_P
    IFS= read -r M_TOP_K
    while IFS= read -r a; do SERVER_ARGS+=("$a"); done
} <<< "$entry"
MODEL="$ROOT/$MODEL_REL"
contained "model (manifest id $CFG_MODEL_ID)" "$MODEL" "$ROOT/models"
check_args "server_args of manifest entry $CFG_MODEL_ID" ${SERVER_ARGS[@]+"${SERVER_ARGS[@]}"}
SERVER_ARGS=(${CHECKED[@]+"${CHECKED[@]}"})
KV=$CFG_KV
[ -n "$M_KV" ] && KV=$M_KV
TEMP=0.6; TOP_P=0.95; TOP_K=20
[ -n "$M_TEMP" ] && TEMP=$M_TEMP
[ -n "$M_TOP_P" ] && TOP_P=$M_TOP_P
[ -n "$M_TOP_K" ] && TOP_K=$M_TOP_K

# ---------------------------------------------------------------- command line
# Flags as in serve.ps1 (-fa on, -np 1, Qwen sampling, --reasoning-format auto,
# --reasoning, --api-key-file, --no-webui, --metrics, --no-mmproj, loopback --host).
# Each one is in this build's --help (version 0.9.0, build 12110, commit cd959bae9).
# Not ported: -hf (models come from the manifest only), --n-cpu-moe (no discrete GPU:
# unified memory), MTP (--spec-type draft-mtp, tied to one Windows model file), -lv 3
# (the -Web mode). The default sampling (0.6 / 0.95 / 20) is Qwen's; a manifest entry's
# "sampling" replaces any of the three, its "kv_type" replaces KV_TYPE, and its
# "server_args" come after the fixed flags and before the -- extra arguments.
# --offline (NOT in serve.ps1): this build's --help says "Offline mode: forces use of
# cache, prevents network access"; it takes no value and has no --no-offline. Measured
# 2026-11-10: `llama-server --offline --version` parses, and `--offline -m <local file>`
# went on to "load_model: loading model" (a FIFO standing in for the model, stopped with
# SIGTERM during the load). A full load and serve of a real model with --offline is
# UNVERIFIED until a model exists.
CMD=("$LLAMA"
    --offline
    -m "$MODEL"
    -c "$CFG_CTX"
    -fa on
    -ctk "$KV" -ctv "$KV"
    -np 1
    --alias local
    --temp "$TEMP" --top-p "$TOP_P" --top-k "$TOP_K"
    --reasoning-format auto
    --reasoning "$REASONING"
    --api-key-file "$KEYFILE"
    --no-webui
    --metrics
    --host 127.0.0.1 --port "$CFG_PORT"
    --no-mmproj)
[ -n "$TEMPLATE" ] && CMD+=(--chat-template-file "$TEMPLATE")
CMD+=(${SERVER_ARGS[@]+"${SERVER_ARGS[@]}"})
CMD+=(${EXTRA[@]+"${EXTRA[@]}"})
CMDLINE=$(printf '%q ' "${CMD[@]}")
CMDLINE=${CMDLINE% }

if [ "$MODE" = dry-run ]; then
    [ -n "$CLEARED_MSG" ] && say "$CLEARED_MSG"
    printf '%s\n' "$CMDLINE"
    exit 0
fi

# ---------------------------------------------------------------- process helpers
# proc_name PID: basename of what `ps -o comm=` reports for a live, non-zombie process.
# On macOS that follows argv[0] (exec -a sets it), so it identifies nothing by itself;
# it is used alone only for the lock owner (a bash), where a wrong answer can only make
# serve.sh refuse to start.
proc_name() {
    local p=$1 st comm
    [[ $p =~ ^[1-9][0-9]{0,9}$ ]] || return 1
    st=$(ps -p "$p" -o stat= 2>/dev/null) || return 1
    case $st in *Z*) return 1 ;; esac
    comm=$(ps -p "$p" -o comm= 2>/dev/null) || return 1
    printf '%s\n' "${comm##*/}"
}
# is_llama PID: 0 when PID is a live, non-zombie process whose comm basename is
# llama-server AND whose executable file, as lsof reports it (the first "txt" entry,
# which the kernel takes from the vnode exec'd, not from argv), is LLAMA_EXE.
# Proven: the process runs that executable file (exec -a and a symlink of the right name
# to another binary both fail). Not proven: that it is the process serve.sh started (any
# llama-server started from that binary passes, from any checkout), or which arguments it
# has. After `brew upgrade llama.cpp` under a running server, the running file is no
# longer LLAMA_EXE and the check fails (UNVERIFIED how lsof names a deleted executable).
# lsof sees only the current user's processes, so another user's process always fails.
exe_of() {
    lsof -w -a -p "$1" -d txt -Fn 2>/dev/null | awk 'substr($0, 1, 1) == "n" { print substr($0, 2); exit }'
}
is_llama() {
    local p=$1
    [ "$(proc_name "$p")" = llama-server ] || return 1
    [ "$(exe_of "$p")" = "$LLAMA_EXE" ]
}
read_pid() {  # first line of a file, or nothing
    local f=$1 v=""
    [ -f "$f" ] && IFS= read -r v < "$f"
    printf '%s' "$v"
}
write_file() {  # atomic replace with mode 600: write_file PATH CONTENT (never into a directory)
    local tmp="$1.tmp.$$"
    [ -d "$1" ] && return 1
    rm -f "$tmp"
    (set -C; printf '%s\n' "$2" > "$tmp") || return 1
    chmod 600 "$tmp" && mv -f "$tmp" "$1"
}

# ---------------------------------------------------------------- logs dir
[ -L "$LOGS" ] && die "$LOGS is a symlink; refusing (the key file lives there)"
mkdir -p "$LOGS" || die "cannot create $LOGS"
[ -O "$LOGS" ] || die "$LOGS is not owned by the current user"
chmod 700 "$LOGS" || die "cannot chmod 700 $LOGS"

# ---------------------------------------------------------------- lock
# The key file is shared by every port, so the lock is per checkout, not per port (a
# second server on another port would rotate the first one's key). Held while the
# owner (this script, a bash process) or the recorded llama-server is alive.
LOCKED=0
CHILD=""
lock_live() {
    local owner srv
    owner=$(read_pid "$LOCKDIR/owner")
    srv=$(read_pid "$PIDFILE")
    if [ -n "$owner" ] && [ "$(proc_name "$owner")" = bash ]; then
        printf 'serve.sh pid %s' "$owner"; return 0
    fi
    if [ -n "$srv" ] && is_llama "$srv"; then
        printf 'llama-server pid %s' "$srv"; return 0
    fi
    # Created in the last 10 s and no owner written yet: another start is mid-way.
    if [ -z "$owner" ] && [ $(( $(date +%s) - $(stat -f %m "$LOCKDIR" 2>/dev/null || echo 0) )) -lt 10 ]; then
        printf 'a serve.sh starting now'; return 0
    fi
    return 1
}
lock_id() {  # inode, birth time and owner of a lock directory
    local i
    i=$(stat -f '%i %B' "$1" 2>/dev/null) || return 1
    printf '%s %s' "$i" "$(read_pid "$1/owner")"
}
take_lock() {
    local holder id grave
    if ! mkdir "$LOCKDIR" 2>/dev/null; then
        # The identity (inode, birth time, owner) of the lock about to be judged is read
        # BEFORE the liveness check, so "stale" is a verdict on exactly that lock: if
        # another start replaces it after this read, the replacement has another identity
        # and is never adopted or deleted as the stale one.
        id=$(lock_id "$LOCKDIR") || die "$LOCKDIR changed while it was being checked; try again"
        if holder=$(lock_live); then
            die "another server holds $LOCKDIR ($holder); stop it first: scripts/stop.sh"
        fi
        # Stale. It is moved aside in one atomic rename, then deleted, then the lock is
        # made anew with mkdir. The moved directory's identity is compared with the
        # identity captured above, before the liveness check: if another start replaced
        # the lock in between, the moved lock is not the one judged stale, so it is moved
        # back and this start gives up. Two starts that judge the same stale lock: one
        # rename wins, the other fails and gives up. Remaining window: a third start that
        # makes a new lock between such a mistaken rename and the move back
        # (milliseconds); then two starts can hold it. A start killed between the rename
        # and the delete leaves a serve.lock.stale.* directory in logs/ (harmless; delete
        # it by hand).
        grave="$LOGS/serve.lock.stale.$$.$RANDOM"
        [ -e "$grave" ] && die "$grave exists; try again"
        say "removing stale lock $LOCKDIR"
        mv "$LOCKDIR" "$grave" 2>/dev/null || die "$LOCKDIR changed while it was being cleared; try again"
        if [ "$(lock_id "$grave")" != "$id" ]; then
            [ -e "$LOCKDIR" ] || mv "$grave" "$LOCKDIR" 2>/dev/null
            die "$LOCKDIR was replaced while it was being cleared; try again"
        fi
        rm -rf "$grave"
        mkdir "$LOCKDIR" 2>/dev/null || die "another start took $LOCKDIR just now"
    fi
    LOCKED=1
    write_file "$LOCKDIR/owner" "$$" || die "cannot write $LOCKDIR/owner"
}
release() {
    # Only what this run owns: the pid file while it still names our child, and the
    # lock while it names this script.
    if [ -n "$CHILD" ] && [ "$(read_pid "$PIDFILE")" = "$CHILD" ]; then rm -f "$PIDFILE"; fi
    if [ "$LOCKED" = 1 ] && [ "$(read_pid "$LOCKDIR/owner")" = "$$" ]; then rm -rf "$LOCKDIR"; fi
    LOCKED=0
}
trap release EXIT
take_lock

srv=$(read_pid "$PIDFILE")
if [ -n "$srv" ]; then
    if is_llama "$srv"; then
        die "llama-server pid $srv from $PIDFILE is still running; stop it first: scripts/stop.sh"
    fi
    say "pid file named pid $srv, which is not a running $LLAMA_EXE; ignoring it"
fi
rm -f "$PIDFILE"

if listeners=$(lsof -nP -iTCP:"$CFG_PORT" -sTCP:LISTEN -t 2>/dev/null) && [ -n "$listeners" ]; then
    die "port $CFG_PORT is already in use (listening pid(s): $(printf '%s' "$listeners" | tr '\n' ' '))"
fi

# ---------------------------------------------------------------- model check
[ -f "$MODEL" ] || die "model file not found: $MODEL (manifest id $CFG_MODEL_ID)"
MSGS=(${OVERRIDE_MSGS[@]+"${OVERRIDE_MSGS[@]}"})
note() { say "$*"; MSGS+=("serve.sh: $*"); }
verify_model() {
    local size mtime boot want hash t0
    size=$(stat -L -f %z "$MODEL") || return 1
    mtime=$(stat -L -f %m "$MODEL") || return 1
    boot=$(sysctl -n kern.boottime 2>/dev/null | sed -E 's/^\{ sec = ([0-9]+),.*/\1/')
    [[ $boot =~ ^[0-9]+$ ]] || boot=""
    want="boot=$boot size=$size mtime=$mtime sha256=$MODEL_SHA path=$MODEL"
    if [ -n "$boot" ] && [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = "$want" ]; then
        note "model sha256 verified earlier this boot ($MARKER); not hashed again"
        return 0
    fi
    if [ "$size" != "$MODEL_SIZE" ]; then
        die "refusing to start: $MODEL is $size bytes, $MANIFEST says $MODEL_SIZE"
    fi
    t0=$SECONDS
    hash=$(shasum -a 256 "$MODEL" | awk '{print $1}') || return 1
    if [ "$hash" != "$MODEL_SHA" ]; then
        die "refusing to start: $MODEL sha256 $hash, $MANIFEST says $MODEL_SHA"
    fi
    if [ -n "$boot" ]; then
        write_file "$MARKER" "$want" || return 1
        note "model sha256 matches $MANIFEST ($((SECONDS - t0)) s); marker $MARKER written"
    else
        note "model sha256 matches $MANIFEST ($((SECONDS - t0)) s); boot time unreadable, no marker written"
    fi
}
verify_model || die "refusing to start: model check failed"

# ---------------------------------------------------------------- key
KEY=$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')
[[ $KEY =~ ^[0-9a-f]{64}$ ]] || die "could not generate an API key"
rm -f "$KEYFILE"
write_file "$KEYFILE" "$KEY" || die "cannot write $KEYFILE"
[ "$(stat -f %Lp "$KEYFILE")" = 600 ] || die "$KEYFILE is not mode 600"
unset KEY

# ---------------------------------------------------------------- memory
# macOS has no free-VRAM number: the GPU uses unified memory. Logged: total RAM, the
# vm_stat page classes the kernel can hand out without swapping (free, inactive,
# speculative, purgeable; whether that sum is a safe fit bound for a Metal model load is
# UNVERIFIED) and the GPU wired limit sysctl (0 = the macOS default cap). llama-server
# itself logs Metal's recommendedMaxWorkingSetSize when it loads a model.
mem_report() {
    local total vs limit
    total=$(sysctl -n hw.memsize 2>/dev/null) || total=""
    vs=$(vm_stat 2>/dev/null) || vs=""
    limit=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null) || limit="unavailable"
    if [ -z "$vs" ] || ! [[ $total =~ ^[0-9]+$ ]]; then
        printf 'memory free: unavailable\n'
        return
    fi
    printf '%s\n' "$vs" | awk -v total="$total" -v limit="$limit" '
        NR == 1 { if (match($0, /page size of [0-9]+/)) ps = substr($0, RSTART + 13, RLENGTH - 13) + 0 }
        /^Pages free:/ { free = $NF }
        /^Pages inactive:/ { inact = $NF }
        /^Pages speculative:/ { spec = $NF }
        /^Pages purgeable:/ { purg = $NF }
        /^Pages wired down:/ { wired = $NF }
        /^Pages active:/ { act = $NF }
        /^Pages occupied by compressor:/ { comp = $NF }
        END {
            if (!ps) { print "memory free: unavailable"; exit }
            m = ps / 1048576
            printf "memory free: %d MiB available (free %d + inactive %d + speculative %d + purgeable %d MiB) of %d MiB; wired %d, active %d, compressed %d MiB; iogpu.wired_limit_mb=%s\n",
                (free + inact + spec + purg) * m, free * m, inact * m, spec * m, purg * m, total / 1048576,
                wired * m, act * m, comp * m, limit
        }'
}
[ -n "$CLEARED_MSG" ] && note "$CLEARED_MSG"
note "$(mem_report)"
note "llama-server $LLAMA: $("$LLAMA" --version 2>&1 | grep -m1 '^version' || echo 'version unknown')"

# ---------------------------------------------------------------- start
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
LOG="$LOGS/server-$STAMP.log"
[ -e "$LOG" ] && LOG="$LOGS/server-$STAMP-$$.log"
(set -C; printf '%s\n' "$CMDLINE" > "$LOG") || die "cannot create $LOG"
for m in ${MSGS[@]+"${MSGS[@]}"}; do printf '%s\n' "$m" >> "$LOG"; done
write_file "$LOCKDIR/log" "$LOG" || die "cannot write $LOCKDIR/log"

cd "$ROOT" || die "cannot cd to $ROOT"
log_tail() { say "last lines of $LOG:"; tail -n 40 "$LOG" >&2; }

# forward: SIGTERM to our own child (one validated pid; it is our unreaped child, so its
# pid cannot have been reused). stop_child: forward, then wait up to 30 s for it to end
# and reap it; 1 if it still runs.
SIGNALED=0
forward() {
    SIGNALED=1
    if [[ $CHILD =~ ^[1-9][0-9]*$ ]] && [ "$CHILD" -gt 1 ]; then kill -TERM "$CHILD" 2>/dev/null; fi
}
stop_child() {
    local i=0 st
    forward
    while [ "$i" -lt 150 ]; do
        st=$(ps -p "$CHILD" -o stat= 2>/dev/null) || st=""
        case $st in ''|*Z*) wait "$CHILD" 2>/dev/null; return 0 ;; esac
        sleep 0.2
        i=$((i + 1))
    done
    return 1
}
pidfile_failed() {
    if stop_child; then
        die "cannot write $PIDFILE; llama-server pid $CHILD was stopped"
    fi
    die "cannot write $PIDFILE; llama-server pid $CHILD did not stop within 30 s of SIGTERM"
}

if [ "$MODE" = detach ]; then
    # An interrupted start leaves no server behind. INT, TERM and HUP only record the
    # signal (the handler does no work, so it cannot race the launch); the wait loop
    # checks it at least once a second (sleep 1, curl --max-time 2) and abort_start
    # forwards SIGTERM to the child, waits for it and reaps it (stop_child), then exits
    # 128+N; the EXIT trap (release) removes the pid file and lock. A child that is still
    # running 30 s after SIGTERM keeps its pid file and lock so scripts/stop.sh can stop
    # it. nohup makes the child ignore SIGHUP, and a background child of this
    # non-interactive bash ignores SIGINT, so these signals reach it only through here.
    DETACH_SIG=""
    trap 'DETACH_SIG=${DETACH_SIG:-INT}' INT
    trap 'DETACH_SIG=${DETACH_SIG:-TERM}' TERM
    trap 'DETACH_SIG=${DETACH_SIG:-HUP}' HUP
    abort_start() {
        local n=15
        case $DETACH_SIG in INT) n=2 ;; HUP) n=1 ;; esac
        trap '' INT TERM HUP
        say "start interrupted by SIG$DETACH_SIG; stopping llama-server pid $CHILD"
        if stop_child; then
            printf 'serve.sh: start interrupted by SIG%s; llama-server stopped\n' "$DETACH_SIG" >> "$LOG"
            say "start interrupted by SIG$DETACH_SIG: llama-server pid $CHILD stopped and reaped; no server left running"
            exit $((128 + n))
        fi
        say "start interrupted by SIG$DETACH_SIG: llama-server pid $CHILD did not stop within 30 s of SIGTERM; its pid file and lock are kept: scripts/stop.sh"
        CHILD=""
        LOCKED=0
        exit $((128 + n))
    }
    nohup "${CMD[@]}" >> "$LOG" 2>&1 < /dev/null &
    CHILD=$!
    write_file "$PIDFILE" "$CHILD" || pidfile_failed
    say "llama-server pid $CHILD starting (log $LOG); waiting up to $WAIT_SEC s for /health"
    i=0
    while [ "$i" -lt "$WAIT_SEC" ]; do
        [ -n "$DETACH_SIG" ] && abort_start
        st=$(ps -p "$CHILD" -o stat= 2>/dev/null) || st=""
        case $st in
            ''|*Z*)
                wait "$CHILD"
                code=$?
                printf 'serve.sh: llama-server exited %s\n' "$code" >> "$LOG"
                log_tail
                die "llama-server exited $code before /health answered"
                ;;
        esac
        if curl -q -fsS --max-time 2 "http://127.0.0.1:$CFG_PORT/health" > /dev/null 2>&1; then
            [ -n "$DETACH_SIG" ] && abort_start
            say "llama-server pid $CHILD healthy on 127.0.0.1:$CFG_PORT (log $LOG)"
            # The server outlives this script: keep the pid file; the lock stays held
            # through it (lock_live) and is reclaimed as stale once the server is gone.
            CHILD=""
            LOCKED=0
            exit 0
        fi
        sleep 1
        i=$((i + 1))
    done
    [ -n "$DETACH_SIG" ] && abort_start
    log_tail
    say "llama-server pid $CHILD is still running but /health did not answer within $WAIT_SEC s; scripts/stop.sh stops it"
    CHILD=""
    LOCKED=0
    exit 1
fi

# Foreground. A background child of a non-interactive bash ignores SIGINT, so Ctrl-C is
# caught here and forwarded as SIGTERM to the child.
trap forward INT TERM HUP
"${CMD[@]}" >> "$LOG" 2>&1 < /dev/null &
CHILD=$!
write_file "$PIDFILE" "$CHILD" || pidfile_failed
say "llama-server pid $CHILD on 127.0.0.1:$CFG_PORT; log $LOG (tail -f it); Ctrl-C or scripts/stop.sh stops it"
# A trapped signal makes wait return 128+N at once, while the child may still run (or
# not yet be reaped): wait again until a wait returns that no trapped signal interrupted,
# which is the child's own status. 127 = the status was already collected: keep the last.
code=127
while :; do
    SIGNALED=0
    wait "$CHILD"
    c=$?
    [ "$c" = 127 ] && break
    code=$c
    [ "$SIGNALED" = 1 ] || break
done
printf 'serve.sh: llama-server exited %s\n' "$code" >> "$LOG"
say "llama-server exited $code"
exit "$code"
