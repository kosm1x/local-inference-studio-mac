#!/bin/bash
# bench/run.sh - benchmark the RUNNING llama-server on 127.0.0.1:PORT, write one JSON.
#
# macOS port of bench/run.ps1 (Windows reference, commit cafc34b), llama backend only.
# Measures, in this order:
#   a. decode tok/s: one discarded warm-up request (the first request after a start is
#      slow), then the median of --runs /completion requests (200-token prompt, 256
#      tokens predicted, cache_prompt false, ignore_eos);
#   b. prefill: the median of --runs cold /completion requests on a --prefill-tokens
#      prompt (cache_prompt false, n_predict 1), then one prefix-cached pair (the first
#      fills the slot cache, the second is the hit);
#   c. tool calls: --tool-trials /v1/messages requests, each needing one of two tools;
#   d. memory: system used memory (wired + active + compressed pages from vm_stat) and
#      the server's RSS, sampled every 0.5 s; peaks recorded. Unified memory has no
#      separate VRAM figure, so vram_mb_peak is null; whether these peaks are a fit
#      measure for Metal buffers is UNVERIFIED.
# Writes bench/results/<yyyyMMdd-HHmmss>-<label>.json (keys of the reference schema, plus
# server_model, mem_used_mb_peak and server_rss_mb_peak) and tees the whole console to
# bench/results/<same stamp>-<label>.log: a number without its saved log is unverified.
# It does not start or stop the server; --label, --quant, --ctx and --ngl are recorded
# as given, while server_build, server_model and flags are read from the server (/props
# and the listening process's command line).
#
# The filler text is seeded (Python random.Random(42)) and so differs token for token
# from the PowerShell original (.NET Random(42)): Windows and macOS prefill figures are
# not the same prompt.
#
# Usage: bench/run.sh --label NAME [--quant Q] [--ctx N] [--ngl V] [--runs N]
#                     [--tool-trials N] [--prefill-tokens N] [--port N] [--out-dir DIR]
# UNVERIFIED: never run against a live server on this machine (no model chosen yet).
set -u

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd -P) || exit 1
ROOT=$(cd "$SCRIPT_DIR/.." && pwd -P) || exit 1
CONF="$ROOT/config/lis.conf"
KEYFILE="$ROOT/logs/api-key"

die() { printf 'bench/run.sh: %s\n' "$*" >&2; exit 1; }

LABEL=""; QUANT=""; CTX=0; NGL=auto; RUNS=3; TOOL_TRIALS=20; PREFILL=20000; PORT=""
OUT_DIR="$SCRIPT_DIR/results"
while [ $# -gt 0 ]; do
    [ $# -ge 2 ] || die "$1 needs a value"
    case $1 in
        --label) LABEL=$2 ;;
        --quant) QUANT=$2 ;;
        --ctx) CTX=$2 ;;
        --ngl) NGL=$2 ;;
        --runs) RUNS=$2 ;;
        --tool-trials) TOOL_TRIALS=$2 ;;
        --prefill-tokens) PREFILL=$2 ;;
        --port) PORT=$2 ;;
        --out-dir) OUT_DIR=$2 ;;
        *) die "unknown argument: $1" ;;
    esac
    shift 2
