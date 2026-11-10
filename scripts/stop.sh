#!/bin/bash
set +xv  # first: no inherited SHELLOPTS=xtrace/verbose tracing (serve.sh does the same)
# stop.sh - stop the llama-server that scripts/serve.sh recorded in logs/llama-server.pid.
#
# macOS port of scripts/stop.ps1 (Windows reference, commit cafc34b). It stops ONE
# process: the pid in the pid file, and only after checking that
#   - the value matches ^[0-9]+$, has no leading zero and is greater than 1;
#   - the process is alive and not a zombie;
#   - `ps -p <pid> -o comm=` has the basename llama-server. On macOS comm follows argv[0]
#     (`exec -a llama-server /bin/sleep` passes it), so this alone proves nothing;
#   - its executable file, as `lsof -a -p <pid> -d txt -Fn` reports it (the first txt
#     entry: the file the kernel exec'd, independent of argv), is the real path of the
#     installed llama-server (/opt/homebrew/bin/llama-server, else
#     /usr/local/bin/llama-server, symlinks resolved).
# What that proves: the pid runs the installed llama-server binary. What it does not
# prove: that serve.sh started it, from this checkout, with these arguments (any
# llama-server run from that binary by the current user passes). lsof sees only the
# current user's processes, so another user's process is always refused. After
# `brew upgrade llama.cpp` under a running server the running file is no longer the
# installed one and stop.sh refuses (UNVERIFIED how lsof names the deleted file): stop
# that server with Ctrl-C in its serve.sh terminal.
# Then `kill -TERM` on that one quoted pid and a wait of up to 30 s. There is no
# process-group kill, no pkill/killall, no computed pid list and no SIGKILL escalation.
#
# Exit codes: 0 stopped, or nothing to stop (no pid file, or a recorded pid that is no
# longer running: the stale pid file is removed); 1 refused (bad pid file content, pid
# 1, a live process that is not the installed llama-server: the pid file is left as it
# is) or the process still runs 30 s after SIGTERM.
#
# Unlike stop.ps1 it does not look the process up by listening port, has no -Force and
# writes no stop-request file (serve.sh has no restart loop to suppress). It appends
# "[stop.sh] stop requested <UTC>" and "[stop.sh] stopped PID <pid> <UTC>" to the server
# log serve.sh recorded in logs/serve.lock/log, only when that is exactly
# <logs>/server-<stamp>.log as serve.sh names it (no "/" or ".." in <stamp>) and a
# regular file, not a symlink.
#
# Test-only overrides (scripts/tests/stop_test.py; never set them otherwise), each
# announced with a "TEST OVERRIDE" line on stderr: LIS_TEST_LLAMA_SERVER (absolute path
# whose real path replaces the installed llama-server's) and LIS_TEST_LLAMA_EXE (the
# executable path expected, as is), as in scripts/serve.sh.
# A clean llama-server exit on SIGTERM after a model has loaded is UNVERIFIED (no model
# yet; a SIGTERM during the load ended it, measured 2026-11-10).
set -u
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd -P) || exit 1
ROOT=$(cd "$SCRIPT_DIR/.." && pwd -P) || exit 1
LOGS="$ROOT/logs"
PIDFILE="$LOGS/llama-server.pid"
LOGREF="$LOGS/serve.lock/log"

say() { printf 'stop.sh: %s\n' "$*" >&2; }
refuse() { say "refusing: $*"; exit 1; }

if [ ! -e "$PIDFILE" ] && [ ! -L "$PIDFILE" ]; then
    say "no pid file ($PIDFILE): nothing to stop"
    exit 0
fi
[ -L "$PIDFILE" ] && refuse "$PIDFILE is a symlink"
[ -f "$PIDFILE" ] || refuse "$PIDFILE is not a regular file"

