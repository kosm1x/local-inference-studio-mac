"""claude_local_test: scripts/claude-local.sh with no live server and no real claude.

Run from the repo root: python3.13 -I scripts/tests/claude_local_test.py

Each case builds a throwaway repo tree in a temp dir (a copy of claude-local.sh, the
settings and the hooks, a written config/lis.conf and logs/api-key, and a stub
scripts/serve.sh) and a bin dir first on PATH with stub `claude` and `curl`. Every stub
appends one JSON line to a call log (who, argv, curl's stdin, claude's env and the state
of its run dir while it runs). The stub curl plays the server from state files: `up`
(GET /health answers ok), `busy` (a slot is processing) and `server-key` (the key the
"server" accepts on /slots). The stub serve.sh writes a new key to logs/api-key and
server-key and marks the server up, as the real one does at every start.

Asserted: the static and the observed call order (refusals < server start < key read <
preflight < run dir < claude); the full env block given to claude and the cleared
credential, routing and LIS_ variables; the key absent from every argv and from the
launcher's stdout and stderr; the loopback refusal; the run dir's mode (700), name and
read-only hook copies, the settings it holds (hooks, denies, no `~/` left) and their
removal at exit; every refusal starting nothing (no curl, no serve.sh, no claude, no run
dir); --full, --no-guard, --no-loop-guard, --strict-slot, --print-args; claude's exit
code passed through. Also: the pass-through allow-list (each refused option named, a
subcommand as the first operand, option words after `--`, every word of a multi-value
list, --permission-mode choices, the allowed options passed unchanged); the turn cap
(--max-turns 1..10000 accepted once, other values refused, the default --max-turns 60
added in print mode only, ahead of the caller's arguments, with a status line); the working
directory's .claude/settings*.json refusals; the flag-level pins in the per-run settings;
the run dir's digits-only name and a planted path never reused (stub `od` and `date`);
the env scrub before serve.sh (its env recorded); an inherited SHELLOPTS=xtrace printing
no key; the tool trust checks (world-writable, foreign owner) and the test-only override
LIS_TEST_CLIENT_BIN (python3.13, curl and claude come from the stub bin dir through it;
it is announced on stderr and reaches neither serve.sh nor claude). The session shell:
CLAUDE_CODE_SHELL and SHELL are /bin/bash in claude's env and in the --settings file
whatever the caller held (zsh, a missing path, empty), the caller's
CLAUDE_CODE_SHELL_PREFIX and CLAUDE_ENV_FILE are cleared, a project file naming any of the
four refuses, the status line names the shell, and the /bin/bash check refuses a
missing, non-executable, world-writable or relative path (swapped into the test copy).
Bash start-up: exported functions (BASH_FUNC_*) and PS4, PROMPT_COMMAND, POSIXLY_CORRECT,
INPUTRC from the caller reach neither serve.sh nor claude, and a project file whose env
names a bash start-up variable or a BASH_FUNC_ name refuses (surrounding whitespace
ignored). Exported functions named -x, --, -, -f, a-b, a.b, ls, say, which bash imports
outside posix mode only, are removed in runs without POSIXLY_CORRECT too, and a surviving
one (removal swapped out of the test copy) refuses the launch. Every bash 3.2 builtin
(plus ls, die, say) as a caller function, one per run: every run ends, only set and
builtin run (the documented limit), and every other name launches with no function.
W-19: no curl upload
deny in config/claude-settings.json (none re-spelled), the curl upload ask rows present, no
allow row for curl, wget or any command, no allow row matching an upload, no deny and some
ask row matching each loopback upload, and lis-guard denying the non-loopback ones.
Stdlib only. Nothing here starts a real server or a real claude.
"""
import concurrent.futures
import importlib.util
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, 'scripts', 'claude-local.sh')
HOOKS = ('lis-guard.py', 'loop-guard.py', 'repeat-guard.py', 'facts-inject.py')
LEAN_TOOLS = 'Bash,Read,Edit,Write,Glob,Grep,Skill'
CONF = 'PORT=8080\nCTX=131072\nCLIENT_CTX=114688\nKV_TYPE=q4_0\nMODEL_ID=\nCHAT_TEMPLATE=\n'
# The variables the launch env is poisoned with; none may reach claude.
POISON = {
    'ANTHROPIC_API_KEY': 'sk-ant-poison',
    'ANTHROPIC_BASE_URL': 'https://api.example.invalid',
    'ANTHROPIC_AUTH_TOKEN': 'poison-token',
    'ANTHROPIC_CUSTOM_HEADERS': 'X-Poison: 1',
    'ANTHROPIC_SMALL_FAST_MODEL': 'poison',
    'ANTHROPIC_BEDROCK_BASE_URL': 'https://bedrock.example.invalid',
    'CLAUDE_CODE_OAUTH_TOKEN': 'poison-oauth',
    'CLAUDE_CODE_SUBAGENT_MODEL': 'opus',
    'CLAUDE_CODE_SUBAGENT_MODEL_FORCE': '1',
    'CLAUDE_CODE_USE_BEDROCK': '1',
    'CLAUDE_CODE_USE_VERTEX': '1',
    'AWS_BEARER_TOKEN_BEDROCK': 'poison-aws',
    'CLOUD_ML_REGION': 'poison-region',
    'LIS_RELAUNCH': '1',
    'LIS_RELAUNCH_RC': '3',
    'LIS_FACTS_FILE': 'poison.md',
    'LIS_API_KEY': 'poison',
    'HTTPS_PROXY': 'http://proxy.example.invalid:3128',
    'https_proxy': 'http://proxy.example.invalid:3128',
    'HTTP_PROXY': 'http://proxy.example.invalid:3128',
    'ALL_PROXY': 'socks5://proxy.example.invalid:1080',
    'NO_PROXY': '*',
    'no_proxy': '*',
    # D: start-up code, trust stores, config dir and parent-session markers (benign values;
    # DYLD_* is checked statically, since SIP strips it before /bin/bash runs the script)
    'NODE_OPTIONS': '--poison-node-option',
    'NODE_EXTRA_CA_CERTS': '/nonexistent/poison-ca.pem',
    'SSL_CERT_FILE': '/nonexistent/poison-cert.pem',
    'CURL_CA_BUNDLE': '/nonexistent/poison-curl.pem',
    'REQUESTS_CA_BUNDLE': '/nonexistent/poison-requests.pem',
    'BUN_OPTIONS': '--poison-bun-option',
    'BASH_ENV': '/nonexistent/poison-bash-env',
    'ENV': '/nonexistent/poison-env',
    'PYTHONSTARTUP': '/nonexistent/poison-startup.py',
    'PYTHONPATH': '/nonexistent/poison-path',
    'PYTHONWARNINGS': 'ignore',
    'CLAUDE_CONFIG_DIR': '/nonexistent/poison-config',
    'CLAUDECODE': '1',
    'CLAUDE_EFFORT': 'poison-effort',
    'AI_AGENT': 'poison-agent',
    'BUN_INSTALL': '/nonexistent/poison-bun',
    'SSL_CERT_DIR': '/nonexistent/poison-certs',
    'CLAUDE_PID': '1',
    # the session shell and what the client runs around or before a shell command (SHELL
    # is not here: it is not scrubbed before serve.sh; session_shell() checks it)
    'CLAUDE_CODE_SHELL': '/bin/zsh',
    'CLAUDE_CODE_SHELL_PREFIX': '/nonexistent/poison-shell-prefix',
    'CLAUDE_ENV_FILE': '/nonexistent/poison-env-file',
    # what bash runs or reads at start-up or around a command (benign values), and exported
    # functions (bash imports these as functions; serve.sh records `declare -F` for them)
    'PS4': '+$(echo poison-ps4) ',
    'PROMPT_COMMAND': 'echo poison-prompt-command',
    'POSIXLY_CORRECT': '1',
    'INPUTRC': '/nonexistent/poison-inputrc',
    'BASH_FUNC_ls%%': '() { echo poison-ls; }',
    'BASH_FUNC_say%%': '() { echo poison-say; }',
    # the server's test overrides: must not reach serve.sh --detach
    'LIS_TEST_LLAMA_SERVER': '/nonexistent/poison-llama-server',
    'LIS_TEST_LLAMA_EXE': '/nonexistent/poison-llama-exe',
}
OVERRIDE = 'LIS_TEST_CLIENT_BIN'
WANT_ENV = {
    'ANTHROPIC_BASE_URL': 'http://127.0.0.1:8080',
    'ANTHROPIC_MODEL': 'local',
    'ANTHROPIC_DEFAULT_OPUS_MODEL': 'local',
    'ANTHROPIC_DEFAULT_SONNET_MODEL': 'local',
    'ANTHROPIC_DEFAULT_HAIKU_MODEL': 'local',
    'CLAUDE_CODE_MAX_CONTEXT_TOKENS': '114688',
    'CLAUDE_CODE_AUTO_COMPACT_WINDOW': '114688',
    'CLAUDE_CODE_MAX_OUTPUT_TOKENS': '16384',
    'API_TIMEOUT_MS': '1800000',
    'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS': '1',
    'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
    'DISABLE_PROMPT_CACHING': '1',
    'ENABLE_TOOL_SEARCH': 'false',
    'CLAUDE_CODE_ATTRIBUTION_HEADER': '0',
    'CLAUDE_CODE_SHELL': '/bin/bash',
    'SHELL': '/bin/bash',
}
SESSION_SHELL = '/bin/bash'
SHELL_ENV = ('CLAUDE_CODE_SHELL', 'SHELL', 'CLAUDE_CODE_SHELL_PREFIX', 'CLAUDE_ENV_FILE')
SHELL_LINE = 'claude-local: session shell: /bin/bash (pinned as CLAUDE_CODE_SHELL and SHELL)'
START_RE = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z')

N = 0
FAILS = 0


def check(cond, label):
    global N, FAILS
    N += 1
    if not cond:
        FAILS += 1
        print(f'FAIL {label}')
    return cond


def python313():
    p = shutil.which('python3.13')
    if p:
        return p
    if os.path.basename(sys.executable).startswith('python3.13'):
        return sys.executable
    raise SystemExit('python3.13 not found on PATH')


CURL_STUB = '''#!{py} -I
import json, os, sys
T = {t!r}
argv = sys.argv[1:]
data = sys.stdin.read() if '-K' in argv else ''
with open(os.path.join(T, 'calls.jsonl'), 'a') as f:
    f.write(json.dumps({{'who': 'curl', 'argv': argv, 'stdin': data}}) + '\\n')
url = argv[-1] if argv else ''
st = os.path.join(T, 'state')
if not os.path.exists(os.path.join(st, 'up')):
    sys.stderr.write('curl: (7) Failed to connect to 127.0.0.1 port 8080\\n')
    sys.exit(7)
if url.endswith('/health'):
    print('{{"status":"ok"}}')
    sys.exit(0)
if url.endswith('/slots'):
    with open(os.path.join(st, 'server-key')) as f:
        want = f.read().strip()
    if 'header = "Authorization: Bearer ' + want + '"' not in data:
        sys.stderr.write('curl: (22) The requested URL returned error: 401\\n')
        sys.exit(22)
    busy = os.path.exists(os.path.join(st, 'busy'))
    print(json.dumps([{{'id': 0, 'is_processing': busy}}, {{'id': 1, 'is_processing': False}}]))
    sys.exit(0)
sys.exit(3)
'''

