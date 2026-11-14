#!/bin/bash
# smoke.sh - smoke-test the running llama-server on 127.0.0.1:PORT (config/lis.conf).
#
# macOS port of scripts/smoke.ps1 (Windows reference, commit cafc34b), extended to a full
# tool round trip. Steps, each one fatal:
#   1. health      GET /health answers HTTP 200 with status "ok"
#   2. text        POST /v1/messages without tools returns a non-empty text block
#   3. tool_use    POST /v1/messages with a get_weather tool returns a get_weather
#                  tool_use block with an id and a non-empty input.city
#   4. tool_result the assistant turn plus a tool_result for that id are sent back; the
#                  reply has a non-empty text block and stop_reason is not tool_use
# count_tokens is reported as information only, as smoke.ps1 did (Claude Code falls back
# to an estimate without it). Exit 0 = PASS; otherwise the failed step is printed and the
# exit code is 1.
#
# Usage: scripts/smoke.sh [--port N] [--max-tokens N] [--model NAME]
# The key is the first line of logs/api-key (written by scripts/serve.sh on each start);
# it goes to curl through a mode-600 config file, never on a command line.
# Ran against a live server on 2026-11-14 (llama-server 1.0.0 build 12359; both manifest models; smoke PASS; bench logs under bench/results/).
set -u
umask 077

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd -P) || exit 1
ROOT=$(cd "$SCRIPT_DIR/.." && pwd -P) || exit 1
CONF="$ROOT/config/lis.conf"
KEYFILE="$ROOT/logs/api-key"

fail() { printf 'FAIL: %s\n' "$*"; exit 1; }

PORT=""
MAX_TOKENS=4096
MODEL=local
while [ $# -gt 0 ]; do
    case $1 in
        --port) [ $# -ge 2 ] || fail "--port needs a value"; PORT=$2; shift ;;
        --max-tokens) [ $# -ge 2 ] || fail "--max-tokens needs a value"; MAX_TOKENS=$2; shift ;;
        --model) [ $# -ge 2 ] || fail "--model needs a value"; MODEL=$2; shift ;;
        *) fail "unknown argument: $1 (usage: scripts/smoke.sh [--port N] [--max-tokens N] [--model NAME])" ;;
    esac
    shift
done
if [ -z "$PORT" ] && [ -f "$CONF" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        case $line in PORT=*) PORT=${line#PORT=} ;; esac
    done < "$CONF"
fi
re_port='^[1-9][0-9]{0,4}$'
[[ $PORT =~ $re_port ]] && [ "$PORT" -le 65535 ] || fail "port must be 1-65535, got '$PORT'"
re_num='^[1-9][0-9]{0,5}$'
[[ $MAX_TOKENS =~ $re_num ]] || fail "--max-tokens must be a positive integer, got '$MAX_TOKENS'"
re_model='^[A-Za-z0-9._:-]+$'
[[ $MODEL =~ $re_model ]] || fail "--model may hold only [A-Za-z0-9._:-], got '$MODEL'"

PY=$(command -v python3.13) || fail "python3.13 not found on PATH"
[ -f "$KEYFILE" ] || fail "no key file $KEYFILE: start the server with scripts/serve.sh"
KEY=""
IFS= read -r KEY < "$KEYFILE" || true
re_key='^[0-9a-f]{64}$'
[[ $KEY =~ $re_key ]] || fail "$KEYFILE does not hold a 64-hex key"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/lis-smoke.XXXXXX") || fail "mktemp failed"
trap 'rm -rf "$TMP"' EXIT
printf 'header = "x-api-key: %s"\nheader = "anthropic-version: 2023-06-01"\nheader = "content-type: application/json"\n' "$KEY" > "$TMP/curl.cfg"
unset KEY
BASE="http://127.0.0.1:$PORT"

# Builds request bodies and checks replies. argv: mode, then files.
PY_SMOKE='
import json, sys
mode = sys.argv[1]
TOOLS = [{
    "name": "get_weather",
    "description": "Get the current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name, e.g. Mexico City"}},
        "required": ["city"],
    },
}]
ASK = ("What is the weather in Mexico City right now? "
       "You must call the get_weather tool; do not answer from memory.")
def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)
def save(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)
def show(resp):
    print("  stop_reason: %s" % resp.get("stop_reason"))
    for b in resp.get("content") or []:
        t = b.get("type")
        if t == "text":
            print("  [text] %s" % b.get("text", "")[:300])
        elif t == "tool_use":
            print("  [tool_use] %s %s" % (b.get("name"), json.dumps(b.get("input"))))
        elif t == "thinking":
            print("  [thinking] %d chars" % len(b.get("thinking") or ""))
        else:
            print("  [%s]" % t)
def texts(resp):
    return [b.get("text") or "" for b in resp.get("content") or []
            if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()]
def die(msg):
    print(msg)
    sys.exit(1)