pid=""
IFS= read -r pid < "$PIDFILE" || true
re_digits='^[0-9]+$'
[[ $pid =~ $re_digits ]] || refuse "$PIDFILE holds '$pid', not a pid"
re_pid='^[1-9][0-9]{0,9}$'
[[ $pid =~ $re_pid ]] || refuse "$PIDFILE holds '$pid' (leading zero or too long)"
[ "$pid" -gt 1 ] || refuse "$PIDFILE holds pid $pid"

st=$(ps -p "$pid" -o stat= 2>/dev/null) || st=""
case $st in
    ''|*Z*)
        say "pid $pid is not running; removing the stale pid file"
        rm -f "$PIDFILE"
        exit 0
        ;;
esac
comm=$(ps -p "$pid" -o comm= 2>/dev/null) || comm=""
name=${comm##*/}
[ "$name" = llama-server ] || refuse "pid $pid is '$comm', not llama-server"

# The executable expected: the installed llama-server's real file.
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
want=""
if [ -n "${LIS_TEST_LLAMA_EXE:-}" ]; then
    want=$LIS_TEST_LLAMA_EXE
    say "TEST OVERRIDE: LIS_TEST_LLAMA_EXE=$want is the executable expected (tests only)"
elif [ -n "${LIS_TEST_LLAMA_SERVER:-}" ]; then
    want=$(real_path "$LIS_TEST_LLAMA_SERVER") || want=""
    say "TEST OVERRIDE: LIS_TEST_LLAMA_SERVER=$LIS_TEST_LLAMA_SERVER replaces the installed llama-server (tests only)"
else
    for cand in /opt/homebrew/bin/llama-server /usr/local/bin/llama-server; do
        if [ -e "$cand" ]; then
            want=$(real_path "$cand") || want=""
            break
        fi
    done
fi
case $want in /*) ;; *) refuse "cannot find the installed llama-server to compare pid $pid with" ;; esac
exe=$(lsof -w -a -p "$pid" -d txt -Fn 2>/dev/null | awk 'substr($0, 1, 1) == "n" { print substr($0, 2); exit }')
[ -n "$exe" ] || refuse "pid $pid: lsof does not report its executable"
[ "$exe" = "$want" ] || refuse "pid $pid runs $exe, not the installed llama-server $want"

# The server log serve.sh recorded: exactly <logs>/server-<stamp>.log, where <stamp> is
# serve.sh's UTC stamp with an optional -<pid> suffix (so no "/" and no ".."), a regular
# file and not a symlink. Anything else: no log line is written.
log=""
if [ -f "$LOGREF" ] && [ ! -L "$LOGREF" ]; then
    IFS= read -r log < "$LOGREF" || true
    case $log in
        "$LOGS"/server-*.log)
            stamp=${log#"$LOGS"/server-}
            stamp=${stamp%.log}
            re_stamp='^[0-9]{8}T[0-9]{6}Z(-[0-9]{1,10})?$'
            if ! [[ $stamp =~ $re_stamp ]] || [ ! -f "$log" ] || [ -L "$log" ]; then log=""; fi
            ;;
        *) log="" ;;
    esac
fi
mark() { [ -n "$log" ] && printf '[stop.sh] %s %s\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$log"; return 0; }

mark "stop requested"
say "stopping llama-server pid $pid"
kill -TERM "$pid" || refuse "kill -TERM $pid failed"

i=0
while [ "$i" -lt 150 ]; do
    st=$(ps -p "$pid" -o stat= 2>/dev/null) || st=""
    case $st in ''|*Z*) break ;; esac
    sleep 0.2
    i=$((i + 1))
done
st=$(ps -p "$pid" -o stat= 2>/dev/null) || st=""
case $st in
    ''|*Z*)
        mark "stopped PID $pid"
        cur=""
        [ -f "$PIDFILE" ] && IFS= read -r cur < "$PIDFILE"
        [ "$cur" = "$pid" ] && rm -f "$PIDFILE"
        say "stopped pid $pid"
        exit 0
        ;;
esac
mark "stop timed out PID $pid"
say "pid $pid still runs 30 s after SIGTERM; not escalating"
exit 1