CLAUDE_STUB = '''#!{py} -I
import json, os, stat, sys
T = {t!r}
argv = sys.argv[1:]
rec = {{'who': 'claude', 'argv': argv, 'env': dict(os.environ)}}
if '--settings' in argv:
    s = argv[argv.index('--settings') + 1]
    d = os.path.dirname(s)
    rec['run_dir'] = d
    rec['run_mode'] = stat.S_IMODE(os.lstat(d).st_mode)
    rec['run_files'] = {{n: stat.S_IMODE(os.lstat(os.path.join(d, n)).st_mode)
                        for n in sorted(os.listdir(d))}}
    with open(s) as f:
        rec['settings'] = json.load(f)
    m = argv[argv.index('--mcp-config') + 1]
    with open(m) as f:
        rec['mcp'] = json.load(f)
with open(os.path.join(T, 'calls.jsonl'), 'a') as f:
    f.write(json.dumps(rec) + '\\n')
print('claude stub ran')
rc = os.path.join(T, 'state', 'claude-rc')
sys.exit(int(open(rc).read()) if os.path.exists(rc) else 0)
'''

SERVE_STUB = '''#!/bin/bash
T='{t}'
printf '{{"who":"serve","argv":["%s"]}}\\n' "$*" >> "$T/calls.jsonl"
compgen -e > "$T/state/serve-env"
declare -F > "$T/state/serve-funcs"
printf '%s\\n' "$-" > "$T/state/serve-flags"
[ -e "$T/state/serve-fails" ] && exit 1
printf '%s\\n' '{key}' > "$T/repo/logs/api-key"
printf '%s\\n' '{key}' > "$T/state/server-key"
: > "$T/state/up"
exit 0
'''