done
[ -n "$LABEL" ] || die "--label is required"
re_n='^[0-9]{1,7}$'
for pair in "ctx:$CTX" "runs:$RUNS" "tool-trials:$TOOL_TRIALS" "prefill-tokens:$PREFILL"; do
    [[ ${pair#*:} =~ $re_n ]] || die "--${pair%%:*} must be a whole number, got '${pair#*:}'"
done
[ "$RUNS" -ge 1 ] || die "--runs must be at least 1"
[ "$PREFILL" -ge 200 ] || die "--prefill-tokens must be at least 200"
if [ -z "$PORT" ] && [ -f "$CONF" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        case $line in PORT=*) PORT=${line#PORT=} ;; esac
    done < "$CONF"
fi
re_port='^[1-9][0-9]{0,4}$'
[[ $PORT =~ $re_port ]] && [ "$PORT" -le 65535 ] || die "port must be 1-65535, got '$PORT'"
PY=$(command -v python3.13) || die "python3.13 not found on PATH"
[ -f "$KEYFILE" ] || die "no key file $KEYFILE: start the server with scripts/serve.sh"

SAFE_LABEL=$(printf '%s' "$LABEL" | tr -c 'A-Za-z0-9_.-' '_')
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$OUT_DIR" || die "cannot create $OUT_DIR"
JSON="$OUT_DIR/$STAMP-$SAFE_LABEL.json"
LOG="$OUT_DIR/$STAMP-$SAFE_LABEL.log"

PY_BENCH='
import json, random, re, statistics, subprocess, sys, threading, time, urllib.error, urllib.request
(port, keyfile, label, quant, ctx, ngl, runs, tool_trials, prefill_n, out_json) = sys.argv[1:11]
ctx, runs, tool_trials, prefill_n = int(ctx), int(runs), int(tool_trials), int(prefill_n)
base = "http://127.0.0.1:%s" % port
with open(keyfile, encoding="utf-8") as f:
    key = f.readline().strip()
bearer = {"Authorization": "Bearer " + key}
anthropic = {"x-api-key": key, "anthropic-version": "2023-06-01"}

def call(path, body=None, headers=None, timeout=1800):
    data = None if body is None else json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers if headers is not None else bearer)
    req = urllib.request.Request(base + path, data=data, headers=h, method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None

def rnd(x, n):
    return None if x is None else round(x, n)

health = call("/health", headers={})
if health.get("status") != "ok":
    sys.exit("/health status is %r, expected ok" % health.get("status"))
props = call("/props")
server_build = props.get("build_info")
server_model = props.get("model_path")

# The listening process: its pid (for the RSS sampler) and its command line (flags).
server_pid, flags = None, None
try:
    out = subprocess.run(["lsof", "-nP", "-iTCP:%s" % port, "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True, timeout=10).stdout.split()
    if out and re.fullmatch(r"[1-9][0-9]*", out[0]):
        server_pid = out[0]
        args = subprocess.run(["ps", "-p", server_pid, "-o", "args="],
                              capture_output=True, text=True, timeout=10).stdout.strip()
        flags = re.sub(r"^\S+\s*", "", args)
except (OSError, subprocess.SubprocessError):
    pass

# Deterministic English filler: seeded word lists, so every run sends the same tokens.
rng = random.Random(42)
subj = ["The engineer", "A small team", "The old library", "Our customer", "The river", "A quiet student",
        "The night shift", "The committee", "A delivery driver", "The museum guide", "The weather station", "My neighbor"]
verb = ["reviewed", "carried", "described", "repaired", "measured", "painted", "questioned", "organized",
        "translated", "photographed", "ignored", "celebrated"]
obj = ["the broken bridge", "a long report", "the morning schedule", "three wooden chairs", "the harbor lights",
       "an unfinished map", "the spring harvest", "a stack of letters", "the city budget", "the northern trail"]
tail = ["before sunrise.", "with great care.", "during the storm.", "after a short debate.", "for the second time.",
        "without any help.", "near the old station.", "while the market closed.", "in under an hour.", "as planned."]
parts, tokens = [], []
while len(tokens) < prefill_n:
    for _ in range(3000):
        parts.append("%s %s %s %s " % (rng.choice(subj), rng.choice(verb), rng.choice(obj), rng.choice(tail)))
    tokens = call("/tokenize", {"content": "".join(parts)})["tokens"]
prefill_prompt = tokens[:prefill_n]
decode_prompt = tokens[:200]

# Memory sampler: system used (wired + active + compressed) and the server RSS, MiB.
peaks = {"used": None, "rss": None}
stop = threading.Event()
def vm_used_mib():
    out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    m = re.search(r"page size of (\d+)", out)
    if not m:
        return None
    pages = 0
    for k in ("Pages wired down", "Pages active", "Pages occupied by compressor"):
        mm = re.search(re.escape(k) + r":\s+(\d+)", out)
        pages += int(mm.group(1)) if mm else 0
    return pages * int(m.group(1)) // 1048576
def rss_mib():
    if not server_pid:
        return None
    out = subprocess.run(["ps", "-p", server_pid, "-o", "rss="], capture_output=True, text=True, timeout=5).stdout.strip()
    return int(out) // 1024 if out.isdigit() else None
def sampler():
    while not stop.is_set():
        for name, fn in (("used", vm_used_mib), ("rss", rss_mib)):
            try:
                v = fn()
            except (OSError, subprocess.SubprocessError):
                v = None
            if v is not None and (peaks[name] is None or v > peaks[name]):
                peaks[name] = v
        stop.wait(0.5)
th = threading.Thread(target=sampler, daemon=True)
th.start()

try:
    # a. decode: one discarded warm-up request, then the measured runs.
    decode_body = {"prompt": decode_prompt, "n_predict": 256, "cache_prompt": False, "ignore_eos": True}
    t = call("/completion", decode_body)["timings"]
    print("decode warm-up (discarded): %.1f tok/s (%s tokens)" % (t["predicted_per_second"], t["predicted_n"]))
    decode_runs = []
    for r in range(1, runs + 1):
        t = call("/completion", decode_body)["timings"]
        print("decode run %d: %.1f tok/s (%s tokens)" % (r, t["predicted_per_second"], t["predicted_n"]))
        decode_runs.append(float(t["predicted_per_second"]))

    # b. prefill, cold then prefix-cached. cache_prompt=false makes each run cold by design.
    prefill_runs = []
    for r in range(1, runs + 1):
        t = call("/completion", {"prompt": prefill_prompt, "n_predict": 1, "cache_prompt": False})["timings"]
        print("prefill run %d: %.0f ms, %.0f tok/s (%s tokens)" % (r, t["prompt_ms"], t["prompt_per_second"], t["prompt_n"]))
        prefill_runs.append(t)
    call("/completion", {"prompt": prefill_prompt, "n_predict": 1, "cache_prompt": True})
    cached = call("/completion", {"prompt": prefill_prompt, "n_predict": 1, "cache_prompt": True})["timings"]
    print("prefill cached: %.0f ms (%s tokens evaluated)" % (cached["prompt_ms"], cached["prompt_n"]))

    # c. tool calls: two tools, each message needs exactly one of them.
    tools = [
        {"name": "get_weather", "description": "Get the current weather for a city.",
         "input_schema": {"type": "object", "required": ["city"],
                          "properties": {"city": {"type": "string", "description": "City name, e.g. Mexico City"}}}},
        {"name": "run_tests", "description": "Run the test suite at a path in the repository and return the results.",
         "input_schema": {"type": "object", "required": ["path"],
                          "properties": {"path": {"type": "string", "description": "Test file or directory, e.g. tests/unit"}}}},
    ]
    cases = [
        ("get_weather", "city", "What is the weather in Mexico City right now?"),
        ("run_tests", "path", "Run the tests in tests/unit and tell me what fails."),
        ("get_weather", "city", "Is it raining in London at the moment?"),
        ("run_tests", "path", "Please execute the test file src/parser.test.js."),
        ("get_weather", "city", "How hot is it in Tokyo today?"),
        ("run_tests", "path", "I changed the auth module; run the tests under tests/auth."),
        ("get_weather", "city", "Check the current weather in Buenos Aires for me."),
        ("run_tests", "path", "Run test/slugify.test.js and report the result."),
        ("get_weather", "city", "Should I bring a coat in Toronto right now? Look up the weather."),
        ("run_tests", "path", "Kick off the integration tests in tests/integration."),
        ("get_weather", "city", "What are the current conditions in Madrid?"),
        ("run_tests", "path", "Verify my fix by running the tests at packages/core/test."),
        ("get_weather", "city", "Get me the live weather for Sydney."),
        ("run_tests", "path", "Run the e2e suite located at e2e/."),
        ("get_weather", "city", "Is it windy in Chicago right now?"),
        ("run_tests", "path", "Run the tests in lib/utils/__tests__ please."),
        ("get_weather", "city", "What temperature is it in Cairo at this moment?"),
        ("run_tests", "path", "Can you run spec/models/user_spec.rb?"),
        ("get_weather", "city", "Tell me the current weather in Oaxaca."),
        ("run_tests", "path", "Run the regression tests in tests/regression and summarize."),
    ]
    passed = wrong = no_call = bad_args = errors = 0
    latencies = []
    for i in range(tool_trials):
        want, field, msg = cases[i % len(cases)]
        body = {"model": "local", "max_tokens": 4096, "tools": tools, "messages": [{"role": "user", "content": msg}]}
        t0 = time.monotonic()
        try:
            resp = call("/v1/messages", body, anthropic)
        except (OSError, ValueError, urllib.error.URLError) as e:
            errors += 1
            print("tool %d: ERROR %s" % (i + 1, e))
            continue
        ms = (time.monotonic() - t0) * 1000
        latencies.append(ms)
        uses = [b for b in resp.get("content") or [] if isinstance(b, dict) and b.get("type") == "tool_use"]
        hit = next((u for u in uses if u.get("name") == want), None)
        if not uses:
            no_call += 1; verdict = "no_call"
        elif hit is None:
            wrong += 1; verdict = "wrong_tool (%s)" % uses[0].get("name")
        elif not str((hit.get("input") or {}).get(field) or "").strip():
            bad_args += 1; verdict = "bad_args"
        else:
            passed += 1; verdict = "pass"
        print("tool %d: %s %s ms" % (i + 1, verdict, format(round(ms), ",")))
finally:
    stop.set()
    th.join(timeout=5)

result = {
    "model": label,
    "quant": quant,
    "ctx": ctx,
    "ngl": ngl,
    "n_cpu_moe": 0,
    "decode_tps": rnd(median(decode_runs), 2),
    "prefill_ms_20k": rnd(median([t["prompt_ms"] for t in prefill_runs]), 1),
    "prefill_tps": rnd(median([t["prompt_per_second"] for t in prefill_runs]), 1),
    "prefill_tokens": int(prefill_runs[0]["prompt_n"]),
    "prefill_ms_20k_cached": rnd(float(cached["prompt_ms"]), 1),
    "prefill_cached_tokens_eval": int(cached["prompt_n"]),
    "toolcall_pass": passed,
    "toolcall_total": tool_trials,
    "toolcall_wrong_tool": wrong,
    "toolcall_no_call": no_call,
    "toolcall_bad_args": bad_args,
    "toolcall_errors": errors,
    "toolcall_latency_ms_median": rnd(median(latencies), 0) if latencies else None,
    "vram_mb_peak": None,
    "mem_used_mb_peak": peaks["used"],
    "server_rss_mb_peak": peaks["rss"],
    "server_build": server_build,
    "server_model": server_model,
    "flags": flags,
    "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
}
with open(out_json, "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2)
    f.write("\n")
print("%s %s: decode %s tok/s | prefill %s ms (%s tok/s), cached %s ms | tools %d/%d (wrong %d, none %d, args %d, err %d, p50 %s ms) | mem used peak %s MiB, server rss peak %s MiB -> %s" % (
    label, quant, result["decode_tps"], result["prefill_ms_20k"], result["prefill_tps"], result["prefill_ms_20k_cached"],
    passed, tool_trials, wrong, no_call, bad_args, errors, result["toolcall_latency_ms_median"],
    result["mem_used_mb_peak"], result["server_rss_mb_peak"], out_json))
'

{
    printf 'bench/run.sh: %s label=%s quant=%s ctx=%s ngl=%s runs=%s tool_trials=%s prefill_tokens=%s port=%s\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$LABEL" "$QUANT" "$CTX" "$NGL" "$RUNS" "$TOOL_TRIALS" "$PREFILL" "$PORT"
    "$PY" -I -u -c "$PY_BENCH" "$PORT" "$KEYFILE" "$LABEL" "$QUANT" "$CTX" "$NGL" "$RUNS" "$TOOL_TRIALS" "$PREFILL" "$JSON"
    code=$?
    printf 'bench/run.sh: exit %s; log %s\n' "$code" "$LOG"
    exit "$code"
} 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