try:
    if mode == "health":
        h = load(sys.argv[2])
        print("  health: %s" % json.dumps(h))
        if not isinstance(h, dict) or h.get("status") != "ok":
            die("/health status is %r, expected \"ok\"" % (h.get("status") if isinstance(h, dict) else h))
    elif mode == "body-text":
        save(sys.argv[2], {"model": sys.argv[3], "max_tokens": int(sys.argv[4]),
             "messages": [{"role": "user", "content": "Reply with one short sentence: what is 2 + 2?"}]})
    elif mode == "check-text":
        r = load(sys.argv[2]); show(r)
        if not texts(r):
            die("no non-empty text block in the reply (if only thinking shows, raise --max-tokens)")
    elif mode == "body-tool":
        save(sys.argv[2], {"model": sys.argv[3], "max_tokens": int(sys.argv[4]), "tools": TOOLS,
             "messages": [{"role": "user", "content": ASK}]})
    elif mode == "count-body":
        save(sys.argv[2], {"model": sys.argv[3], "tools": TOOLS, "messages": [{"role": "user", "content": ASK}]})
    elif mode == "count-show":
        print("  count_tokens: input_tokens=%s" % load(sys.argv[2]).get("input_tokens"))
    elif mode == "check-tool":
        req, r = load(sys.argv[2]), load(sys.argv[3]); show(r)
        calls = [b for b in r.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"
                 and b.get("name") == "get_weather" and b.get("id")
                 and isinstance(b.get("input"), dict) and str(b["input"].get("city") or "").strip()]
        if not calls:
            die("no get_weather tool_use block with an id and a non-empty input.city")
        call = calls[0]
        req["messages"] = req["messages"] + [
            {"role": "assistant", "content": r["content"]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call["id"],
                                          "content": "22 C, clear sky, light wind from the north."}]},
        ]
        save(sys.argv[4], req)
    elif mode == "check-final":
        r = load(sys.argv[2]); show(r)
        if r.get("stop_reason") == "tool_use":
            die("the model called a tool again instead of answering from the tool_result")
        if not texts(r):
            die("no non-empty text block after the tool_result")
    else:
        die("unknown mode %s" % mode)
except (OSError, ValueError, KeyError, TypeError) as e:
    die("%s: %s" % (mode, e))
'
py() { "$PY" -I -c "$PY_SMOKE" "$@"; }
# post PATH BODYFILE OUTFILE TIMEOUT -> prints the HTTP status
post() {
    curl -sS --max-time "$4" -K "$TMP/curl.cfg" -o "$3" -w '%{http_code}' --data-binary "@$2" "$BASE$1"
}

echo "1. health ($BASE/health)"
code=$(curl -sS --max-time 10 -o "$TMP/health.json" -w '%{http_code}' "$BASE/health") || fail "health: /health unreachable at $BASE"
[ "$code" = 200 ] || fail "health: HTTP $code from /health"
py health "$TMP/health.json" || fail "health"

echo "2. text (/v1/messages, no tools)"
py body-text "$TMP/text.req" "$MODEL" "$MAX_TOKENS" || fail "text: could not build the request"
code=$(post /v1/messages "$TMP/text.req" "$TMP/text.json" 600) || fail "text: request failed"
[ "$code" = 200 ] || fail "text: HTTP $code: $(head -c 400 "$TMP/text.json")"
py check-text "$TMP/text.json" || fail "text"

echo "3. tool_use (/v1/messages with get_weather)"
py body-tool "$TMP/tool.req" "$MODEL" "$MAX_TOKENS" || fail "tool_use: could not build the request"
code=$(post /v1/messages "$TMP/tool.req" "$TMP/tool.json" 600) || fail "tool_use: request failed"
[ "$code" = 200 ] || fail "tool_use: HTTP $code: $(head -c 400 "$TMP/tool.json")"
py check-tool "$TMP/tool.req" "$TMP/tool.json" "$TMP/final.req" || fail "tool_use"

echo "4. tool_result (assistant turn + tool_result sent back)"
code=$(post /v1/messages "$TMP/final.req" "$TMP/final.json" 600) || fail "tool_result: request failed"
[ "$code" = 200 ] || fail "tool_result: HTTP $code: $(head -c 400 "$TMP/final.json")"
py check-final "$TMP/final.json" || fail "tool_result"

echo "info: count_tokens (/v1/messages/count_tokens, not fatal)"
if py count-body "$TMP/count.req" "$MODEL" \
    && code=$(post /v1/messages/count_tokens "$TMP/count.req" "$TMP/count.json" 60) && [ "$code" = 200 ]; then
    py count-show "$TMP/count.json" || echo "  count_tokens: unreadable reply"
else
    echo "  count_tokens: unavailable (HTTP ${code:-none})"
fi

echo "PASS: health, text, tool_use and tool_result round trip"
exit 0