class Env:
    """One throwaway repo, bin dir, home and temp dir."""

    def __init__(self, conf=CONF):
        self.t = os.path.realpath(tempfile.mkdtemp(prefix='cl-test-'))
        self.repo = os.path.join(self.t, 'repo')
        self.key0 = secrets.token_hex(32)
        self.key1 = secrets.token_hex(32)
        for d in ('scripts/hooks', 'scripts/tests', 'config', 'logs'):
            os.makedirs(os.path.join(self.repo, d))
        for d in ('bin', 'home', 'tmp', 'state', 'work'):
            os.makedirs(os.path.join(self.t, d))
        shutil.copy(SCRIPT, os.path.join(self.repo, 'scripts', 'claude-local.sh'))
        shutil.copy(os.path.join(REPO, 'config', 'claude-settings.json'),
                    os.path.join(self.repo, 'config', 'claude-settings.json'))
        for h in HOOKS:
            shutil.copy(os.path.join(REPO, 'scripts', 'hooks', h),
                        os.path.join(self.repo, 'scripts', 'hooks', h))
        self.write('repo/config/lis.conf', conf)
        self.write('repo/logs/api-key', self.key0 + '\n')
        self.write('state/server-key', self.key0 + '\n')
        py = python313()
        self.write('bin/curl', CURL_STUB.format(py=py, t=self.t), 0o755)
        self.write('bin/claude', CLAUDE_STUB.format(py=py, t=self.t), 0o755)
        self.write('repo/scripts/serve.sh', SERVE_STUB.format(t=self.t, key=self.key1), 0o755)
        # the launcher takes python3.13, curl and claude from the test-only override dir
        os.symlink(py, os.path.join(self.t, 'bin', 'python3.13'))
        self.env = dict(POISON)
        self.env.update({
            OVERRIDE: os.path.join(self.t, 'bin'),
            'PATH': os.pathsep.join([os.path.join(self.t, 'bin'), os.path.dirname(py),
                                     '/usr/bin', '/bin', '/usr/sbin', '/sbin']),
            'HOME': os.path.join(self.t, 'home'),
            'TMPDIR': os.path.join(self.t, 'tmp') + '/',
            'LANG': 'C',
        })

    def write(self, rel, text, mode=0o644):
        p = os.path.join(self.t, rel)
        with open(p, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        os.chmod(p, mode)
        return p

    def up(self, busy=False):
        self.write('state/up', '')
        if busy:
            self.write('state/busy', '')

    def run(self, *args, timeout=120):
        return subprocess.run(['/bin/bash', os.path.join(self.repo, 'scripts', 'claude-local.sh'), *args],
                              env=self.env, cwd=os.path.join(self.t, 'work'),
                              capture_output=True, text=True, timeout=timeout)

    def serve_env(self):
        """The exported names serve.sh saw, or None when it never ran."""
        p = os.path.join(self.t, 'state', 'serve-env')
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return set(f.read().split())

    def calls(self):
        p = os.path.join(self.t, 'calls.jsonl')
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [json.loads(line) for line in f if line.strip()]

    def run_dirs(self):
        return sorted(n for n in os.listdir(os.path.join(self.t, 'tmp')) if n.startswith('lis-hooks-'))

    def close(self):
        shutil.rmtree(self.t, ignore_errors=True)


def kind(c):
    """'curl /health', 'curl /slots', 'serve' or 'claude'."""
    if c['who'] == 'curl':
        return 'curl /' + c['argv'][-1].rsplit('/', 1)[-1]
    return c['who']


# Trace lines bash prints while a caller set function keeps xtrace on: the removal loop for
# the POISON functions and set, a handful of lines per function. The bound leaves room for
# a few more functions; the launcher past the removal traces hundreds of lines.
# Only lines up to the environment scrub are counted: once it unsets POISON's PS4 the trace
# lines carry no + prefix (with the second set +xv deleted: 165 + lines, then 856 more
# without one). The count depends on how many functions are exported (POISON's ls and say
# plus this test's set: 26 lines measured); no_key() is the check that carries the result.
XTRACE_REMOVAL_LINES = 40


def no_key(e, r, label):
    """The key (old or new) in no argv and not in the launcher's stdout or stderr."""
    for c in e.calls():
        for k in (e.key0, e.key1):
            check(not any(k in a for a in c['argv']), f'{label}: key in {c["who"]} argv')
    for k in (e.key0, e.key1):
        check(k not in r.stdout and k not in r.stderr, f'{label}: key on the console')


def static_order():
    with open(SCRIPT, encoding='utf-8') as f:
        src = f.read()
    marks = ['# ---------------------------------------------------------------- 1. refusals',
             '# ---------------------------------------------------------------- 2. server start',
             '"$SERVE" --detach',
             '# ---------------------------------------------------------------- 3. key read',
             '< "$KEY_FILE"',
             '# ---------------------------------------------------------------- 4. preflight',
             'mkdir -m 700 "$cand"',
             '# ---------------------------------------------------------------- 6. env and launch',
             '"$CLAUDE" --settings']
    pos = [src.find(m) for m in marks]
    check(all(p >= 0 for p in pos) and pos == sorted(pos), f'static order of the sections: {pos}')
    s2, s3 = pos[1], pos[3]
    check(src.count('"$SERVE" --detach') == 1, 'serve.sh is run in one place')
    check('$KEY_FILE' not in src[src.index('KEY_FILE=') + 9:s3], 'the key file is not touched before section 3')
    check(not re.search(r'(?m)^\s*exec\b', src), 'claude runs as a child (no exec)')
    pre = src[:s2]
    check(not re.search(r'"\$CURL"|"\$SERVE"|"\$CLAUDE"', pre), 'no curl, serve.sh or claude run before section 2')
    check('printf \'header = "Authorization: Bearer %s"\\n\' "$KEY"' in src, 'curl gets the key on stdin')
    check('mktemp' not in src and 'mkdir -p' not in src, 'run dir: no mktemp, no mkdir -p (an existing path is never reused)')
    # the scrub runs first, before section 1 and so before serve.sh --detach
    scrub = src.find('for name in $(compgen -e); do')
    check(0 <= scrub < pos[0] and src.count('$(compgen -e)') == 1,
          f'env: one scrub, before every section (at {scrub}, section 1 at {pos[0]})')
    pre_scrub = src[:scrub]
    check(pre_scrub.count('LIS_TEST_CLIENT_BIN') and not re.search(r'"\$(PY|CURL|SERVE|CLAUDE)"', pre_scrub),
          'env: nothing runs before the scrub but reading the override')
    code = [ln for ln in src.splitlines() if ln.strip() and not ln.lstrip().startswith('#')]
    check(code[0].startswith('set +xv'), f'xtrace off first thing: {code[0]!r}')
    check('export -n SHELLOPTS' in src, 'an inherited SHELLOPTS is unexported')
    # exported functions: removed before the script defines any (a redefined one stays exported)
    fx = src.find('builtin unset -f')
    check(0 <= fx < src.find('\ndie() {') and fx < scrub and src.find('fns=$(builtin declare -F)') < fx,
          'env: imported functions removed before die() and the variable scrub')
    check('builtin unset -f -- "${fn#declare -f* }"' in src, 'env: function names after --, so -x is removed too')
    # nothing but set +xv runs before the removal, whose loop condition is a keyword test
    # (builtin_sweep checks the behaviour), and set +xv runs again after the check
    check(code[:3] == ['set +xv    # an inherited SHELLOPTS=xtrace or verbose would print the key',
                       'fns=$(builtin declare -F)', 'while [[ -n $fns ]]; do'],
          f'env: the removal comes right after set +xv: {code[:3]!r}')
    fail_closed = src.find("an exported shell function survived the removal")
    check(0 < fail_closed < src.find('\nset +xv    # again') < src.find('\nset -u\n'),
          'env: set +xv again after the fail-closed check, before set -u')
    check(src.find('set +o posix') < src.find('\ndie() {') and 'set +o posix' in src,
          'env: posix mode (an inherited POSIXLY_CORRECT) switched off first')
    # SIP strips DYLD_* before /bin/bash starts, so no run can show the unset: read it.
    check(re.search(r'[|\\]\s*DYLD_\*\s*\|', src[scrub:pos[0]]) is not None, 'env: DYLD_* is in the cleared set')
    # tool locations: python3.13 from Homebrew folders, curl from /usr/bin, never PATH
    check('for d in /opt/homebrew/bin /usr/local/bin; do' in src and 'CURL_FOUND=/usr/bin/curl' in src
          and 'command -v python3.13' not in src and 'command -v curl' not in src,
          'tools: python3.13 from /opt/homebrew/bin then /usr/local/bin, curl from /usr/bin')
    check('2.1.322' in src and 'RE-READ IT ON' in src, 'allow-list: names the client it was read from')


def lean_run():
    e = Env()
    try:
        e.up()
        e.write('state/claude-rc', '7')
        r = e.run('-p', 'hello world', '--', 'and more')
        check(r.returncode == 7, f'lean: claude exit code passed through (got {r.returncode}, {r.stderr[-300:]!r})')
        check(r.stdout == 'claude stub ran\n', f'lean: stdout is claude\'s only: {r.stdout!r}')
        calls = e.calls()
        check([kind(c) for c in calls] == ['curl /health', 'curl /slots', 'claude'],
              f'lean: call order {[kind(c) for c in calls]}')
        no_key(e, r, 'lean')
        cl = [c for c in calls if c['who'] == 'claude']
        if not check(len(cl) == 1, 'lean: claude ran once'):
            return
        c = cl[0]
        a = c['argv']
        run_dir = c['run_dir']
        settings, mcp = a[1], a[a.index('--mcp-config') + 1]
        check(a[0] == '--settings' and os.path.dirname(settings) == run_dir
              and re.fullmatch(r'lis-settings-\d+\.json', os.path.basename(settings)) is not None,
              f'lean: --settings <run dir>/lis-settings-<pid>.json: {a[:2]}')
        check(a[2:10] == ['--setting-sources', 'project,local', '--tools', LEAN_TOOLS,
                          '--strict-mcp-config', '--mcp-config', mcp, '--append-system-prompt'],
              f'lean: lean arguments {a[2:10]}')
        check(re.fullmatch(r'lis-mcp-\d+\.json', os.path.basename(mcp)) is not None
              and os.path.dirname(mcp) == run_dir, 'lean: MCP file in the run dir')
        pol = a[10]
        check(pol.startswith('This session runs on one local model served by llama-server on this machine.')
              and 'the context window is 131,072 tokens.' in pol and 'never spawn subagents' in pol
              and 'For a file over 300 lines' in pol and 'Measure-Object' not in pol
              and 'nvidia-smi' not in pol, 'lean: policy text')
        check(a[11:13] == ['--max-turns', '60'], f'lean: print mode adds the default turn cap: {a[11:13]}')
        check(a[13:] == ['-p', 'hello world', '--', 'and more'], f'lean: caller arguments last, in order: {a[13:]}')
        env = c['env']
        for k, v in WANT_ENV.items():
            check(env.get(k) == v, f'lean: env {k}={env.get(k)!r}, want {v!r}')
        check(env.get('ANTHROPIC_AUTH_TOKEN') == e.key0, 'lean: ANTHROPIC_AUTH_TOKEN is the key file\'s key')
        check(START_RE.fullmatch(env.get('LIS_SESSION_START', '')) is not None,
              f'lean: LIS_SESSION_START {env.get("LIS_SESSION_START")!r}')
        fam = {k for k in env if re.match(r'(ANTHROPIC_|CLAUDE_CODE_|LIS_)', k)}
        want_fam = {k for k in WANT_ENV if re.match(r'(ANTHROPIC_|CLAUDE_CODE_|LIS_)', k)}
        want_fam |= {'ANTHROPIC_AUTH_TOKEN', 'LIS_SESSION_START'}
        check(fam == want_fam, f'lean: ANTHROPIC_/CLAUDE_CODE_/LIS_ set is exactly the block: '
                               f'extra {sorted(fam - want_fam)} missing {sorted(want_fam - fam)}')
        for k, v in POISON.items():
            if k in WANT_ENV or k == 'ANTHROPIC_AUTH_TOKEN':
                check(env.get(k) != v, f'lean: inherited {k} overridden')
            else:
                check(k not in env, f'lean: inherited {k} cleared')
        check(not any(k.lower().endswith('_proxy') for k in env), 'lean: no proxy variable')
        # the run dir while claude ran
        name = os.path.basename(run_dir)
        check(re.fullmatch(r'lis-hooks-[0-9-]+', name) is not None
              and re.fullmatch(r'lis-hooks-\d{8}-\d{6}-\d+-\d+', name) is not None,
              f'lean: run dir name lis-hooks-<digits and dashes>: {name}')
        check(os.path.dirname(run_dir) == os.path.realpath(os.path.join(e.t, 'tmp')), 'lean: run dir under TMPDIR')
        check(c['run_mode'] == 0o700, f'lean: run dir mode {oct(c["run_mode"])}')
        files = c['run_files']
        for h in ('lis-guard.py', 'loop-guard.py', 'repeat-guard.py'):
            check(files.get(h) == 0o444, f'lean: hook copy {h} mode {oct(files.get(h, 0))}')
            with open(os.path.join(run_dir, h), 'rb') as f1, \
                    open(os.path.join(REPO, 'scripts', 'hooks', h), 'rb') as f2:
                check(f1.read() == f2.read(), f'lean: hook copy {h} is the repo file')
        check('facts-inject.py' not in files, 'lean: facts-inject not copied (no longrun profile)')
        check(files.get(os.path.basename(settings)) == 0o400 and files.get(os.path.basename(mcp)) == 0o400,
              f'lean: settings and MCP files read-only: {files}')
        check(c['mcp'] == {'mcpServers': {}}, f'lean: empty MCP config {c["mcp"]}')
        s = c['settings']
        check(s.get('model') == 'local' and s['env'].get('CLAUDE_CODE_SUBAGENT_MODEL') == 'local',
              'lean: settings model and subagent model local')
        check(s['env'].get('CLAUDE_CODE_AUTO_COMPACT_WINDOW') == '114688'
              and s['env'].get('LIS_SESSION_START') == env.get('LIS_SESSION_START'),
              'lean: settings env agrees with the process env')
        # C(a): the flag-level pins
        check(s['env'].get('ANTHROPIC_BASE_URL') == 'http://127.0.0.1:8080' == env.get('ANTHROPIC_BASE_URL'),
              f'lean: settings pin env.ANTHROPIC_BASE_URL {s["env"].get("ANTHROPIC_BASE_URL")!r}')
        for k in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_SONNET_MODEL',
                  'ANTHROPIC_DEFAULT_HAIKU_MODEL'):
            check(s['env'].get(k) == 'local', f'lean: settings pin env.{k}')
        check(s.get('disableAllHooks') is False, f'lean: settings pin disableAllHooks false: {s.get("disableAllHooks")!r}')
        check('apiKeyHelper' not in s and 'ANTHROPIC_AUTH_TOKEN' not in s['env'], 'lean: no key setting in the file')
        blob = json.dumps(s)
        check(e.key0 not in blob and e.key1 not in blob, 'lean: the key is not in the settings file')
        rules = [r for k in ('allow', 'ask', 'deny') for r in s['permissions'].get(k, [])]
        check(not any(r.startswith(('Edit(~/', 'Read(~/')) for r in rules), 'lean: no ~/ rule left')
        home_rule = '/' + os.path.join(e.t, 'home')
        check(f'Read({home_rule}/.ssh/**)' in rules and f'Read({home_rule}/Library/Keychains/**)' in rules,
              'lean: ~/ rules rewritten to the home rule root')
        deny = s['permissions']['deny']
        rr = '/' + e.repo
        for g in ('scripts/claude-local.sh', 'scripts/serve.sh', 'scripts/stop.sh', 'config/**',
                  'scripts/hooks/**', 'logs/api-key', 'logs/llama-server.pid', 'models/manifest.json'):
            check(f'Edit({rr}/{g})' in deny, f'lean: deny Edit of {g} by absolute path')
        check(f'Edit(/{run_dir}/**)' in deny and 'Edit(//**/lis-hooks-*/**)' in deny
              and 'Edit(**/.claude/settings*.json)' in deny, 'lean: deny Edit of the run dir and settings files')
        hooks = s.get('hooks', {})
        py = os.path.realpath(python313())     # the launcher runs the checked real file
        pre = hooks.get('PreToolUse', [])
        check(len(pre) == 2 and pre[0]['matcher'] == 'Bash' and pre[1]['matcher'] == '*',
              f'lean: PreToolUse lis-guard then loop-guard: {[p.get("matcher") for p in pre]}')
        g = pre[0]['hooks'][0]
        check(g['command'] == f'"{py}" -I "{run_dir}/lis-guard.py" --hooks-dir "{e.repo}/scripts/hooks"'
              and g['timeout'] == 10 and g['onFailure'] == 'block', f'lean: lis-guard command {g}')
        lg = pre[1]['hooks'][0]
        check(lg['command'] == f'"{py}" -I "{run_dir}/loop-guard.py"' and lg['timeout'] == 5
              and 'onFailure' not in lg, f'lean: loop-guard command {lg}')
        for ev in ('PostToolUse', 'PostToolUseFailure', 'Stop', 'SubagentStop'):
            h = hooks.get(ev, [{}])[0].get('hooks', [{}])[0]
            check(h.get('command') == f'"{py}" -I "{run_dir}/repeat-guard.py"' and h.get('timeout') == 10,
                  f'lean: repeat-guard on {ev}')
        check('SessionStart' not in hooks, 'lean: no SessionStart hook')
        check(os.path.isabs(py) and py.startswith('/'), 'lean: hook interpreter by absolute path')
        check(f'TEST OVERRIDE: {OVERRIDE}=' in r.stderr, 'lean: the test override is announced on stderr')
        check(OVERRIDE not in env, 'lean: the test override does not reach claude')
        # after exit: the settings and MCP files gone, the hook copies kept
        check(not os.path.exists(settings) and not os.path.exists(mcp), 'lean: per-run JSON removed at exit')
        check(all(os.path.exists(os.path.join(run_dir, h)) for h in ('lis-guard.py', 'loop-guard.py')),
              'lean: hook copies kept after exit')
        check(stat.S_IMODE(os.lstat(run_dir).st_mode) == 0o700, 'lean: run dir still 700')
        # lis-guard treats the copies and the per-run settings as guard files
        sys.dont_write_bytecode = True      # no __pycache__ in scripts/hooks
        spec = importlib.util.spec_from_file_location('lis_guard_t', os.path.join(REPO, 'scripts', 'hooks', 'lis-guard.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for cmd in (f'cp /dev/null {run_dir}/lis-guard.py', f'echo x > {settings}', f'chmod u+w {run_dir}/loop-guard.py'):
            d = mod.decide(cmd, None)
            check(bool(d) and d[0] == 'deny', f'lean: lis-guard denies {cmd!r}: {d}')
    finally:
        e.close()


def full_run():
    e = Env()
    try:
        e.up()
        r = e.run('--full', 'fix it')
        check(r.returncode == 0, f'--full: rc {r.returncode} {r.stderr[-300:]!r}')
        c = [c for c in e.calls() if c['who'] == 'claude']
        if not check(len(c) == 1, '--full: claude ran'):
            return
        a = c[0]['argv']
        check(a[0] == '--settings' and a[2:] == ['--mcp-config', a[3], '--strict-mcp-config', 'fix it'],
              f'--full: arguments {a}')
        check('--tools' not in a and '--append-system-prompt' not in a and '--setting-sources' not in a,
              '--full: no tool list, no policy, user scope on')
        check(c[0]['mcp'] == {'mcpServers': {}}, '--full: MCP still off')
        check('user scope: on (--full)' in r.stderr, '--full: banner names user scope on')
        no_key(e, r, '--full')
    finally:
        e.close()


def scope_and_guards():
    e = Env()
    try:
        e.up()
        r = e.run('--user-scope', '--no-loop-guard', '-p', 'x')
        c = [c for c in e.calls() if c['who'] == 'claude'][0]
        a = c['argv']
        check('--setting-sources' not in a and '--tools' in a, '--user-scope: no --setting-sources, lean kept')
        h = c['settings'].get('hooks', {})
        check(list(h) == ['PreToolUse'] and len(h['PreToolUse']) == 1
              and h['PreToolUse'][0]['matcher'] == 'Bash', f'--no-loop-guard: lis-guard only: {list(h)}')
        check(set(c['run_files']) == {'lis-guard.py', os.path.basename(a[1]),
                                      os.path.basename(a[a.index('--mcp-config') + 1])},
              f'--no-loop-guard: copies {sorted(c["run_files"])}')
        check(r.returncode == 0, '--no-loop-guard: rc 0')
    finally:
        e.close()
    e = Env()
    try:
        e.up()
        r = e.run('--no-guard', '-p', 'x')
        c = [c for c in e.calls() if c['who'] == 'claude'][0]
        check('hooks' not in c['settings'], '--no-guard: no hooks')
        check(not any(n.endswith('.py') for n in c['run_files']), '--no-guard: no hook copies')
        check('Edit(//**/lis-hooks-*/**)' in c['settings']['permissions']['deny'],
              '--no-guard: the run dir deny stays')
    finally:
        e.close()


def start_server():
    e = Env()
    try:
        r = e.run('--start-server', '-p', 'x')
        calls = e.calls()
        check(r.returncode == 0, f'--start-server: rc {r.returncode} {r.stderr[-300:]!r}')
        check([kind(c) for c in calls] == ['curl /health', 'serve', 'curl /health', 'curl /slots', 'claude'],
              f'--start-server: order {[kind(c) for c in calls]}')
        check(calls[1]['argv'] == ['--detach'], f'--start-server: serve.sh --detach: {calls[1]["argv"]}')
        sl = [c for c in calls if kind(c) == 'curl /slots'][0]
        check(e.key1 in sl['stdin'] and e.key0 not in sl['stdin'],
              '--start-server: /slots carries the key serve.sh wrote (read after the start)')
        cl = [c for c in calls if c['who'] == 'claude'][0]
        # key order, by behaviour: serve.sh wrote key1 over key0 at this start; the claude
        # launched afterwards must hold key1 (a key read before the start would give key0)
        check(cl['env'].get('ANTHROPIC_AUTH_TOKEN') == e.key1 != e.key0,
              '--start-server: claude gets the NEW key serve.sh wrote, not the old one')
        check(e.key0 not in json.dumps(cl) and e.key1 not in json.dumps(cl.get('settings')),
              '--start-server: the old key reaches claude nowhere; no key in the settings')
        check('-q' == sl['argv'][0] and '-K' in sl['argv'] and sl['argv'][sl['argv'].index('-K') + 1] == '-'
              and '--noproxy' in sl['argv'], f'--start-server: curl -q ... -K - --noproxy: {sl["argv"]}')
        no_key(e, r, '--start-server')
        # the scrub runs before serve.sh: no inherited variable of the poison set reaches it,
        # the server's own test overrides and this script's override included
        se = e.serve_env()
        if check(se is not None, '--start-server: serve.sh recorded its env'):
            leaked = sorted(k for k in list(POISON) + [OVERRIDE] if k in se)
            check(not leaked, f'--start-server: scrubbed before serve.sh, leaked {leaked}')
            check('LIS_TEST_LLAMA_SERVER' not in se and 'LIS_TEST_LLAMA_EXE' not in se,
                  '--start-server: LIS_TEST_LLAMA_SERVER / _EXE do not reach serve.sh')
        with open(os.path.join(e.t, 'state', 'serve-funcs')) as f:
            funcs = f.read()
        check(funcs == '', f'--start-server: no exported function reaches serve.sh: {funcs!r}')
        check(not any(k.startswith('BASH_FUNC_') for k in cl['env']),
              f'--start-server: no BASH_FUNC_ name reaches claude: {sorted(k for k in cl["env"] if k.startswith("BASH_FUNC_"))}')
        # the launcher's own say() ran, not the caller's exported say (BASH_FUNC_say%%)
        check('poison-say' not in r.stderr and 'poison-say' not in r.stdout
              and 'claude-local: user scope: off (S-O3)' in r.stderr,
              f'--start-server: the launcher\'s say() is its own: {r.stderr[-200:]!r}')
    finally:
        e.close()
    # an inherited SHELLOPTS=xtrace (and verbose) prints nothing of the key, and reaches no child
    e = Env()
    try:
        e.env['SHELLOPTS'] = 'xtrace:verbose'
        r = e.run('--start-server', '-p', 'x')
        cl = [c for c in e.calls() if c['who'] == 'claude']
        check(r.returncode == 0 and len(cl) == 1, f'SHELLOPTS=xtrace: launches: {r.returncode} {r.stderr[-200:]!r}')
        no_key(e, r, 'SHELLOPTS=xtrace')
        check(cl and cl[0]['env'].get('ANTHROPIC_AUTH_TOKEN') == e.key1, 'SHELLOPTS=xtrace: claude still gets the key')
        se = e.serve_env() or set()
        with open(os.path.join(e.t, 'state', 'serve-flags')) as f:
            flags = f.read().strip()
        check('SHELLOPTS' not in se and 'x' not in flags and 'v' not in flags and 'u' not in flags,
              f'SHELLOPTS=xtrace: serve.sh inherits no shell options (flags {flags!r})')
        check(not cl or 'SHELLOPTS' not in cl[0]['env'], 'SHELLOPTS=xtrace: claude gets no SHELLOPTS')
    finally:
        e.close()
    # the same with a caller function named set (no posix mode): it replaces the first
    # set +xv, so xtrace stays on through the function removal; the second set +xv, after
    # the removal, must switch it off before anything reads the key
    e = Env()
    try:
        e.env.pop('POSIXLY_CORRECT', None)
        e.env['SHELLOPTS'] = 'xtrace'
        e.env['BASH_FUNC_set%%'] = '() { /bin/echo stand-in-set; }'
        r = e.run('--start-server', '-p', 'x')
        label = 'SHELLOPTS=xtrace and a caller set function'
        cl = [c for c in e.calls() if c['who'] == 'claude']
        check(r.returncode == 0 and len(cl) == 1, f'{label}: launches: {r.returncode} {r.stderr[-200:]!r}')
        no_key(e, r, label)
        trace = [ln for ln in r.stderr.splitlines() if ln.startswith('+')]
        check(len(trace) <= XTRACE_REMOVAL_LINES,
              f'{label}: at most {XTRACE_REMOVAL_LINES} trace lines (the removal only): {len(trace)}')
    finally:
        e.close()
    e = Env()
    try:
        e.up()
        e.run('--start-server', '-p', 'x')
        check([kind(c) for c in e.calls()] == ['curl /health', 'curl /health', 'curl /slots', 'claude'],
              f'--start-server, server up: no serve.sh: {[kind(c) for c in e.calls()]}')
    finally:
        e.close()
    e = Env()
    try:
        e.write('state/serve-fails', '')
        r = e.run('--start-server', '-p', 'x')
        check(r.returncode != 0 and [kind(c) for c in e.calls()] == ['curl /health', 'serve']
              and not e.run_dirs(), f'--start-server, serve.sh fails: refused, no claude: '
                                    f'{[kind(c) for c in e.calls()]}')
    finally:
        e.close()


def preflight():
    e = Env()
    try:
        r = e.run('-p', 'x')
        check(r.returncode != 0 and [kind(c) for c in e.calls()] == ['curl /health'] and not e.run_dirs()
              and '--start-server' in r.stderr, f'server down: refused after /health alone: {r.stderr[-200:]!r}')
    finally:
        e.close()
    e = Env()
    try:
        e.up(busy=True)
        r = e.run('--strict-slot', '-p', 'x')
        check(r.returncode != 0 and [kind(c) for c in e.calls()] == ['curl /health', 'curl /slots']
              and not e.run_dirs() and 'slot 0 is processing' in r.stderr, '--strict-slot: a busy slot refuses')
        no_key(e, r, '--strict-slot')
    finally:
        e.close()
    e = Env()
    try:
        e.up(busy=True)
        r = e.run('-p', 'x')
        check(r.returncode == 0 and 'warning: server slot 0 is processing' in r.stderr
              and e.calls()[-1]['who'] == 'claude', 'busy slot: warns and launches')
    finally:
        e.close()
    e = Env()
    try:
        e.up()
        e.write('state/server-key', secrets.token_hex(32) + '\n')    # the server holds another key
        r = e.run('-p', 'x')
        check(r.returncode != 0 and 'refused /slots' in r.stderr and not e.run_dirs()
              and all(c['who'] == 'curl' for c in e.calls()), '/slots 401: refused, no claude')
        no_key(e, r, '/slots 401')
    finally:
        e.close()
    e = Env()
    try:
        e.up()
        e.write('repo/logs/api-key', 'bad key"\n')
        r = e.run('-p', 'x')
        check(r.returncode != 0 and 'bad key' not in r.stderr and not e.calls(),
              'a malformed key file refuses without printing it')
    finally:
        e.close()


def print_args():
    e = Env()
    try:
        e.up()
        r = e.run('--print-args', '-p', 'x')
        check(r.returncode == 0 and not e.calls() and not e.run_dirs(),
              f'--print-args: rc 0, no stub call, no run dir: {r.returncode} {[kind(c) for c in e.calls()]}')
        check('claude arguments (JSON array, in order):' in r.stdout and '"--setting-sources"' in r.stdout
              and 'settings JSON' in r.stdout and '"PreToolUse"' in r.stdout, '--print-args: prints args and settings')
        no_key(e, r, '--print-args')
    finally:
        e.close()


REFUSALS = [
    (['--settings', 'x.json'], None),
    (['--settings=x.json'], None),
    (['--SETTING-SOURCES', 'user'], None),
    (['--plugin-dir', 'p'], None),
    (['--dangerously-skip-permissions'], None),
    (['--allow-dangerously-skip-permissions'], None),
    (['--permission-mode', 'bypassPermissions'], None),
    (['--permission-mode=bypassPermissions'], None),
    (['--full', '--mcp-config', 'm.json'], None),
    (['--full', '--allowedTools', 'Bash,WebFetch'], None),
    (['--full', '--allowed-tools=mcp__x__y'], None),
    (['--full', '--full'], None),
    (['--context-tokens', '12'], None),
    (['--context-tokens', 'abc'], None),
    (['--context-tokens'], None),
    ([], 'PORT=8080@evil.example\nCTX=131072\nCLIENT_CTX=114688\n'),
    ([], 'PORT=99999\nCTX=131072\nCLIENT_CTX=114688\n'),
    ([], 'PORT=\nCTX=131072\nCLIENT_CTX=114688\n'),
    ([], 'PORT=8080/../x\nCTX=131072\nCLIENT_CTX=114688\n'),
    ([], 'PORT= 8080\nCTX=131072\nCLIENT_CTX=114688\n'),
    ([], 'PORT=8080\nCTX=131072\nCLIENT_CTX=lots\n'),
]


def refusals():
    """Each refusal, with --start-server and the server down, starts nothing at all."""
    for args, conf in REFUSALS:
        e = Env(conf) if conf else Env()
        try:
            r = e.run('--start-server', *args, '-p', 'x')
            label = f'refusal {args or conf.splitlines()[0]}'
            check(r.returncode != 0 and r.stderr.startswith('claude-local: ') and not r.stdout,
                  f'{label}: rc {r.returncode} stderr {r.stderr[-200:]!r}')
            check(not e.calls() and not e.run_dirs(), f'{label}: started {[kind(c) for c in e.calls()]}')
            if conf and conf.startswith('PORT=') and 'CLIENT_CTX=lots' not in conf:
                check('loopback' in r.stderr or '65535' in r.stderr, f'{label}: names the loopback rule')
        finally:
            e.close()
    # --full with a web-free --allowedTools is not refused (the rule names web tools only)
    e = Env()
    try:
        e.up()
        r = e.run('--full', '--allowedTools', 'Bash', '-p', 'x')
        check(r.returncode == 0 and e.calls()[-1]['who'] == 'claude', '--full --allowedTools Bash launches')
    finally:
        e.close()


def refused_quietly(e, r, label, names=(), hidden=()):
    """A refusal: rc != 0, one claude-local: message naming each of names, nothing started."""
    check(r.returncode != 0 and r.stderr.startswith('claude-local: ') and not r.stdout,
          f'{label}: rc {r.returncode} stdout {r.stdout[-100:]!r} stderr {r.stderr[-200:]!r}')
    for n in names:
        check(n in r.stderr, f'{label}: the refusal names {n!r}: {r.stderr[-200:]!r}')
    for h in hidden:
        check(h not in r.stderr, f'{label}: the refusal does not print {h!r}')
    check(not e.calls() and not e.run_dirs(), f'{label}: started {[kind(c) for c in e.calls()]}')


# A: each refused by name, with --start-server and the server down (so nothing may start).
ALLOW_REFUSALS = [
    (['--add-dir', 'd'], ['--add-dir']),
    (['--mcp-config', 'm.json'], ['--mcp-config']),
    (['--mcp-config=m.json'], ['--mcp-config']),
    (['--tools', 'Bash'], ['--tools']),
    (['--debug'], ['--debug']),
    (['-d'], ["'-d'"]),
    (['--debug-file', 'f.log'], ['--debug-file']),
    (['--model', 'opus'], ['--model']),
    (['--model=opus'], ['--model']),
    (['--system-prompt', 's'], ['--system-prompt']),
    (['--bare'], ['--bare']),
    (['--help'], ['--help']),
    (['--fork-session'], ['--fork-session']),
    (['-pc'], ["'-pc'"]),
    (['--print=yes'], ['--print']),
    (['--verbose=1'], ['--verbose']),
    (['-'], ["'-'"]),
    # a first operand that is a subcommand name, before or after --, any case
    (['mcp'], ["'mcp'", 'subcommand']),
    (['doctor'], ["'doctor'"]),
    (['-p', 'Update'], ["'Update'"]),
    (['--verbose', 'help'], ["'help'"]),
    (['--', 'plugins'], ["'plugins'"]),
    (['--allowedTools', 'Bash', '--', 'auth'], ["'auth'"]),
    # option-shaped words after a bare --
    (['-p', '--', '--model', 'opus'], ["'--model' after '--'"]),
    (['--', 'hi', '-x'], ["'-x' after '--'"]),
    (['--', '--'], ["'--' after '--'"]),
    # --permission-mode outside manual/acceptEdits/plan, and the format choices
    (['--permission-mode', 'auto'], ["'auto'"]),
    (['--permission-mode=dontAsk'], ["'dontAsk'"]),
    (['--permission-mode', 'default'], ["'default'"]),
    (['--permission-mode'], ['--permission-mode needs a value']),
    (['--output-format', 'xml'], ["'xml'"]),
    (['--input-format=json'], ['--input-format', "'json'"]),
    # every word of a multi-value list is checked, the second one too
    (['--allowedTools', 'Bash', 'fix the bug'], ["'fix the bug'"]),
    (['--disallowed-tools', 'Write', 'two words'], ["'two words'"]),
    (['--allowedTools'], ['--allowedTools needs at least one tool rule']),
    (['--allowedTools', '-p'], ['--allowedTools needs at least one tool rule']),
    (['--full', '--allowedTools', 'Bash', 'WebFetch'], ["'WebFetch'", '--full']),
    (['--full', '--allowed-tools', 'Read', 'mcp__s__t'], ["'mcp__s__t'"]),
    # --resume's optional value
    (['--resume', 'a b'], ["'a b'"]),
    (['-r', 'x/y'], ["'x/y'"]),
    (['--resume=../s'], ["'../s'"]),
    # --name's value, validated like a resume id
    (['-n', 'a b'], ["-n value 'a b'"]),
    (['--name'], ['--name needs a value']),
    (['--name=../x'], ["--name value '../x'"]),
    (['-n', '-p', 'x'], ["-n value '-p'"]),
    (['--no-session-persistence=1'], ['--no-session-persistence']),
]


# The turn cap: refused values, each with --start-server and the server down.
MAX_TURNS_REFUSALS = [
    (['--max-turns', '0', '-p', 'x'], ["--max-turns value '0'"]),
    (['--max-turns', '10001', '-p', 'x'], ["--max-turns value '10001'"]),
    (['--max-turns', '-1', '-p', 'x'], ["--max-turns value '-1'"]),
    (['--max-turns', '+5', '-p', 'x'], ["--max-turns value '+5'"]),
    (['--max-turns', '1.5', '-p', 'x'], ["--max-turns value '1.5'"]),
    (['--max-turns', '', '-p', 'x'], ["--max-turns value ''"]),
    (['--max-turns=', '-p', 'x'], ["--max-turns value ''"]),
    (['--max-turns', '05', '-p', 'x'], ["--max-turns value '05'"]),
    (['--max-turns', ' 5', '-p', 'x'], ["--max-turns value ' 5'"]),
    (['--max-turns', '99999999999', '-p', 'x'], ["--max-turns value '99999999999'"]),
    (['--max-turns=abc', '-p', 'x'], ["--max-turns value 'abc'"]),
    (['-p', 'x', '--max-turns'], ['--max-turns needs a value']),
    (['--max-turns', '3', '-p', 'x', '--max-turns', '4'], ['--max-turns was given more than once']),
    (['--max-turns=3', '--max-turns', '3', '-p', 'x'], ['--max-turns was given more than once']),
    (['-p', 'x', '--', '--max-turns', '5'], ["'--max-turns' after '--'"]),
    (['--MAX-TURNS', '5', '-p', 'x'], ['--MAX-TURNS']),
]
TURNS_LINE = 'claude-local: print mode without --max-turns: added --max-turns 60'


def max_turns():
    for args, names in MAX_TURNS_REFUSALS:
        e = Env()
        try:
            r = e.run('--start-server', *args)
            refused_quietly(e, r, f'max-turns refuses {args}', names)
        finally:
            e.close()

    def launched(args):
        e = Env()
        try:
            e.up()
            r = e.run(*args)
            cl = [c for c in e.calls() if c['who'] == 'claude']
            check(r.returncode == 0 and len(cl) == 1, f'max-turns {args}: launches: rc {r.returncode} {r.stderr[-200:]!r}')
            return r, (cl[0]['argv'] if cl else [])
        finally:
            e.close()

    # accepted values, passed unchanged at the caller's position, no default, no status line
    for args in (['-p', 'x', '--max-turns', '1'], ['--max-turns', '60', '-p', 'x'],
                 ['-p', '--max-turns', '10000', 'x'], ['--max-turns=9999', '--print', 'x'],
                 ['--max-turns', '5', 'x']):
        r, a = launched(args)
        check(a[-len(args):] == args, f'max-turns {args}: passed in place: {a[-len(args):]}')
        check(sum(1 for w in a if w == '--max-turns' or w.startswith('--max-turns=')) == 1,
              f'max-turns {args}: one --max-turns only (an explicit value replaces the default): {a}')
        check(TURNS_LINE not in r.stderr, f'max-turns {args}: no default status line')
    # print mode without it: the default, ahead of the prompt and of a `--`, with the line
    for args in (['-p', 'hello', '--', 'more'], ['--print', 'x'], ['--full', '-p', 'x'],
                 ['--output-format', 'json', '-p', 'x'], ['--allowedTools', 'Bash', 'Read', '-p', '--', 'y']):
        r, a = launched(args)
        want = [w for w in args if w != '--full']
        if not check('--max-turns' in a, f'max-turns default {args}: added: {a}'):
            continue
        k = a.index('--max-turns')
        check(a[k + 1] == '60' and a.count('--max-turns') == 1, f'max-turns default {args}: --max-turns 60 once')
        check(a[k + 2:] == want, f'max-turns default {args}: right before the caller arguments: {a[k:]}')
        check(k < a.index('--print' if '--print' in a else '-p'), f'max-turns default {args}: before the prompt')
        if '--' in a:
            check(k < a.index('--') and '--max-turns' not in a[a.index('--'):],
                  f'max-turns default {args}: before the --, not after it')
        check(TURNS_LINE in r.stderr and '(caps turns, not the time one turn takes)' in r.stderr,
              f'max-turns default {args}: status line: {r.stderr[-300:]!r}')
    # interactive: nothing added, no line
    for args in (['fix it'], ['--verbose', '--', 'x'], ['--full', 'x'], []):
        r, a = launched(args)
        check(not any(w.startswith('--max-turns') for w in a), f'max-turns interactive {args}: none added: {a}')
        check(TURNS_LINE not in r.stderr, f'max-turns interactive {args}: no status line')
    # --print-args shows it (no server needed)
    for args, want, line in ((['-p', 'x'], '60', True), (['-p', 'x', '--max-turns', '5'], '5', False),
                             (['x'], None, False)):
        e = Env()
        try:
            r = e.run('--print-args', *args)
            arr = json.loads(r.stdout.split('claude arguments (JSON array, in order):\n', 1)[1]
                             .split('settings JSON', 1)[0])
            got = arr[arr.index('--max-turns') + 1] if '--max-turns' in arr else None
            check(r.returncode == 0 and got == want and arr.count('--max-turns') == (0 if want is None else 1),
                  f'--print-args {args}: --max-turns {got!r}, want {want!r}')
            check((TURNS_LINE in r.stderr) == line, f'--print-args {args}: status line {line}')
            check(not e.calls() and not e.run_dirs(), f'--print-args {args}: nothing started')
        finally:
            e.close()


def allow_list():
    for args in ALLOW_REFUSALS:
        args, names = args
        e = Env()
        try:
            r = e.run('--start-server', *args)
            refused_quietly(e, r, f'allow-list refuses {args}', names)
        finally:
            e.close()
    # every listed option passes, in order and unchanged
    ok = ['-p', '--output-format', 'stream-json', '--input-format=text', '--include-partial-messages',
          '--verbose', '--permission-mode', 'acceptEdits', '--allowedTools', 'Bash(git log:*)', 'Read,Grep',
          '--disallowed-tools=Write', '-r', 'abc-123', '-c', 'summarise this', '--', 'mcp and more']
    for args in (ok, ['--resume', '-p', 'explain', 'mcp'], ['--permission-mode=plan', '--print', 'x'],
                 ['--disallowedTools', 'WebFetch', 'mcp__s__t', '--continue'],
                 ['--no-session-persistence', '-n', 'my-run_1', '-p', 'x'], ['--name=abc.def', '-p', 'x'],
                 ['--full', '--allowed-tools', 'Bash', 'Read', '-p', 'x']):
        e = Env()
        try:
            e.up()
            r = e.run(*args)
            cl = [c for c in e.calls() if c['who'] == 'claude']
            want = [a for a in args if a != '--full']      # a wrapper option, taken out
            check(r.returncode == 0 and len(cl) == 1 and cl[0]['argv'][-len(want):] == want,
                  f'allow-list passes {args}: rc {r.returncode} {r.stderr[-200:]!r}')
        finally:
            e.close()


# C(b): project settings of the working directory that refuse the launch.
PROJECT_REFUSALS = [
    ('settings.json', '{"apiKeyHelper": "helper.sh"}', ['key apiKeyHelper'], ['helper.sh']),
    ('settings.local.json', '{"disableAllHooks": false}', ['key disableAllHooks'], []),
    ('settings.json', '{"hooks": {}}', ['key hooks'], []),
    ('settings.json', '{"permissions": {"defaultMode": "bypassPermissions"}}', ['permissions.defaultMode'], []),
    ('settings.local.json', '{"permissions": {"defaultMode": "auto"}}', ['permissions.defaultMode'], []),
    ('settings.json', '{"permissions": {"defaultMode": 1}}', ['permissions.defaultMode'], []),
    ('settings.json', '{"env": {"ANTHROPIC_BASE_URL": "http://elsewhere.example.invalid"}}',
     ['env.ANTHROPIC_BASE_URL'], ['elsewhere.example.invalid']),
    ('settings.json', '{"env": {"anthropic_auth_token": "t0k3n-value"}}', ['env.anthropic_auth_token'], ['t0k3n-value']),
    ('settings.json', '{"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}}', ['env.CLAUDE_CODE_USE_BEDROCK'], []),
    ('settings.local.json', '{"env": {"HTTPS_PROXY": "http://proxy.example.invalid:3128"}}',
     ['env.HTTPS_PROXY'], ['proxy.example.invalid']),
    ('settings.json', '{"env": {"no_proxy": "*"}}', ['env.no_proxy'], []),
    ('settings.json', '{"env": ["x"]}', ['key env is not an object'], []),
    # the session shell and what the client runs around or before a shell command
    ('settings.json', '{"env": {"CLAUDE_CODE_SHELL": "/bin/zsh"}}', ['env.CLAUDE_CODE_SHELL'], ['/bin/zsh']),
    ('settings.local.json', '{"env": {"CLAUDE_CODE_SHELL": "/bin/zsh"}}', ['env.CLAUDE_CODE_SHELL'], ['/bin/zsh']),
    ('settings.json', '{"env": {"SHELL": "/bin/zsh"}}', ['env.SHELL'], ['/bin/zsh']),
    ('settings.local.json', '{"env": {"SHELL": "/bin/zsh"}}', ['env.SHELL'], ['/bin/zsh']),
    ('settings.json', '{"env": {"shell": "/bin/zsh"}}', ['env.shell'], ['/bin/zsh']),
    ('settings.json', '{"env": {"CLAUDE_CODE_SHELL_PREFIX": "/x/wrap-cmd"}}',
     ['env.CLAUDE_CODE_SHELL_PREFIX'], ['/x/wrap-cmd']),
    ('settings.local.json', '{"env": {"CLAUDE_CODE_SHELL_PREFIX": "/x/wrap-cmd"}}',
     ['env.CLAUDE_CODE_SHELL_PREFIX'], ['/x/wrap-cmd']),
    ('settings.json', '{"env": {"CLAUDE_ENV_FILE": "/x/env-script.sh"}}', ['env.CLAUDE_ENV_FILE'], ['/x/env-script.sh']),
    ('settings.local.json', '{"env": {"CLAUDE_ENV_FILE": "/x/env-script.sh"}}',
     ['env.CLAUDE_ENV_FILE'], ['/x/env-script.sh']),
    ('settings.json', '{"env": {"FOO": "1", "SHELL": "/bin/bash", "CLAUDE_ENV_FILE": "/x/e"}}',
     ['env.SHELL', 'env.CLAUDE_ENV_FILE'], []),
]
# what bash runs or reads at start-up or around a command: each name in both files
BASH_STARTUP_ENV = ['BASH_ENV', 'ENV', 'HOME', 'POSIXLY_CORRECT', 'INPUTRC', 'SHELLOPTS', 'BASHOPTS',
                    'PS1', 'PS2', 'PS4', 'PROMPT_COMMAND']
PROJECT_REFUSALS += [(f, json.dumps({'env': {n: '/x/planted-value'}}), [f'env.{n}'], ['/x/planted-value'])
                     for n in BASH_STARTUP_ENV for f in ('settings.json', 'settings.local.json')]
PROJECT_REFUSALS += [
    ('settings.json', '{"env": {"Bash_Env": "/x/planted-value"}}', ['env.Bash_Env'], ['/x/planted-value']),
    ('settings.json', '{"env": {"BASH_FUNC_x%%": "() { echo planted; }"}}', ['env.BASH_FUNC_x%%'], ['planted']),
    ('settings.local.json', '{"env": {"BASH_FUNC_ls%%": "() { echo planted; }"}}', ['env.BASH_FUNC_ls%%'], ['planted']),
    ('settings.json', '{"env": {"bash_func_ls%%": "() { echo planted; }"}}', ['env.bash_func_ls%%'], ['planted']),
    ('settings.json', '{"env": {"FOO": "1", "PS4": "+$(x) ", "BASH_FUNC_cd%%": "() { :; }"}}',
     ['env.PS4', 'env.BASH_FUNC_cd%%'], []),
]
# the same names with surrounding whitespace (whether the client trims a key is UNVERIFIED)
PROJECT_REFUSALS += [
    (f, json.dumps({'env': {key: '/x/planted-value'}}), [f'env.{json.dumps(key)[1:-1]}'], ['/x/planted-value'])
    for key, f in ((' BASH_ENV ', 'settings.json'), ('BASH_ENV\t', 'settings.local.json'),
                   (' CLAUDE_CODE_SHELL', 'settings.json'), ('SHELL ', 'settings.local.json'),
                   (' CLAUDE_CODE_SHELL_PREFIX ', 'settings.json'), ('\nCLAUDE_ENV_FILE', 'settings.local.json'),
                   (' ps4', 'settings.json'), ('PROMPT_COMMAND ', 'settings.local.json'),
                   (' BASH_FUNC_ls%% ', 'settings.json'), (' HOME', 'settings.local.json'),
                   (' ANTHROPIC_BASE_URL', 'settings.json'), ('HTTPS_PROXY ', 'settings.local.json'))
]
PROJECT_REFUSALS += [
    ('settings.json', '{ not json', ['.claude/settings.json: cannot be read as JSON'], []),
    ('settings.local.json', '[]', ['.claude/settings.local.json: the top level is not an object'], []),
]


def project_settings():
    for name, text, names, hidden in PROJECT_REFUSALS:
        e = Env()
        try:
            os.makedirs(os.path.join(e.t, 'work', '.claude'))
            e.write(f'work/.claude/{name}', text)
            r = e.run('--start-server', '-p', 'x')
            refused_quietly(e, r, f'project {name} {text}', [f'.claude/{name}'] + names, hidden)
        finally:
            e.close()
    # refused before --print-args prints anything; a dangling link refuses too
    e = Env()
    try:
        os.makedirs(os.path.join(e.t, 'work', '.claude'))
        e.write('work/.claude/settings.local.json', '{"hooks": {"Stop": []}}')
        r = e.run('--print-args', '-p', 'x')
        refused_quietly(e, r, 'project hooks under --print-args', ['key hooks'])
    finally:
        e.close()
    e = Env()
    try:
        os.makedirs(os.path.join(e.t, 'work', '.claude'))
        os.symlink(os.path.join(e.t, 'nowhere.json'), os.path.join(e.t, 'work', '.claude', 'settings.json'))
        r = e.run('--start-server', '-p', 'x')
        refused_quietly(e, r, 'project settings dangling link', ['cannot be read as JSON'])
    finally:
        e.close()
    # harmless project settings launch
    e = Env()
    try:
        e.up()
        os.makedirs(os.path.join(e.t, 'work', '.claude'))
        e.write('work/.claude/settings.json',
                '{"permissions": {"defaultMode": "plan", "allow": ["Read"]}, "env": {"FOO": "1"}}')
        e.write('work/.claude/settings.local.json', '{"permissions": {"defaultMode": "default"}}')
        r = e.run('-p', 'x')
        check(r.returncode == 0 and e.calls()[-1]['who'] == 'claude',
              f'project settings without those keys launch: {r.stderr[-200:]!r}')
    finally:
        e.close()


def session_shell():
    """The session shell: /bin/bash in claude's env and the settings, whatever the caller holds."""
    # caller values: (CLAUDE_CODE_SHELL, SHELL), None = unset; the prefix and env file always set
    callers = [(None, None), ('/bin/zsh', '/bin/zsh'), ('/nonexistent/sh', '/nonexistent/sh'), ('', ''),
               (None, '/bin/zsh'), ('/bin/zsh', None), ('/bin/sh', '/usr/local/bin/fish')]
    for ccs, sh in callers:
        for mode in ((), ('--full',), ('--user-scope',), ('--no-guard',), ('--no-loop-guard',)):
            e = Env()
            try:
                e.up()
                for k, v in (('CLAUDE_CODE_SHELL', ccs), ('SHELL', sh)):
                    e.env.pop(k, None)
                    if v is not None:
                        e.env[k] = v
                e.env['CLAUDE_CODE_SHELL_PREFIX'] = '/nonexistent/caller-prefix'
                e.env['CLAUDE_ENV_FILE'] = '/nonexistent/caller-env-file'
                r = e.run(*mode, '-p', 'x')
                label = f'session shell, caller CLAUDE_CODE_SHELL={ccs!r} SHELL={sh!r} {list(mode)}'
                cl = [c for c in e.calls() if c['who'] == 'claude']
                if not check(r.returncode == 0 and len(cl) == 1, f'{label}: launches: rc {r.returncode} {r.stderr[-200:]!r}'):
                    continue
                env = cl[0]['env']
                check(env.get('CLAUDE_CODE_SHELL') == SESSION_SHELL,
                      f'{label}: claude gets CLAUDE_CODE_SHELL={env.get("CLAUDE_CODE_SHELL")!r}')
                check(env.get('SHELL') == SESSION_SHELL, f'{label}: claude gets SHELL={env.get("SHELL")!r}')
                check('CLAUDE_CODE_SHELL_PREFIX' not in env, f'{label}: the caller CLAUDE_CODE_SHELL_PREFIX is cleared')
                check('CLAUDE_ENV_FILE' not in env, f'{label}: the caller CLAUDE_ENV_FILE is cleared')
                left = sorted(k for k in env if k.startswith('BASH_FUNC_')
                              or k in ('PS4', 'PROMPT_COMMAND', 'POSIXLY_CORRECT', 'INPUTRC', 'BASH_ENV', 'ENV',
                                       'SHELLOPTS', 'BASHOPTS'))
                check(not left, f'{label}: no bash start-up name or exported function reaches claude: {left}')
                s_env = cl[0]['settings'].get('env', {})
                check(s_env.get('CLAUDE_CODE_SHELL') == SESSION_SHELL and s_env.get('SHELL') == SESSION_SHELL,
                      f'{label}: the --settings file pins env.CLAUDE_CODE_SHELL and env.SHELL: '
                      f'{s_env.get("CLAUDE_CODE_SHELL")!r} {s_env.get("SHELL")!r}')
                check('CLAUDE_CODE_SHELL_PREFIX' not in s_env and 'CLAUDE_ENV_FILE' not in s_env,
                      f'{label}: no prefix or env file in the --settings file')
                check(SHELL_LINE in r.stderr.splitlines(), f'{label}: status line names the session shell')
            finally:
                e.close()
    # --print-args: the settings JSON carries the pin, and the status line is printed
    e = Env()
    try:
        e.env['SHELL'] = '/bin/zsh'
        r = e.run('--print-args', '-p', 'x')
        s = json.loads(r.stdout.split('settings JSON (the --settings file):\n', 1)[1]
                       .split('MCP config JSON', 1)[0]) if 'settings JSON (the --settings file):\n' in r.stdout else {}
        check(r.returncode == 0 and s.get('env', {}).get('CLAUDE_CODE_SHELL') == SESSION_SHELL
              and s['env'].get('SHELL') == SESSION_SHELL, f'--print-args: settings pin the session shell: {r.stderr[-200:]!r}')
        check(SHELL_LINE in r.stderr.splitlines(), '--print-args: status line names the session shell')
    finally:
        e.close()
    # the fixed path, checked like the other tools: no PATH lookup, no Homebrew bash
    with open(SCRIPT, encoding='utf-8') as f:
        src = f.read()
    check(src.count('\nSESSION_SHELL=/bin/bash\n') == 1 and "check_tool 'bash (the session shell)' \"$SESSION_SHELL\"" in src
          and 'command -v bash' not in src and '/opt/homebrew/bin/bash' not in src,
          'session shell: the fixed path /bin/bash, through check_tool')
    check('export CLAUDE_CODE_SHELL="$SESSION_SHELL"' in src and 'export SHELL="$SESSION_SHELL"' in src,
          'session shell: exported in the launch block')
    # the check itself, on the test copy with the fixed path swapped (nothing starts)
    def swapped(setup, label, names):
        e = Env()
        try:
            p = os.path.join(e.repo, 'scripts', 'claude-local.sh')
            with open(p, encoding='utf-8') as f:
                text = f.read()
            if not check(text.count('\nSESSION_SHELL=/bin/bash\n') == 1, f'{label}: SESSION_SHELL line in the copy'):
                return
            target = setup(e)
            with open(p, 'w', encoding='utf-8') as f:
                f.write(text.replace('\nSESSION_SHELL=/bin/bash\n', f'\nSESSION_SHELL={target}\n'))
            r = e.run('--start-server', '-p', 'x')
            refused_quietly(e, r, label, names)
        finally:
            e.close()
    swapped(lambda e: os.path.join(e.t, 'nowhere', 'bash'), 'session shell missing',
            ['bash (the session shell)', 'not an executable file'])
    swapped(lambda e: e.write('bin/noexec-bash', '#!/bin/sh\n', 0o644), 'session shell not executable',
            ['bash (the session shell)', 'not an executable file'])
    swapped(lambda e: os.path.join(e.t, 'bin'), 'session shell a directory',
            ['bash (the session shell)', 'not an executable file'])
    swapped(lambda e: e.write('bin/ww-bash', '#!/bin/sh\n', 0o757), 'session shell world-writable',
            ['bash (the session shell) refused', 'writable by everyone'])
    swapped(lambda e: 'bash', 'session shell relative', ['bash (the session shell)', 'not an absolute path'])


# Exported functions whose names are no identifier: bash 3.2 imports them only outside
# posix mode, so these runs drop POSIXLY_CORRECT from the poison set (the posix-mode runs
# are every other case). Name presence only.
ODD_FUNCS = {f'BASH_FUNC_{n}%%': f'() {{ echo stand-in-{i}; }}'
             for i, n in enumerate(('-x', '--', '-', '-f', 'a-b', 'a.b', 'ls', 'say'))}


def exported_functions():
    for args, server_up in ((('-p', 'x'), True), (('--start-server', '-p', 'x'), False),
                            (('--full', '-p', 'x'), True), (('--no-guard', '-p', 'x'), True)):
        e = Env()
        try:
            e.env.pop('POSIXLY_CORRECT', None)
            e.env.update(ODD_FUNCS)
            if server_up:
                e.up()
            r = e.run(*args)
            label = f'exported functions, no posix mode, {list(args)}'
            cl = [c for c in e.calls() if c['who'] == 'claude']
            if not check(r.returncode == 0 and len(cl) == 1, f'{label}: launches: rc {r.returncode} {r.stderr[-300:]!r}'):
                continue
            left = sorted(k for k in cl[0]['env'] if k.startswith('BASH_FUNC_'))
            check(not left, f'{label}: no BASH_FUNC_ name reaches claude: {left}')
            check('stand-in-' not in r.stdout and 'stand-in-' not in r.stderr,
                  f'{label}: no caller function ran in the launcher')
            if not server_up:
                p = os.path.join(e.t, 'state', 'serve-funcs')
                if check(os.path.exists(p), f'{label}: serve.sh ran and recorded its functions'):
                    with open(p) as f:
                        funcs = f.read()
                    check(funcs == '', f'{label}: no exported function reaches serve.sh: {funcs!r}')
        finally:
            e.close()
    builtin_sweep()
    # fail closed: with the removal swapped out in the test copy, a surviving function refuses
    e = Env()
    try:
        e.env.pop('POSIXLY_CORRECT', None)
        e.env.update(ODD_FUNCS)
        p = os.path.join(e.repo, 'scripts', 'claude-local.sh')
        with open(p, encoding='utf-8') as f:
            text = f.read()
        old = 'builtin unset -f -- "${fn#declare -f* }"'
        if check(text.count(old) == 1, 'exported functions: the removal line is in the copy'):
            with open(p, 'w', encoding='utf-8') as f:
                f.write(text.replace(old, ':'))
            r = e.run('--start-server', '-p', 'x')
            refused_quietly(e, r, 'exported functions: one survives the removal',
                            ['an exported shell function survived the removal'])
    finally:
        e.close()


SWEEP_TIMEOUT = 30    # one sweep run takes about a second


def sweep_one(name):
    """One run with one caller-exported function named name (no posix mode).

    Returns (ran, r, calls, funcs): whether the function ran, the result (None when the
    launcher did not end within SWEEP_TIMEOUT), the kinds of the recorded calls and the
    BASH_FUNC_ names in claude's environment."""
    e = Env()
    try:
        e.env.pop('POSIXLY_CORRECT', None)
        for k in [k for k in e.env if k.startswith('BASH_FUNC_')]:
            del e.env[k]
        marks = os.path.join(e.t, 'state', 'function-ran')
        e.env[f'BASH_FUNC_{name}%%'] = f"() {{ /bin/echo ran >> '{marks}'; }}"
        e.up()
        try:
            r = e.run('-p', 'x', timeout=SWEEP_TIMEOUT)
        except subprocess.TimeoutExpired:
            r = None
        cl = e.calls()
        funcs = sorted(k for c in cl if c['who'] == 'claude' for k in c['env'] if k.startswith('BASH_FUNC_'))
        return os.path.exists(marks), r, [kind(c) for c in cl], funcs
    finally:
        e.close()


# The caller functions that run in the launcher, out of every builtin name: set replaces
# the first line (set +xv) before the removal; builtin replaces every `builtin X`. The
# launcher does not defend itself against the shell that starts it (README, Known limits);
# this set is pinned so that a new bare command word before the removal fails here.
SWEEP_RAN = ['builtin', 'set']


def builtin_sweep():
    """Every bash 3.2 builtin (plus ls, die, say) as a caller function, one per run.

    Every run ends; for every name but builtin the launch succeeds and no BASH_FUNC_ name
    reaches claude; the functions that ran are exactly SWEEP_RAN. For builtin only the
    end of the run is checked: it defeats the removal (the documented limit)."""
    names = subprocess.run(['/bin/bash', '-c', 'compgen -b'], env={'PATH': '/usr/bin:/bin'},
                           capture_output=True, text=True, timeout=30).stdout.split()
    check(len(names) > 50 and {':', 'set', 'builtin', 'break', 'exit', 'printf'} <= set(names),
          f'sweep: compgen -b lists the builtins: {len(names)}')
    names += ['ls', 'die', 'say']
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = dict(zip(names, pool.map(sweep_one, names)))
    hung = sorted(n for n, (_, r, _, _) in results.items() if r is None)
    check(not hung, f'sweep: every run ends within {SWEEP_TIMEOUT} s: {hung}')
    ran = sorted(n for n, (hit, _, _, _) in results.items() if hit)
    check(ran == SWEEP_RAN, f'sweep: the caller functions that ran are exactly {SWEEP_RAN}: {ran}')
    for n, (hit, r, calls, funcs) in results.items():
        if n == 'builtin' or r is None:
            continue
        check(r.returncode == 0 and calls[-1:] == ['claude'] and not funcs,
              f'sweep {n}: launches, no BASH_FUNC_ name reaches claude: rc {r.returncode} '
              f'{r.stderr[-200:]!r} {calls} {funcs}')


FIXED_STAMP = '20260101-000000'
DATE_STUB = '''#!/bin/bash
[ "$*" = '+%Y%m%d-%H%M%S' ] && {{ printf '%s\\n' '{stamp}'; exit 0; }}
exec /bin/date "$@"
'''
# Prints the next number of state/od-plan (the last one repeats). For a number listed in
# state/od-plant it first creates lis-hooks-<stamp>-<launcher pid>-<number> in TMPDIR
# (mode 755, a marker file inside), the path the launcher is about to try.
OD_STUB = '''#!{py} -I
import os, subprocess, sys
T = {t!r}
st = os.path.join(T, 'state')
seq = os.path.join(st, 'od-seq')
n = int(open(seq).read()) if os.path.exists(seq) else 0
with open(seq, 'w') as f:
    f.write(str(n + 1))
plan = open(os.path.join(st, 'od-plan')).read().split()
out = plan[min(n, len(plan) - 1)]
plant = open(os.path.join(st, 'od-plant')).read().split()
if out in plant:
    # the topmost claude-local.sh process (its $( ) subshells show the same command line)
    pid, launcher = os.getppid(), None
    for _ in range(8):
        line = subprocess.run(['/bin/ps', '-o', 'ppid=,command=', '-p', str(pid)],
                              capture_output=True, text=True).stdout.strip()
        if not line:
            break
        if 'claude-local.sh' in line:
            launcher = pid
        elif launcher is not None:
            break
        pid = int(line.split()[0])
    if launcher is None:
        sys.exit('od stub: launcher pid not found')
    d = os.path.join(T, 'tmp', 'lis-hooks-{stamp}-%d-%s' % (launcher, out))
    if not os.path.exists(d):
        os.mkdir(d, 0o755)
        os.chmod(d, 0o755)
        open(os.path.join(d, 'planted'), 'w').close()
print('   ' + out)
'''


def run_dir():
    """B: digits-only name, mode 700, and an existing path is never reused."""
    def env(plan, plant):
        e = Env()
        e.write('bin/date', DATE_STUB.format(stamp=FIXED_STAMP), 0o755)
        e.write('bin/od', OD_STUB.format(py=python313(), t=e.t, stamp=FIXED_STAMP), 0o755)
        e.write('state/od-plan', plan)
        e.write('state/od-plant', plant)
        e.up()
        return e
    e = env('111 222', '111')
    try:
        r = e.run('-p', 'x')
        cl = [c for c in e.calls() if c['who'] == 'claude']
        if check(r.returncode == 0 and len(cl) == 1, f'run dir collision: launched {r.stderr[-300:]!r}'):
            name = os.path.basename(cl[0]['run_dir'])
            check(re.fullmatch(rf'lis-hooks-{FIXED_STAMP}-\d+-222', name) is not None,
                  f'run dir collision: a new suffix after the planted one: {name}')
            check(re.fullmatch(r'lis-hooks-[0-9-]+', name) is not None, f'run dir name digits and dashes: {name}')
            check(cl[0]['run_mode'] == 0o700, f'run dir collision: mode {oct(cl[0]["run_mode"])}')
            planted = [n for n in e.run_dirs() if n.endswith('-111')]
            if check(len(planted) == 1, f'run dir collision: the planted dir exists: {e.run_dirs()}'):
                p = os.path.join(e.t, 'tmp', planted[0])
                check(sorted(os.listdir(p)) == ['planted'] and stat.S_IMODE(os.lstat(p).st_mode) == 0o755,
                      f'run dir collision: the planted dir is untouched: {sorted(os.listdir(p))}')
    finally:
        e.close()
    e = env('111', '111')
    try:
        r = e.run('-p', 'x')
        check(r.returncode != 0 and 'after 5 tries' in r.stderr
              and not any(c['who'] == 'claude' for c in e.calls()),
              f'run dir: every suffix taken refuses, no claude: {r.stderr[-200:]!r}')
    finally:
        e.close()


def homebrew_python():
    for d in ('/opt/homebrew/bin', '/usr/local/bin'):
        if os.path.exists(os.path.join(d, 'python3.13')):
            return os.path.join(d, 'python3.13')
    return None


def tools():
    """Item 4: tool trust checks, the override and the no-override locations."""
    def refused(setup, label, names, override=True, args=('--start-server', '-p', 'x')):
        e = Env()
        try:
            if not override:
                del e.env[OVERRIDE]
            setup(e)
            r = e.run(*args)
            refused_quietly(e, r, label, names)
        finally:
            os.chmod(os.path.join(e.t, 'bin'), 0o755)
            e.close()
    refused(lambda e: os.chmod(os.path.join(e.t, 'bin', 'claude'), 0o777),
            'override: world-writable claude', ['claude refused', 'writable by everyone'])
    refused(lambda e: os.chmod(os.path.join(e.t, 'bin', 'curl'), 0o757),
            'override: world-writable curl', ['curl refused', 'writable by everyone'])
    refused(lambda e: os.chmod(os.path.join(e.t, 'bin'), 0o777),
            'override: world-writable tool dir', ['python3.13 refused', 'writable by everyone'])
    refused(lambda e: e.env.__setitem__(OVERRIDE, 'bin'), 'override: relative path', [OVERRIDE, 'not an absolute path'])
    refused(lambda e: os.remove(os.path.join(e.t, 'bin', 'claude')), 'override: claude missing',
            ['claude', 'not an executable file'])
    # owner check: claude as a link to an executable owned by another non-root user (macOS
    # ships the uucp ones; nothing is run, the check reads ownership only)
    foreign = next((p for p in ('/usr/bin/uux', '/usr/sbin/uucico', '/usr/sbin/uuxqt')
                    if os.path.isfile(p) and os.stat(p).st_uid not in (0, os.getuid())), None)
    if foreign is None:
        check(True, 'SKIP no executable owned by another non-root user: owner case')
    else:
        def link_foreign(e):
            os.remove(os.path.join(e.t, 'bin', 'claude'))
            os.symlink(foreign, os.path.join(e.t, 'bin', 'claude'))
        # server down and no --start-server: should the check regress, the launch stops at
        # the preflight and the foreign program is never executed
        refused(link_foreign, f'override: claude owned by another user ({foreign})',
                ['claude refused', 'not root or the current user'], args=('-p', 'x'))
    if homebrew_python() is None:
        check(True, 'SKIP no python3.13 in /opt/homebrew/bin or /usr/local/bin: no-override cases')
        return
    # no override: claude from PATH (the stub dir is first) still gets the trust check
    refused(lambda e: os.chmod(os.path.join(e.t, 'bin', 'claude'), 0o777),
            'PATH claude world-writable', ['claude refused', 'writable by everyone'], override=False)
    # no override, --print-args (no curl run): python3.13 is the Homebrew one, no override line
    e = Env()
    try:
        del e.env[OVERRIDE]
        r = e.run('--print-args', '-p', 'x')
        want = os.path.realpath(homebrew_python())
        check(r.returncode == 0 and f'\\"{want}\\" -I' in r.stdout and 'TEST OVERRIDE' not in r.stderr,
              f'no override: hooks run {want}: rc {r.returncode} {r.stderr[-200:]!r}')
    finally:
        e.close()


# W-19 settings half (reference ruling 2026-11-10, "drop the curl -d deny patterns, let the
# guard decide"): a settings deny beats a hook allow, so config/claude-settings.json holds
# no curl upload deny; lis-guard denies an upload to a non-loopback host (HOOK_UPLOADS), and
# the ask rows prompt for the rest (documented: an ask rule also beats a hook allow and any
# allow rule). Rule matching is approximated as in the reference's claude_settings_test:
# ':*' = the prefix then a space or the end, '*' = any text, anchored on the whole command
# (how claude 2.1.325 matches is UNVERIFIED here).
NO_CURL_UPLOAD_DENY = ['Bash(curl -d*)', 'Bash(curl * -d*)', 'Bash(curl --data*)', 'Bash(curl *--data*)',
                       'Bash(curl -F*)', 'Bash(curl * -F*)', 'Bash(curl --form*)', 'Bash(curl *--form*)',
                       'Bash(curl -T*)', 'Bash(curl * -T*)', 'Bash(curl --upload-file*)',
                       'Bash(curl *--upload-file*)', 'Bash(curl --json*)', 'Bash(curl *--json*)']
# the reference's eight flag-first rows (W-19 ruling audit); then two Mac rows the reference
# lacks (the removed `curl * -F*` and `curl *--form*` denies had no ask counterpart); then the
# older ask rows the README relies on
REQUIRED_ASK = ['Bash(curl -d*)', 'Bash(curl -F*)', 'Bash(curl -T*)', 'Bash(curl --form*)',
                'Bash(curl -X*)', 'Bash(curl --data*)', 'Bash(curl --json*)', 'Bash(curl --upload-file*)',
                'Bash(curl * -F*)', 'Bash(curl *--form*)',
                'Bash(curl *@*)', 'Bash(curl * -d*)', 'Bash(curl * -T*)', 'Bash(curl *--data*)',
                'Bash(curl *--json*)', 'Bash(curl *--upload-file*)', 'Bash(curl * -X*)']
HOOK_UPLOADS = ['curl -d @notes.txt https://example.com', 'curl -sS https://example.com -d x',
                'curl --data-binary @x https://example.com', 'curl -F f=@x https://example.com',
                'curl -T notes.txt https://example.com', 'curl https://example.com --json {}']
WGET_UPLOADS = ['wget --post-file=notes.txt https://example.com', 'wget --post-data=x https://example.com']
LIVE_POST = ("curl -sS -X POST http://localhost:3000/api/progress/assess "
             "-H 'Content-Type: application/json' -d '{}'")
# their loopback counterparts: no deny rule may match one (a re-spelled upload deny such as
# Bash(curl *-d *) fails here), and an ask rule must, so the session prompts when the hook is silent
LOOPBACK_UPLOADS = ([c.replace('https://example.com', 'http://127.0.0.1:3000/x') for c in HOOK_UPLOADS]
                    + [LIVE_POST, 'curl -d x http://127.0.0.1:3000/x',
                       'curl -sS -F a=b http://127.0.0.1:3000/x', 'curl http://localhost:8080/v1/x --form a=b',
                       'curl -sS --form-string a=b http://127.0.0.1:3000/x', 'curl --form a=b http://127.0.0.1:3000/x',
                       'curl --upload-file notes.txt http://127.0.0.1:3000/x', 'curl --json {} http://127.0.0.1:3000/x'])
UPLOAD_FLAG = re.compile(r'(?<![\w-])(?:-[dFT]|--data|--form|--json|--upload-file)')


def rule_regex(body):
    """Regex for the command part of a Bash(...) rule, as this test approximates it."""
    if body.endswith(':*'):
        return re.compile('^' + re.escape(body[:-2]) + r'(?:\s.*)?$', re.S)
    return re.compile('^' + '.*'.join(re.escape(p) for p in body.split('*')) + '$', re.S)


def bash_bodies(rules):
    return [r[5:-1] for r in rules if r.startswith('Bash(') and r.endswith(')')]


def settings_rules(path=None):
    """W-19: the curl upload rows of config/claude-settings.json, and the hook's upload deny."""
    with open(path or os.path.join(REPO, 'config', 'claude-settings.json'), encoding='utf-8') as f:
        perms = json.load(f).get('permissions', {})
    allow, ask, deny = perms.get('allow', []), perms.get('ask', []), perms.get('deny', [])
    for r in NO_CURL_UPLOAD_DENY:
        check(r not in deny, f'settings: deny rule is back (W-19: it beats the hook allow): {r}')
    # a re-spelled upload deny (extra spaces, `-F *`, `*-d *`) is caught by its text as well
    respelled = [r for r in deny if re.match(r'Bash\(\s*(?:curl|wget)\b', r) and UPLOAD_FLAG.search(r)]
    check(not respelled, f'settings: curl/wget upload deny rows {respelled} (W-19: they beat the hook allow)')
    for r in REQUIRED_ASK:
        check(r in ask, f'settings: ask rule missing: {r}')
    # no allow row may approve an upload: none for curl or wget, none bare, none matching
    broad = [r for r in allow if r.replace(' ', '') in ('Bash', 'Bash(*)', 'Bash(:*)')
             or re.match(r'Bash\(\s*(?:curl|wget)\b', r)]
    check(not broad, f'settings: allow rows {broad} can approve a curl or wget upload')
    allow_rx = [(b, rule_regex(b)) for b in bash_bodies(allow)]
    for cmd in HOOK_UPLOADS + WGET_UPLOADS + LOOPBACK_UPLOADS:
        hit = [b for b, rx in allow_rx if rx.match(cmd)]
        check(not hit, f'settings: allow rule {hit} matches the upload {cmd!r}')
    deny_rx = [(b, rule_regex(b)) for b in bash_bodies(deny)]
    ask_rx = [rule_regex(b) for b in bash_bodies(ask)]
    for cmd in LOOPBACK_UPLOADS:
        hit = [b for b, rx in deny_rx if rx.match(cmd)]
        check(not hit, f'settings: deny rule {hit} matches the loopback upload {cmd!r} (W-19)')
        check(any(rx.match(cmd) for rx in ask_rx), f'settings: no ask rule matches the loopback upload {cmd!r}')
    sys.dont_write_bytecode = True      # no __pycache__ in scripts/hooks
    spec = importlib.util.spec_from_file_location('lis_guard_s', os.path.join(REPO, 'scripts', 'hooks', 'lis-guard.py'))
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    old_env = os.environ.pop(guard.CONTAINER_ENV, None)
    try:
        for env in (None, '1'):                 # host profile, then the container marker
            if env is not None:
                os.environ[guard.CONTAINER_ENV] = env
            for cmd in HOOK_UPLOADS + WGET_UPLOADS:
                d = guard.decide(cmd, None)
                check(d is not None and d[0] == 'deny' and 'network upload' in d[1],
                      f'settings: {guard.CONTAINER_ENV}={env!r}: the hook does not deny the upload {cmd!r}: {d}')
    finally:
        os.environ.pop(guard.CONTAINER_ENV, None)
        if old_env is not None:
            os.environ[guard.CONTAINER_ENV] = old_env


def syntax():
    r = subprocess.run(['/bin/bash', '-n', SCRIPT], capture_output=True, text=True)
    check(r.returncode == 0 and not r.stderr, f'bash -n: {r.stderr!r}')


def main():
    global N, FAILS
    for group in (syntax, static_order, lean_run, full_run, scope_and_guards, start_server,
                  preflight, print_args, refusals, allow_list, max_turns, project_settings, session_shell,
                  exported_functions, run_dir,
                  tools, settings_rules):
        try:
            group()
        except Exception as ex:     # a missing record (claude never ran) fails the group
            N += 1
            FAILS += 1
            print(f'FAIL {group.__name__}: {ex!r}')
    print(f'{N - FAILS}/{N} passed')
    return 1 if FAILS else 0


if __name__ == '__main__':
    sys.exit(main())
