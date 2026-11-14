"""Tests for scripts/serve.sh.

Run from the repo root: python3.13 -I scripts/tests/serve_test.py

No model and no real llama-server start is needed: each test copies serve.sh into a temp
checkout with its own config/lis.conf, a fake models/manifest.json entry (a tiny file with
its real size and sha256) and a fake `llama-server` that serve.sh runs only because the
test-only override LIS_TEST_LLAMA_SERVER names it (a copy first on PATH is ignored). The
fake records its argv, pid, environment and the key file's mode, then exits, sleeps or
answers /health, as the test asks (FAKE_MODE). python3.13 must be installed in
/opt/homebrew/bin or /usr/local/bin (serve.sh takes it from there, never from PATH).
Tests that need the installed llama-server (its real argument parser) skip without it.
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SERVE = REPO / "scripts" / "serve.sh"
PY = shutil.which("python3.13") or sys.executable
INSTALLED = next((p for p in ("/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server") if os.path.exists(p)), None)

# The refusal list of serve.ps1 ($fixedFlag, Windows reference commit cafc34b).
REFUSED = [
    "--host", "--port", "--api-key", "--api-key-file", "--connect", "--connect-code", "--tools",
    "--agent", "--reuse-port", "--webui", "--ui", "--mcp-servers-config", "--mcp-servers-json",
    "--ui-mcp-proxy", "--webui-mcp-proxy", "--path", "--media-path", "--hf-token",
    "--slot-save-path", "--log-prompts-dir", "--props", "--models-dir", "--models-preset",
    "-ag", "-hft",
]
# The macOS build additions (MAC_FLAG_RE): flags of llama-server (read from 0.9.0's --help;
# all still in 1.0.0, build 12359, checked 2026-11-14) that the Windows list does not name
# and that widen exposure.
MAC_REFUSED = [
    "--ui-config-file", "--webui-config-file", "--log-file", "--lookup-cache-dynamic", "--tools-runtime",
    "--video-ffmpeg-dir", "--rpc", "--model", "--model-url", "--docker-repo", "--hf-repo", "--hf-file",
    "--spec-draft-hf", "--hf-repo-draft", "--mmproj-url", "--embd-gemma-default", "--fim-qwen-1.5b-default",
    "--fim-qwen-3b-default", "--fim-qwen-7b-default", "--fim-qwen-7b-spec", "--fim-qwen-14b-spec",
    "--fim-qwen-30b-default", "--gpt-oss-20b-default", "--gpt-oss-120b-default", "--vision-gemma-4b-default",
    "--vision-gemma-12b-default",
    "-lcd", "-m", "-mu", "-dr", "-hf", "-hfr", "-hff", "-hfd", "-hfrd", "-mmu",
]
HYPHENATED = [f for f in REFUSED + MAC_REFUSED if f.startswith("--") and "-" in f[2:]]
# The allow-list (ALLOWED_FLAGS in serve.sh), by value kind.
ALLOW_LIST = {
    "count": ["-t", "--threads", "-tb", "--threads-batch", "-b", "--batch-size", "-ub", "--ubatch-size",
              "--cache-reuse", "-ctxcp", "--ctx-checkpoints", "--swa-checkpoints", "-cms", "--checkpoint-min-step",
              "-cram", "--cache-ram", "-ncmoe", "--n-cpu-moe", "-fitc", "--fit-ctx", "--threads-http"],
    "layers": ["-ngl", "--gpu-layers", "--n-gpu-layers"],
    "on-off": ["-fit", "--fit"],
    "mib-list": ["-fitt", "--fit-target"],
    "load-mode": ["-lm", "--load-mode"],
    "percent": ["--poll"],
    "verbosity": ["-lv", "--verbosity", "--log-verbosity"],
    "none": ["--swa-full", "-cmoe", "--cpu-moe", "--warmup", "--no-warmup", "-kvu", "--kv-unified", "-no-kvu",
             "--no-kv-unified", "-kvo", "--kv-offload", "-nkvo", "--no-kv-offload", "--repack", "-nr", "--no-repack",
             "--op-offload", "--no-op-offload", "--cache-idle-slots", "--no-cache-idle-slots", "--log-timestamps",
             "--no-log-timestamps", "--log-prefix", "--no-log-prefix"],
}
SAMPLE = {"count": "4", "layers": "all", "on-off": "on", "mib-list": "1024,512", "load-mode": "mmap+mlock",
          "percent": "50", "verbosity": "3", "none": None}
# Flags this build accepts that are NOT on the allow-list (weights and file loaders,
# network/TLS/CORS, verbosity above info, fixed and sampling flags, misspellings).
NOT_ALLOWED = [
    ["--api-prefix", "/x"], ["--ssl-key-file", "k.pem"], ["--ssl-cert-file", "c.pem"], ["--cors-origins", "x"],
    ["--ui-config", "{}"], ["--model-draft", "d.gguf"], ["-md", "d.gguf"], ["--spec-draft-model", "d.gguf"],
    ["--lora", "a.gguf"], ["--lora-scaled", "a.gguf", "1"], ["--control-vector", "v.gguf"], ["-mm", "p.gguf"],
    ["--mmproj", "p.gguf"], ["-lcs", "c.bin"], ["--grammar-file", "g.gbnf"], ["-jf", "s.json"], ["-v"],
    ["--verbose"], ["--log-verbose"], ["-c", "4096"], ["--ctx-size", "4096"], ["-np", "4"], ["--alias", "x"],
    ["--temp", "1"], ["--chat-template-file", "t.jinja"], ["--offline"], ["--bogus"], ["--THREADS", "4"],
    ["-T", "4"], ["-no_kvu"], ["--threa", "4"], ["--fim-qwen-1x5b-default"], ["--hostname-like"], ["--ports"],
    ["-agx"], ["-mx"], ["--models"], ["--rpcs"], ["--"],
]

FAKE = r'''#!@PY@ -I
import http.server, json, os, signal, sys, time
argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("version: 0 (fake)")
    sys.exit(0)
def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None
FAMILIES = ("LLAMA", "GGML", "DYLD", "HF_", "HUGGING_FACE", "MODEL_ENDPOINT", "PERL5", "PERLLIB", "MTL_", "Malloc",
            "KEEP_ME", "SHELLOPTS")
info = {"argv": argv, "cwd": os.getcwd(), "pid": os.getpid(),
        "env": {k: v for k, v in os.environ.items()
                if k.startswith(FAMILIES) or k.lower().endswith("_proxy")}}
keyfile = opt("--api-key-file")
if keyfile and os.path.exists(keyfile):
    info["key_mode"] = stat_mode = os.stat(keyfile).st_mode & 0o777
    info["dir_mode"] = os.stat(os.path.dirname(keyfile)).st_mode & 0o777
    with open(keyfile) as f:
        info["key"] = f.read()
rec = os.environ.get("FAKE_RECORD")
if rec:
    with open(rec, "w") as f:
        json.dump(info, f)
mode = os.environ.get("FAKE_MODE", "exit")
if mode == "sleep":
    if os.environ.get("FAKE_TERM_EXIT"):
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(int(os.environ["FAKE_TERM_EXIT"])))
    time.sleep(float(os.environ.get("FAKE_SLEEP", "30")))
elif mode == "serve":
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"status":"ok"}'
            self.send_response(200 if self.path == "/health" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *a):
            pass
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    http.server.HTTPServer((opt("--host"), int(opt("--port"))), H).serve_forever()
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def alive(pid):
    out = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
    return bool(out) and "Z" not in out


def exe_of(pid):
    out = subprocess.run(["/usr/sbin/lsof", "-w", "-a", "-p", str(pid), "-d", "txt", "-Fn"],
                         capture_output=True, text=True).stdout
    return next((l[1:] for l in out.splitlines() if l.startswith("n")), "")


def end(pid):
    """Test cleanup of a fake this test started (never a computed list)."""
    if isinstance(pid, int) and pid > 1 and alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not alive(pid):
                return
            time.sleep(0.1)


class Tree:
    """A throwaway checkout: scripts/serve.sh, config, manifest, model, fake binary."""

    def __init__(self, port=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        (self.root / "scripts").mkdir()
        shutil.copy2(SERVE, self.root / "scripts" / "serve.sh")
        (self.root / "config" / "chat-templates").mkdir(parents=True)
        (self.root / "config" / "chat-templates" / "t.jinja").write_text("{{ messages }}\n")
        (self.root / "models" / "fake").mkdir(parents=True)
        self.model = self.root / "models" / "fake" / "fake.gguf"
        self.model.write_bytes(b"GGUF fake model payload\n")
        (self.root / "bin").mkdir()
        self.fake = self.root / "bin" / "llama-server"
        self.fake.write_text(FAKE.replace("@PY@", PY))
        self.fake.chmod(0o755)
        # A python3.13 first on PATH that leaves a canary: serve.sh must never run it.
        self.py_canary = self.root / "path-python-ran"
        fake_py = self.root / "bin" / "python3.13"
        fake_py.write_text("#!/bin/sh\ntouch %s\nexit 1\n" % shlex.quote(str(self.py_canary)))
        fake_py.chmod(0o755)
        self.record = self.root / "record.json"
        self.port = port or free_port()
        self.conf = {"PORT": str(self.port), "CTX": "131072", "CLIENT_CTX": "114688", "KV_TYPE": "q4_0",
                     "MODEL_ID": "fake", "CHAT_TEMPLATE": ""}
        self.write_conf()
        self.entry = {"id": "fake", "path": "models/fake/fake.gguf", "repo": "x/y", "revision": "0" * 40,
                      "file": "fake.gguf", "size": self.model.stat().st_size,
                      "sha256": hashlib.sha256(self.model.read_bytes()).hexdigest()}
        self.write_manifest()

    def write_conf(self, extra_lines=()):
        lines = ["# test config"] + ["%s=%s" % kv for kv in self.conf.items()] + list(extra_lines)
        (self.root / "config" / "lis.conf").write_text("\n".join(lines) + "\n")

    def write_manifest(self, entries=None):
        data = {"about": "test", "files": [self.entry] if entries is None else entries}
        (self.root / "models" / "manifest.json").write_text(json.dumps(data))

    @property
    def logs(self):
        return self.root / "logs"

    def env(self, **extra):
        env = {
            "PATH": "%s:%s:/usr/bin:/bin:/usr/sbin:/sbin" % (self.root / "bin", os.path.dirname(PY)),
            "HOME": os.environ.get("HOME", "/tmp"),
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "FAKE_RECORD": str(self.record),
            "LIS_TEST_LLAMA_SERVER": str(self.fake),
        }
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    def argv(self, *args):
        return ["/bin/bash", str(self.root / "scripts" / "serve.sh")] + list(args)

    def run(self, *args, timeout=60, **env):
        return subprocess.run(self.argv(*args), env=self.env(**env), capture_output=True, text=True,
                              timeout=timeout, cwd="/")

    def popen(self, *args, **env):
        return subprocess.Popen(self.argv(*args), env=self.env(**env), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, cwd="/")

    def rec(self):
        return json.loads(self.record.read_text())

    def pidfile(self):
        return self.logs / "llama-server.pid"

    def close(self):
        p = self.pidfile()
        if p.is_file():
            v = p.read_text().strip()
            if v.isdigit():
                end(int(v))
        self.tmp.cleanup()


def opt(argv, name):
    i = argv.index(name)
    return argv[i + 1]


def allowed_sample():
    """Every allow-list spelling with a sample value, and the same with every long flag
    spelled with underscores (what the binary also accepts)."""
    plain, under = [], []
    for kind, flags in ALLOW_LIST.items():
        for f in flags:
            val = [SAMPLE[kind]] if SAMPLE[kind] else []
            plain += [f] + val
            under += ["--" + f[2:].replace("-", "_") if f.startswith("--") else f] + val
    return plain, under


class ServeTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()

    def tearDown(self):
        self.t.close()

    # ------------------------------------------------------------ dry run
    def dry(self, *extra, **env):
        r = self.t.run("--dry-run", *extra, **env)
        return r

    def test_dry_run_command_line(self):
        r = self.dry()
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 1, r.stdout)
        argv = shlex.split(lines[0])
        self.assertEqual(argv[0], str(self.t.fake))
        self.assertEqual(argv[1], "--offline", "--offline is the first fixed flag")
        self.assertEqual(argv.count("--offline"), 1)
        self.assertIn("TEST OVERRIDE: LIS_TEST_LLAMA_SERVER=", r.stderr)
        self.assertEqual(argv.count("--host"), 1)
        self.assertEqual(opt(argv, "--host"), "127.0.0.1")
        self.assertEqual(opt(argv, "--port"), str(self.t.port))
        self.assertEqual(opt(argv, "-m"), str(self.t.model))
        self.assertEqual(opt(argv, "-c"), "131072")
        self.assertEqual(opt(argv, "-ctk"), "q4_0")
        self.assertEqual(opt(argv, "-ctv"), "q4_0")
        self.assertEqual(opt(argv, "--reasoning"), "off")
        self.assertEqual(opt(argv, "-fa"), "on")
        self.assertEqual(opt(argv, "--api-key-file"), str(self.t.logs / "api-key"))
        for flag in ("--no-webui", "--metrics", "--no-mmproj"):
            self.assertIn(flag, argv)
        self.assertNotIn("--chat-template-file", argv)
        self.assertNotIn("0.0.0.0", lines[0])
        self.assertFalse(self.t.logs.exists(), "a dry run writes nothing")
        self.assertFalse(self.t.py_canary.exists(), "python3.13 is never taken from PATH")

    def test_dry_run_config_values(self):
        self.t.conf.update(CTX="32768", KV_TYPE="q8_0", CHAT_TEMPLATE="config/chat-templates/t.jinja")
        self.t.write_conf()
        argv = shlex.split(self.dry().stdout)
        self.assertEqual(opt(argv, "-c"), "32768")
        self.assertEqual(opt(argv, "-ctk"), "q8_0")
        self.assertEqual(opt(argv, "-ctv"), "q8_0")
        self.assertEqual(opt(argv, "--chat-template-file"), str(self.t.root / "config/chat-templates/t.jinja"))
        argv = shlex.split(self.dry("--reasoning", "auto").stdout)
        self.assertEqual(opt(argv, "--reasoning"), "auto")

    def test_dry_run_needs_no_model_file(self):
        self.t.model.unlink()
        r = self.dry()
        self.assertEqual(r.returncode, 0, r.stderr)

    # ------------------------------------------------------------ tools (never from PATH)
    @unittest.skipUnless(INSTALLED, "no llama-server in /opt/homebrew/bin or /usr/local/bin")
    def test_path_fake_not_run_without_override(self):
        r = self.dry(LIS_TEST_LLAMA_SERVER=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = shlex.split(r.stdout)
        self.assertEqual(argv[0], os.path.realpath(INSTALLED), "the installed binary, resolved, not PATH's fake")
        self.assertNotIn("TEST OVERRIDE", r.stderr)
        self.assertFalse(self.t.py_canary.exists())

    def test_override_announced_in_log(self):
        r = self.t.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        log = next(self.t.logs.glob("server-*.log")).read_text()
        self.assertIn("serve.sh: TEST OVERRIDE: LIS_TEST_LLAMA_SERVER=%s" % self.t.fake, log)

    def test_override_must_be_absolute_and_trusted(self):
        r = self.dry(LIS_TEST_LLAMA_SERVER="bin/llama-server")
        self.assertEqual(r.returncode, 1)
        self.assertIn("absolute", r.stderr)
        self.t.fake.chmod(0o757)
        r = self.dry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("writable by everyone", r.stderr)
        self.t.fake.chmod(0o755)
        (self.t.root / "bin").chmod(0o777)
        try:
            r = self.dry()
            self.assertEqual(r.returncode, 1)
            self.assertIn("writable by everyone", r.stderr)
        finally:
            (self.t.root / "bin").chmod(0o755)
        self.assertEqual(self.dry().returncode, 0)

    def test_override_writable_by_a_shared_group_refused(self):
        if subprocess.run(["chgrp", "everyone", str(self.t.fake)], capture_output=True).returncode != 0:
            self.skipTest("cannot chgrp to everyone here")
        self.t.fake.chmod(0o775)
        r = self.dry()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("writable by group everyone", r.stderr)

    def chmod_acl(self, path, *args):
        r = subprocess.run(["/bin/chmod", *args, str(path)], capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest("chmod %s failed here: %s" % (" ".join(args), r.stderr))

    def test_override_with_acl_granting_write_refused(self):
        """trusted_node reads the ACL from ls -led: an allow entry giving anyone but the
        owner a right beyond read/list/execute refuses, on the file or its directory."""
        bindir = self.t.root / "bin"
        me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
        refused = [
            (bindir, "group:everyone allow add_file", "allows group:everyone add_file"),
            (bindir, "group:staff allow delete_child", "allows group:staff delete_child"),
            (self.t.fake, "user:nobody allow write", "allows user:nobody write"),
            (self.t.fake, "group:everyone allow read,writeattr", "allows group:everyone writeattr"),
            (self.t.fake, "user:root allow append", "allows user:root append"),
            (bindir, "group:everyone allow list,add_subdirectory,file_inherit", "allows group:everyone add_subdirectory"),
        ]
        try:
            for path, entry, msg in refused:
                with self.subTest(path=path.name, entry=entry):
                    self.chmod_acl(path, "+a", entry)
                    r = self.dry()
                    self.assertEqual(r.returncode, 1, r.stdout)
                    self.assertIn(msg, r.stderr)
                    self.assertIn("LIS_TEST_LLAMA_SERVER", r.stderr)
                    self.chmod_acl(path, "-N")
            accepted = [
                (bindir, "group:everyone deny delete"),
                (bindir, "group:everyone allow list,search,readattr"),
                (self.t.fake, "user:nobody allow read,execute"),
                (self.t.fake, "user:%s allow write" % me),
            ]
            for path, entry in accepted:
                self.chmod_acl(path, "+a", entry)
            r = self.dry()
            self.assertEqual(r.returncode, 0, r.stderr)
            # An inherited allow entry counts like any other: the fake is made in a
            # directory with an inheritable entry, then renamed into bin/ (keeping it).
            self.chmod_acl(bindir, "-N")
            self.chmod_acl(self.t.fake, "-N")
            nursery = self.t.root / "nursery"
            nursery.mkdir()
            self.chmod_acl(nursery, "+a", "group:everyone allow add_file,file_inherit")
            born = nursery / "llama-server"
            born.write_text(FAKE.replace("@PY@", PY))
            born.chmod(0o755)
            os.replace(born, self.t.fake)
            out = subprocess.run(["/bin/ls", "-led", str(self.t.fake)], capture_output=True, text=True).stdout
            self.assertIn("inherited allow write", out)
            r = self.dry()
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("allows group:everyone write", r.stderr)
        finally:
            for path in (self.t.fake, bindir):
                if path.exists():
                    subprocess.run(["/bin/chmod", "-N", str(path)], capture_output=True)

    # ------------------------------------------------------------ allow-list
    def test_allowed_extra_args_pass_through(self):
        r = self.dry("--", "--threads", "4", "--swa_full", "-lv", "3", "--fit", "on", "--n_gpu_layers", "all")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = shlex.split(r.stdout)
        self.assertEqual(argv[-9:], ["--threads", "4", "--swa-full", "-lv", "3", "--fit", "on", "--n-gpu-layers", "all"])

    def test_every_allowed_flag_passes_both_channels(self):
        plain, under = allowed_sample()
        for args in (plain, under):
            with self.subTest(channel="--", underscores=args is under):
                r = self.dry("--", *args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(shlex.split(r.stdout)[-len(plain):], plain, "normalised to the hyphen spelling")
            with self.subTest(channel="server_args", underscores=args is under):
                self.entry_with(server_args=args)
                r = self.dry()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(shlex.split(r.stdout)[-len(plain):], plain)
                self.entry_with(server_args=[])

    @unittest.skipUnless(INSTALLED, "no installed llama-server")
    def test_allowed_flags_parse_in_installed_binary(self):
        """Each allow-list spelling, with a sample value, is accepted by the real parser."""
        for kind, flags in ALLOW_LIST.items():
            for f in flags:
                args = [f] + ([SAMPLE[kind]] if SAMPLE[kind] else [])
                with self.subTest(args=args):
                    r = subprocess.run([INSTALLED] + args + ["--version"], capture_output=True, text=True, timeout=30)
                    out = r.stdout + r.stderr
                    self.assertEqual(r.returncode, 0, out)
                    self.assertIn("version:", out)
                    self.assertNotIn("invalid", out)

    @unittest.skipUnless(INSTALLED, "no installed llama-server")
    def test_binary_normalises_underscores(self):
        """The parser quirk the normalisation mirrors: --reuse_port is --reuse-port."""
        r = subprocess.run([INSTALLED, "--reuse_port", "--version"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = subprocess.run([INSTALLED, "--bogus_flag", "--version"], capture_output=True, text=True, timeout=30)
        self.assertIn("--bogus-flag", r.stdout + r.stderr)

    def assert_extra_refused(self, args, msg):
        r = self.dry("--", *args)
        self.assertEqual(r.returncode, 1, (args, r.stdout))
        self.assertEqual(r.stdout, "")
        self.assertIn(msg, r.stderr)

    def assert_server_args_refused(self, args, msg):
        self.entry_with(server_args=args)
        r = self.dry()
        self.assertEqual(r.returncode, 1, (args, r.stdout))
        self.assertEqual(r.stdout, "")
        self.assertIn("server_args of manifest entry fake", r.stderr)
        self.assertIn(msg, r.stderr)

    def test_each_refused_flag(self):
        for flag in REFUSED + MAC_REFUSED:
            forms = [[flag, "x"], [flag + "=x"], [flag.upper(), "x"]]
            if flag in HYPHENATED:
                u = "--" + flag[2:].replace("-", "_")
                forms += [[u, "x"], [u + "=x"], [u.upper(), "x"]]
            for form in forms:
                with self.subTest(form=form):
                    self.assert_extra_refused(form, "may not set")

    def test_underscore_spellings_refused_through_server_args(self):
        for flag in HYPHENATED:
            u = "--" + flag[2:].replace("-", "_")
            with self.subTest(flag=u):
                self.assert_server_args_refused([u, "x"], "may not set %s" % u)

    def test_server_args_refused_flags(self):
        for flag in REFUSED + MAC_REFUSED:
            with self.subTest(flag=flag):
                self.assert_server_args_refused([flag, "x"], "may not set")
        self.assert_server_args_refused(["--HOST=x"], "may not set")

    def test_not_on_allow_list(self):
        for args in NOT_ALLOWED:
            with self.subTest(args=args):
                self.assert_extra_refused(args, "not on serve.sh's allow-list")
                self.assert_server_args_refused(args, "not on serve.sh's allow-list")

    def test_values_validated(self):
        cases = [
            (["--threads", "-1"], "the value must be a whole number"),
            (["--threads", "--host"], "the value must be"),
            (["--threads", "0x10"], "the value must be"),
            (["--threads", "08"], "the value must be"),
            (["--threads", "12345678"], "the value must be"),
            (["--threads"], "needs a value"),
            (["-lv", "4"], "the value must be 0, 1, 2 or 3"),
            (["--log-verbosity", "9"], "the value must be 0, 1, 2 or 3"),
            (["-lv", "-1"], "the value must be"),
            (["--fit"], "needs a value"),
            (["--fit", "--threads", "4"], "the value must be on or off"),
            (["--fit", "auto"], "the value must be on or off"),
            (["-ngl", "-1"], "the value must be"),
            (["-ngl", "most"], "the value must be"),
            (["--load-mode", "bogus"], "the value must be auto, none"),
            (["--poll", "101"], "the value must be a whole number 0-100"),
            (["--fit-target", "1024,"], "the value must be"),
            (["--fit-target", "-1024"], "the value must be"),
            (["--swa-full", "extra"], "is not a flag"),
            (["4"], "is not a flag"),
            (["--threads=4"], "does not accept --flag=value"),
            (["--swa_full=1"], "does not accept --flag=value"),
            (["--reuse_port=1"], "may not set"),
            (["--threads 4"], "whitespace"),
            (["--threads 4 --host x"], "whitespace"),
        ]
        for args, msg in cases:
            with self.subTest(args=args):
                self.assert_extra_refused(args, msg)
        for args, msg in cases[:8] + cases[-3:]:
            with self.subTest(channel="server_args", args=args):
                self.assert_server_args_refused(args, msg)

    def test_refused_flag_on_real_start_writes_nothing(self):
        for args in (["--host", "0.0.0.0"], ["--reuse_port"], ["--lora", "x.gguf"]):
            with self.subTest(args=args):
                r = self.t.run("--", *args)
                self.assertNotEqual(r.returncode, 0)
                self.assertFalse(self.t.logs.exists())
                self.assertFalse(self.t.record.exists(), "llama-server must not start")

    # ------------------------------------------------------------ per-model settings
    def entry_with(self, **fields):
        self.t.entry.update(fields)
        self.t.write_manifest()

    def test_server_args_appended_before_extra(self):
        self.entry_with(server_args=["--swa-full", "--threads", "4", "--n_gpu_layers", "99"])
        argv = shlex.split(self.dry("--", "--batch-size", "512").stdout)
        self.assertEqual(argv[-7:], ["--swa-full", "--threads", "4", "--n-gpu-layers", "99", "--batch-size", "512"])
        r = self.t.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.t.rec()["argv"][-5:], ["--swa-full", "--threads", "4", "--n-gpu-layers", "99"])

    def test_server_args_refused_on_real_start(self):
        for args in (["--host", "0.0.0.0"], ["--api_key_file", "/tmp/k"], ["-md", "d.gguf"]):
            with self.subTest(args=args):
                self.entry_with(server_args=args)
                r = self.t.run()
                self.assertEqual(r.returncode, 1)
                self.assertFalse(self.t.record.exists())
                self.assertFalse((self.t.logs / "api-key").exists())

    def test_sampling_defaults_and_overrides(self):
        argv = shlex.split(self.dry().stdout)
        self.assertEqual([opt(argv, f) for f in ("--temp", "--top-p", "--top-k")], ["0.6", "0.95", "20"])
        self.entry_with(sampling={"temp": 1, "top_p": 0.95, "top_k": 64})
        argv = shlex.split(self.dry().stdout)
        self.assertEqual([opt(argv, f) for f in ("--temp", "--top-p", "--top-k")], ["1.0", "0.95", "64"])
        self.entry_with(sampling={"temp": 0.7})
        argv = shlex.split(self.dry().stdout)
        self.assertEqual([opt(argv, f) for f in ("--temp", "--top-p", "--top-k")], ["0.7", "0.95", "20"])
        self.entry_with(sampling={"top_k": 0, "top_p": 1})
        argv = shlex.split(self.dry().stdout)
        self.assertEqual([opt(argv, f) for f in ("--temp", "--top-p", "--top-k")], ["0.6", "1.0", "0"])
        self.assertEqual(argv.count("--temp"), 1)

    def test_kv_type_override_and_f16(self):
        self.entry_with(kv_type="q8_0")
        argv = shlex.split(self.dry().stdout)
        self.assertEqual((opt(argv, "-ctk"), opt(argv, "-ctv")), ("q8_0", "q8_0"))
        self.entry_with(kv_type="f16")
        argv = shlex.split(self.dry().stdout)
        self.assertEqual((opt(argv, "-ctk"), opt(argv, "-ctv")), ("f16", "f16"))
        del self.t.entry["kv_type"]
        self.t.write_manifest()
        self.t.conf.update(KV_TYPE="f16")
        self.t.write_conf()
        argv = shlex.split(self.dry().stdout)
        self.assertEqual((opt(argv, "-ctk"), opt(argv, "-ctv")), ("f16", "f16"))
        for bad in ("f32 f16", "f16 ", "q3", "F16", "*"):
            with self.subTest(KV_TYPE=bad):
                self.t.conf.update(KV_TYPE=bad)
                self.t.write_conf()
                r = self.dry()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("KV_TYPE", r.stderr)

    def test_kv_type_f8_e4m3(self):
        """1.0.0 lists f8_e4m3 for both -ctk and -ctv; it is accepted from either source."""
        self.t.conf.update(KV_TYPE="f8_e4m3")
        self.t.write_conf()
        argv = shlex.split(self.dry().stdout)
        self.assertEqual((opt(argv, "-ctk"), opt(argv, "-ctv")), ("f8_e4m3", "f8_e4m3"))
        self.t.conf.update(KV_TYPE="q4_0")
        self.t.write_conf()
        self.entry_with(kv_type="f8_e4m3")
        argv = shlex.split(self.dry().stdout)
        self.assertEqual((opt(argv, "-ctk"), opt(argv, "-ctv")), ("f8_e4m3", "f8_e4m3"))

    @unittest.skipUnless(INSTALLED, "no installed llama-server")
    def test_kv_types_parse_in_installed_binary(self):
        """Every KV type serve.sh allows is a value the real parser takes for -ctk and -ctv."""
        kv = re.search(r'^KV_ALLOWED="([^"]*)"$', SERVE.read_text(), re.M).group(1).split()
        self.assertIn("f8_e4m3", kv)
        for t in kv:
            with self.subTest(kv=t):
                r = subprocess.run([INSTALLED, "-ctk", t, "-ctv", t, "--version"], capture_output=True, text=True,
                                   timeout=30)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertNotIn("Unsupported cache type", r.stdout + r.stderr)

    def reasoning_of(self, *args):
        r = self.dry(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = shlex.split(r.stdout)
        self.assertEqual(argv.count("--reasoning"), 1)
        return opt(argv, "--reasoning")

    def test_reasoning_precedence(self):
        """--reasoning flag when given, else the entry's reasoning, else off."""
        self.assertEqual(self.reasoning_of(), "off", "neither: off")
        for v in ("off", "on", "auto"):
            with self.subTest(flag=v, entry=None):
                self.assertEqual(self.reasoning_of("--reasoning", v), v)
        for v in ("off", "on", "auto"):
            with self.subTest(entry=v, flag=None):
                self.entry_with(reasoning=v)
                self.assertEqual(self.reasoning_of(), v, "entry, no flag: the entry's value")
        for entry, flag in (("auto", "off"), ("auto", "on"), ("off", "auto"), ("on", "off"), ("off", "off")):
            with self.subTest(entry=entry, flag=flag):
                self.entry_with(reasoning=entry)
                self.assertEqual(self.reasoning_of("--reasoning", flag), flag, "a given flag wins, off included")
                self.assertEqual(self.reasoning_of("--reasoning", flag, "--", "--threads", "4"), flag)
        self.entry_with(reasoning="auto")
        r = self.t.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(opt(self.t.rec()["argv"], "--reasoning"), "auto", "the real start gets the entry's value")
        time.sleep(1.1)  # distinct log stamp
        r = self.t.run("--reasoning", "off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(opt(self.t.rec()["argv"], "--reasoning"), "off")

    def template_of(self):
        r = self.dry()
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = shlex.split(r.stdout)
        if "--chat-template-file" not in argv:
            return None
        self.assertEqual(argv.count("--chat-template-file"), 1)
        return opt(argv, "--chat-template-file")

    def test_chat_template_field(self):
        tpl_dir = self.t.root / "config" / "chat-templates"
        (tpl_dir / "t2.jinja").write_text("{{ messages }} 2\n")
        conf_tpl, entry_tpl = str(tpl_dir / "t.jinja"), str(tpl_dir / "t2.jinja")
        self.assertIsNone(self.template_of(), "neither: the GGUF's embedded template")
        self.entry_with(chat_template="config/chat-templates/t2.jinja")
        self.assertEqual(self.template_of(), entry_tpl, "entry only")
        self.t.conf.update(CHAT_TEMPLATE="config/chat-templates/t.jinja")
        self.t.write_conf()
        self.assertEqual(self.template_of(), entry_tpl, "the entry's chat_template overrides CHAT_TEMPLATE")
        del self.t.entry["chat_template"]
        self.t.write_manifest()
        self.assertEqual(self.template_of(), conf_tpl, "no entry field: CHAT_TEMPLATE as before")
        self.entry_with(chat_template="config/chat-templates/t2.jinja")
        r = self.t.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(opt(self.t.rec()["argv"], "--chat-template-file"), entry_tpl)

    def test_chat_template_field_paths_refused(self):
        """The entry's chat_template passes exactly the CHAT_TEMPLATE checks, with no
        fallback to CHAT_TEMPLATE when it fails."""
        tpl = self.t.root / "config" / "chat-templates" / "t.jinja"
        (self.t.root / "models" / "x.jinja").write_text("{{ x }}\n")
        (self.t.root / "config" / "link.jinja").symlink_to(tpl)
        out = self.t.root / "tpl-out"
        out.mkdir()
        (out / "t.jinja").write_text("{{ y }}\n")
        (self.t.root / "config" / "out").symlink_to(out)
        outside = "outside %s/config" % self.t.root
        cases = [
            ("models/x.jinja", outside),
            ("config/../models/x.jinja", "must be a plain repo-relative path"),
            ("../x.jinja", "must be a plain repo-relative path"),
            ("/etc/hosts", "must be a plain repo-relative path"),
            ("config/./chat-templates/t.jinja", "must be a plain repo-relative path"),
            ("config//chat-templates/t.jinja", "must be a plain repo-relative path"),
            ("config/link.jinja", "is a symlink"),
            ("config/out/t.jinja", outside),
            ("config/missing.jinja", "chat template not found"),
            ("config/chat-templates", "chat template not found"),
            ("config/$(id).jinja", "has characters outside"),
            ("config/t .jinja", "has characters outside"),
        ]
        self.t.conf.update(CHAT_TEMPLATE="config/chat-templates/t.jinja")
        self.t.write_conf()
        for value, msg in cases:
            with self.subTest(chat_template=value):
                self.entry_with(chat_template=value)
                r = self.dry()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertEqual(r.stdout, "")
                self.assertIn("chat_template of manifest entry fake", r.stderr)
                self.assertIn(msg, r.stderr)
        self.entry_with(chat_template="config/missing.jinja")
        r = self.t.run()
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.t.logs.exists(), "a refused template writes nothing")
        self.assertFalse(self.t.record.exists(), "llama-server must not start")

    def test_conf_template_still_checked_when_entry_overrides(self):
        self.entry_with(chat_template="config/chat-templates/t.jinja")
        self.t.conf.update(CHAT_TEMPLATE="config/missing.jinja")
        self.t.write_conf()
        r = self.dry()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("CHAT_TEMPLATE in", r.stderr)
        self.assertIn("not found", r.stderr)

    def test_malformed_entries_refused(self):
        base = dict(self.t.entry)
        cases = [
            ({"server_args": "--swa-full"}, "server_args must be a list"),
            ({"server_args": [1]}, "server_args elements"),
            ({"server_args": [""]}, "server_args elements"),
            ({"server_args": ["--threads\n4"]}, "server_args elements"),
            ({"server_args": ["a\tb"]}, "server_args elements"),
            ({"server_args": None}, "server_args must be a list"),
            ({"sampling": [0.6]}, "sampling must be an object"),
            ({"sampling": {"temperature": 0.6}}, "sampling may hold only"),
            ({"sampling": {"temp": "0.6"}}, "finite number"),
            ({"sampling": {"temp": True}}, "finite number"),
            ({"sampling": {"temp": None}}, "finite number"),
            ({"sampling": {"temp": -0.1}}, "temp must be >= 0"),
            ({"sampling": {"top_p": 1.5}}, "top_p must be between 0 and 1"),
            ({"sampling": {"top_k": 2.5}}, "top_k must be an integer"),
            ({"sampling": {"top_k": -1}}, "top_k must be an integer"),
            ({"kv_type": "q3"}, "kv_type must be one of"),
            ({"kv_type": 16}, "kv_type must be one of"),
            ({"kv_type": ""}, "kv_type must be one of"),
            ({"reasoning": "maybe"}, "reasoning must be one of the strings off, on, auto"),
            ({"reasoning": "OFF"}, "reasoning must be one of"),
            ({"reasoning": "auto "}, "reasoning must be one of"),
            ({"reasoning": ""}, "reasoning must be one of"),
            ({"reasoning": True}, "reasoning must be one of"),
            ({"reasoning": False}, "reasoning must be one of"),
            ({"reasoning": None}, "reasoning must be one of"),
            ({"reasoning": 0}, "reasoning must be one of"),
            ({"reasoning": ["auto"]}, "reasoning must be one of"),
            ({"reasoning": {"mode": "auto"}}, "reasoning must be one of"),
            ({"chat_template": 5}, "chat_template must be a non-empty"),
            ({"chat_template": ""}, "chat_template must be a non-empty"),
            ({"chat_template": None}, "chat_template must be a non-empty"),
            ({"chat_template": True}, "chat_template must be a non-empty"),
            ({"chat_template": ["config/chat-templates/t.jinja"]}, "chat_template must be a non-empty"),
            ({"chat_template": "config/chat-templates/t.jinja\n--host"}, "chat_template must be a non-empty"),
            ({"chat_template": "config/chat-templates/t.jinja\t"}, "chat_template must be a non-empty"),
            ({"sever_args": []}, "unknown field(s) sever_args"),
            ({"reasonning": "auto"}, "unknown field(s) reasonning"),
            ({"chat-template": "config/chat-templates/t.jinja"}, "unknown field(s) chat-template"),
            ({"default": "yes"}, "default must be true or false"),
            ({"repo": 5}, "repo must be a string"),
        ]
        for change, msg in cases:
            with self.subTest(change=change):
                self.t.entry = dict(base, **change)
                self.t.write_manifest()
                r = self.dry()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertEqual(r.stdout, "")
                self.assertIn(msg, r.stderr)
                self.assertIn("entry 'fake'", r.stderr, "the message names the entry")

    def test_non_finite_sampling_refused(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                text = json.dumps({"about": "t", "files": [dict(self.t.entry, sampling={"temp": 0})]})
                (self.t.root / "models" / "manifest.json").write_text(text.replace('"temp": 0', '"temp": ' + literal))
                r = self.dry()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("non-finite", r.stderr)

    # ------------------------------------------------------------ paths
    def test_symlinked_models_dir_refused(self):
        real = self.t.root / "elsewhere-models"
        (self.t.root / "models").rename(real)
        (self.t.root / "models").symlink_to(real)
        for args in ((), ("--dry-run",)):
            with self.subTest(args=args):
                r = self.t.run(*args)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("outside %s/models" % self.t.root, r.stderr)
                self.assertFalse(self.t.record.exists())

    def test_symlinked_model_file_refused(self):
        real = self.t.root / "models" / "fake" / "real.gguf"
        self.t.model.rename(real)
        self.t.model.symlink_to(real)
        r = self.dry()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("is a symlink", r.stderr)

    def test_symlinked_subdir_leading_out_refused(self):
        outside = self.t.root / "outside"
        (self.t.root / "models" / "fake").rename(outside)
        (self.t.root / "models" / "fake").symlink_to(outside)
        r = self.dry()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("resolves to %s/fake.gguf" % outside, r.stderr)

    def test_symlinked_subdir_inside_models_allowed(self):
        inner = self.t.root / "models" / "store"
        (self.t.root / "models" / "fake").rename(inner)
        (self.t.root / "models" / "fake").symlink_to(inner)
        r = self.t.run()
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_template_paths(self):
        tpl = self.t.root / "config" / "chat-templates" / "t.jinja"
        (self.t.root / "models" / "x.jinja").write_text("{{ x }}\n")
        cases = [("models/x.jinja", "outside %s/config" % self.t.root)]
        link = self.t.root / "config" / "link.jinja"
        link.symlink_to(tpl)
        cases.append(("config/link.jinja", "is a symlink"))
        out = self.t.root / "tpl-out"
        out.mkdir()
        (out / "t.jinja").write_text("{{ y }}\n")
        (self.t.root / "config" / "out").symlink_to(out)
        cases.append(("config/out/t.jinja", "outside %s/config" % self.t.root))
        for value, msg in cases:
            with self.subTest(CHAT_TEMPLATE=value):
                self.t.conf.update(CHAT_TEMPLATE=value)
                self.t.write_conf()
                r = self.dry()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(msg, r.stderr)

    # ------------------------------------------------------------ environment
    BAD_ENV = {"LLAMA_ARG_HOST": "0.0.0.0", "LLAMA_ARG_PORT": "1", "LLAMA_ARG_SOME_NEW_FLAG": "1",
               "LLAMA_API_KEY": "leak", "LLAMA_ARG_MCP_SERVERS_JSON": "{}", "LLAMA_CACHE": "/tmp/x",
               "GGML_BACKEND_PATH": "/tmp/evil.dylib", "GGML_METAL_PATH_RESOURCES": "/tmp",
               "HF_TOKEN": "t", "HF_ENDPOINT": "http://x", "HUGGING_FACE_HUB_TOKEN": "t", "MODEL_ENDPOINT": "http://x",
               "http_proxy": "http://x", "HTTPS_PROXY": "http://x", "all_proxy": "socks5://x", "NO_PROXY": "*",
               "PERL5OPT": "-Mstrict", "PERL5LIB": "/tmp",
               "MTL_DEBUG_LAYER": "1", "MTL_CAPTURE_ENABLED": "1", "MTL_LOG_TO_STDERR": "1",
               "MallocGuardEdges": "1", "MallocCheckHeapSleep": "0"}

    def test_env_cleared_dry_run(self):
        r = self.dry(**self.BAD_ENV, KEEP_ME="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = re.search(r"cleared (\d+) inherited environment variables \(([^)]*)\)", r.stderr)
        self.assertTrue(m, r.stderr)
        self.assertEqual(int(m.group(1)), len(self.BAD_ENV))
        self.assertEqual(set(m.group(2).split(", ")), set(self.BAD_ENV))

    def test_env_cleared_at_launch(self):
        r = self.t.run(**self.BAD_ENV, KEEP_ME="1", DYLD_INSERT_LIBRARIES="/tmp/evil.dylib",
                       DYLD_LIBRARY_PATH="/tmp/nonexistent")
        self.assertEqual(r.returncode, 0, r.stderr)
        env = self.t.rec()["env"]
        self.assertEqual(env, {"KEEP_ME": "1"}, "only the control variable reaches llama-server")
        # (DYLD_* are already stripped by SIP when /bin/bash starts; this checks the end
        # result, not that serve.sh removed them.)

    def test_inherited_xtrace_does_not_print_the_key(self):
        """bash imports SHELLOPTS from the environment, so xtrace would be on from the
        first line; set +xv on line 2 turns it off before anything else runs."""
        for opts in ("xtrace", "verbose", "xtrace:verbose"):
            with self.subTest(SHELLOPTS=opts):
                r = self.t.run(SHELLOPTS=opts, BASH_XTRACEFD="2")
                self.assertEqual(r.returncode, 0, r.stderr)
                key = self.t.rec()["key"].strip()
                self.assertRegex(key, r"^[0-9a-f]{64}$")
                self.assertNotIn(key, r.stderr)
                self.assertNotIn(key, r.stdout)
                traced = [l for l in r.stderr.splitlines() if l.startswith("+")]
                self.assertLessEqual(set(traced), {"+ set +xv"}, traced)
                self.assertNotIn("xtrace", self.t.rec()["env"].get("SHELLOPTS", ""))
                self.assertNotIn("verbose", self.t.rec()["env"].get("SHELLOPTS", ""))
                time.sleep(1.1)  # distinct log stamp

    def test_env_name_that_cannot_be_unset_refuses(self):
        r = self.dry(**{"LLAMA_ARG.HOST": "0.0.0.0"})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("survived clearing", r.stderr)

    # ------------------------------------------------------------ key file
    def test_key_file_mode_and_rotation(self):
        r = self.t.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = self.t.rec()
        self.assertEqual(rec["key_mode"], 0o600, "key file mode when llama-server starts")
        self.assertEqual(rec["dir_mode"], 0o700, "logs/ mode when llama-server starts")
        self.assertRegex(rec["key"], r"^[0-9a-f]{64}\n$")
        key1 = rec["key"]
        self.assertNotIn(key1.strip(), " ".join(rec["argv"]))
        for log in self.t.logs.glob("server-*.log"):
            self.assertNotIn(key1.strip(), log.read_text())
            self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.t.logs / "api-key").stat().st_mode), 0o600)
        time.sleep(1.1)  # distinct log stamp
        r = self.t.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotEqual(self.t.rec()["key"], key1, "a fresh key per start")

    def test_key_symlink_is_replaced_not_followed(self):
        self.t.logs.mkdir(mode=0o755)
        victim = self.t.root / "victim.txt"
        victim.write_text("untouched\n")
        (self.t.logs / "api-key").symlink_to(victim)
        r = self.t.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(victim.read_text(), "untouched\n")
        self.assertFalse((self.t.logs / "api-key").is_symlink())
        self.assertEqual(stat.S_IMODE(self.t.logs.stat().st_mode), 0o700, "logs/ tightened to 700")

    def test_logs_symlink_refused(self):
        other = self.t.root / "elsewhere"
        other.mkdir()
        self.t.logs.symlink_to(other)
        r = self.t.run()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("symlink", r.stderr)
        self.assertEqual(list(other.iterdir()), [])

    # ------------------------------------------------------------ log file
    def test_log_first_line_is_command_line(self):
        cmd = self.dry().stdout.strip()
        r = self.t.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        logs = sorted(self.t.logs.glob("server-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertRegex(logs[0].name, r"^server-\d{8}T\d{6}Z(-\d+)?\.log$")
        text = logs[0].read_text().splitlines()
        self.assertEqual(text[0], cmd)
        self.assertTrue(any(l.startswith("serve.sh: memory free: ") for l in text), text)
        self.assertEqual(text[-1], "serve.sh: llama-server exited 0")

    def test_memory_report_has_numbers(self):
        r = self.t.run()
        m = re.search(r"memory free: (\d+) MiB available .* of (\d+) MiB", r.stderr)
        self.assertTrue(m, r.stderr)
        self.assertGreater(int(m.group(2)), int(m.group(1)))

    # ------------------------------------------------------------ lock
    def wait_pidfile(self, proc, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            p = self.t.pidfile()
            if p.is_file() and p.read_text().strip().isdigit():
                return int(p.read_text().strip())
            if proc is not None and proc.poll() is not None:
                self.fail("serve.sh exited early: %s" % (proc.communicate(),))
            time.sleep(0.1)
        self.fail("no pid file")

    def test_lock_one_server_per_checkout(self):
        first = self.t.popen(FAKE_MODE="sleep", FAKE_SLEEP="30")
        try:
            child = self.wait_pidfile(first)
            self.assertTrue((self.t.logs / "serve.lock").is_dir())
            r = self.t.run(FAKE_RECORD=str(self.t.root / "second.json"))
            self.assertEqual(r.returncode, 1)
            self.assertIn("another server holds", r.stderr)
            self.assertFalse((self.t.root / "second.json").exists(), "second llama-server must not start")
            r = self.t.run("--detach", FAKE_RECORD=str(self.t.root / "second.json"))
            self.assertEqual(r.returncode, 1)
            self.assertEqual(self.dry().returncode, 0, "a dry run takes no lock")
            first.send_signal(signal.SIGTERM)
            out, err = first.communicate(timeout=20)
            self.assertEqual(first.returncode, 128 + signal.SIGTERM, err)
            self.assertFalse(alive(child), "SIGTERM is forwarded to llama-server")
            self.assertFalse((self.t.logs / "serve.lock").exists(), "lock released on exit")
            self.assertFalse(self.t.pidfile().exists(), "pid file removed on exit")
        finally:
            if first.poll() is None:
                first.kill()
                first.communicate()

    def test_foreground_returns_child_status_after_forwarded_signal(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig.name):
                first = self.t.popen(FAKE_MODE="sleep", FAKE_SLEEP="30", FAKE_TERM_EXIT="7")
                try:
                    child = self.wait_pidfile(first)
                    time.sleep(0.5)  # the fake has installed its SIGTERM handler
                    first.send_signal(sig)
                    out, err = first.communicate(timeout=20)
                    self.assertEqual(first.returncode, 7, "the child's own status, not 128+%d" % sig)
                    self.assertFalse(alive(child))
                    log = sorted(self.t.logs.glob("server-*.log"))[-1].read_text().splitlines()
                    self.assertEqual(log[-1], "serve.sh: llama-server exited 7")
                finally:
                    if first.poll() is None:
                        first.kill()
                        first.communicate()
                time.sleep(1.1)  # distinct log stamp

    def test_stale_lock_reclaimed(self):
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        lock = self.t.logs / "serve.lock"
        lock.mkdir(parents=True)
        (lock / "owner").write_text("%d\n" % dead.pid)
        self.t.pidfile().write_text("%d\n" % dead.pid)
        old = time.time() - 60
        os.utime(lock, (old, old))
        r = self.t.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("removing stale lock", r.stderr)
        self.assertFalse(lock.exists())
        self.assertEqual(sorted(p.name for p in self.t.logs.glob("serve.lock*")), [], "no moved-aside lock is left")

    def test_lock_with_live_owner_refused(self):
        holder = subprocess.Popen(["/bin/bash", "-c", "read x"], stdin=subprocess.PIPE)
        try:
            lock = self.t.logs / "serve.lock"
            lock.mkdir(parents=True)
            (lock / "owner").write_text("%d\n" % holder.pid)
            r = self.t.run()
            self.assertEqual(r.returncode, 1)
            self.assertIn("serve.sh pid %d" % holder.pid, r.stderr)
            self.assertTrue(lock.is_dir(), "a live lock is left alone")
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)

    def sleeper(self, how):
        """A live process that ps names llama-server: /bin/sleep via a symlink of that
        name ("symlink") or via exec -a ("exec-a")."""
        if how == "symlink":
            d = self.t.root / "other-bin"
            d.mkdir(exist_ok=True)
            link = d / "llama-server"
            if not link.exists():
                os.symlink("/bin/sleep", link)
            argv = [str(link), "60"]
        else:
            argv = ["/bin/bash", "-c", "exec -a llama-server /bin/sleep 60"]
        p = subprocess.Popen(argv)
        self.addCleanup(lambda: (p.poll() is None and p.terminate(), p.wait(timeout=10)))
        for _ in range(50):
            out = subprocess.run(["ps", "-p", str(p.pid), "-o", "comm="], capture_output=True, text=True).stdout
            if os.path.basename(out.strip()) == "llama-server" and exe_of(p.pid) == "/bin/sleep":
                return p
            time.sleep(0.05)
        self.fail("fake llama-server did not start")

    def test_lock_held_by_detached_server(self):
        p = self.sleeper("symlink")
        dead = subprocess.Popen(["/usr/bin/true"])
        dead.wait()
        lock = self.t.logs / "serve.lock"
        lock.mkdir(parents=True)
        (lock / "owner").write_text("%d\n" % dead.pid)  # the detaching serve.sh is gone
        self.t.pidfile().write_text("%d\n" % p.pid)
        r = self.t.run(LIS_TEST_LLAMA_EXE="/bin/sleep")
        self.assertEqual(r.returncode, 1)
        self.assertIn("llama-server pid %d" % p.pid, r.stderr)
        self.assertTrue(alive(p.pid))
        self.assertEqual(self.t.pidfile().read_text(), "%d\n" % p.pid)

    def test_live_pidfile_without_lock_refused(self):
        p = self.sleeper("symlink")
        self.t.logs.mkdir()
        self.t.pidfile().write_text("%d\n" % p.pid)
        r = self.t.run(LIS_TEST_LLAMA_EXE="/bin/sleep")
        self.assertEqual(r.returncode, 1)
        self.assertIn("still running", r.stderr)
        self.assertFalse(self.t.record.exists())
        self.assertFalse((self.t.logs / "serve.lock").exists(), "lock released after the refusal")

    def test_process_named_llama_server_running_another_binary_is_not_a_server(self):
        """comm can be spoofed (exec -a, a symlink's name): only the executable file counts."""
        for how in ("exec-a", "symlink"):
            with self.subTest(how=how):
                p = self.sleeper(how)
                dead = subprocess.Popen(["/usr/bin/true"])
                dead.wait()
                lock = self.t.logs / "serve.lock"
                lock.mkdir(parents=True)
                (lock / "owner").write_text("%d\n" % dead.pid)
                old = time.time() - 60
                os.utime(lock, (old, old))
                self.t.pidfile().write_text("%d\n" % p.pid)
                r = self.t.run()  # expected executable: the fake's own path, not /bin/sleep
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("removing stale lock", r.stderr)
                self.assertIn("pid file named pid %d, which is not a running" % p.pid, r.stderr)
                self.assertTrue(alive(p.pid), "serve.sh never signals the recorded process")
                p.terminate()
                p.wait(timeout=10)
                time.sleep(1.1)  # distinct log stamp

    def test_fresh_lock_without_owner_refused(self):
        (self.t.logs / "serve.lock").mkdir(parents=True)
        r = self.t.run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("starting now", r.stderr)

    def test_port_in_use_refused(self):
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", self.t.port))
            s.listen(1)
            r = self.t.run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("already in use", r.stderr)
        self.assertFalse(self.t.record.exists())
        self.assertFalse((self.t.logs / "serve.lock").exists(), "lock released after a refusal")

    # ------------------------------------------------------------ model check
    def test_sha_mismatch_refused_before_key(self):
        self.t.entry["sha256"] = "0" * 64
        self.t.write_manifest()
        r = self.t.run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("sha256", r.stderr)
        self.assertFalse((self.t.logs / "api-key").exists())
        self.assertFalse(self.t.record.exists())

    def test_size_mismatch_refused(self):
        self.t.entry["size"] += 1
        self.t.write_manifest()
        r = self.t.run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("bytes", r.stderr)

    def test_missing_model_refused_on_real_start(self):
        self.t.model.unlink()
        r = self.t.run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("model file not found", r.stderr)

    def test_hash_once_per_boot(self):
        r = self.t.run()
        self.assertIn("model sha256 matches", r.stderr)
        r = self.t.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("verified earlier this boot", r.stderr)
        old = time.time() - 120
        os.utime(self.t.model, (old, old))
        r = self.t.run()
        self.assertIn("model sha256 matches", r.stderr, "an mtime change rehashes")
        self.t.model.write_bytes(b"GGUF tampered payload!!\n")  # same size, new content, new mtime
        r = self.t.run()
        self.assertEqual(r.returncode, 1, "a rewritten file (new mtime) is rehashed and refused")
        self.assertIn("sha256", r.stderr)

    # ------------------------------------------------------------ config
    def test_config_errors(self):
        cases = [
            ({"MODEL_ID": ""}, "MODEL_ID is empty"),
            ({"MODEL_ID": "a b"}, "MODEL_ID"),
            ({"MODEL_ID": "other"}, "0 entries"),
            ({"PORT": "0"}, "PORT"),
            ({"PORT": "abc"}, "PORT"),
            ({"PORT": "99999"}, "PORT"),
            ({"PORT": "08080"}, "PORT"),
            ({"CTX": "-1"}, "CTX"),
            ({"KV_TYPE": "q3"}, "KV_TYPE"),
            ({"CHAT_TEMPLATE": "../x.jinja"}, "CHAT_TEMPLATE"),
            ({"CHAT_TEMPLATE": "/etc/hosts"}, "CHAT_TEMPLATE"),
            ({"CHAT_TEMPLATE": "config/missing.jinja"}, "not found"),
            ({"CHAT_TEMPLATE": "config/$(id).jinja"}, "CHAT_TEMPLATE"),
        ]
        base = dict(self.t.conf)
        for change, msg in cases:
            with self.subTest(change=change):
                self.t.conf = dict(base, **change)
                self.t.write_conf()
                r = self.dry()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(msg, r.stderr)
        self.t.conf = base
        self.t.write_conf(["PORT=9"])
        r = self.dry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("set twice", r.stderr)
        self.t.write_conf(["FUTURE_KEY=1"])
        r = self.dry()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("unknown key FUTURE_KEY", r.stderr)

    def test_config_is_not_sourced(self):
        canary = self.t.root / "sourced"
        self.t.write_conf(["X_HOOK=$(touch %s)" % canary, "touch %s" % canary])
        r = self.dry()
        self.assertEqual(r.returncode, 1)
        self.assertFalse(canary.exists())

    def test_manifest_errors(self):
        bad = [
            dict(self.t.entry, path="/abs/fake.gguf"),
            dict(self.t.entry, path="models/../fake.gguf"),
            dict(self.t.entry, path="elsewhere/fake.gguf"),
            dict(self.t.entry, path="models/fake/fake.bin"),
            dict(self.t.entry, size="24"),
            dict(self.t.entry, sha256="ABC"),
        ]
        for entry in bad:
            with self.subTest(entry=entry):
                self.t.write_manifest([entry])
                self.assertEqual(self.dry().returncode, 1)
        self.t.write_manifest([self.t.entry, dict(self.t.entry)])
        r = self.dry()
        self.assertEqual(r.returncode, 1)
        self.assertIn("2 entries", r.stderr)

    # ------------------------------------------------------------ modes
    def test_foreground_exit_code_and_cleanup(self):
        r = self.t.run(FAKE_EXIT="5")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertFalse(self.t.pidfile().exists())
        self.assertFalse((self.t.logs / "serve.lock").exists())
        self.assertEqual(self.t.rec()["cwd"], str(self.t.root))

    def test_detach_healthy(self):
        r = self.t.run("--detach", "--wait", "30", FAKE_MODE="serve")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("healthy on 127.0.0.1:%d" % self.t.port, r.stderr)
        pid = int(self.t.pidfile().read_text())
        self.assertTrue(alive(pid))
        self.assertTrue((self.t.logs / "serve.lock").is_dir(), "lock kept while the server runs")
        r2 = self.t.run(FAKE_RECORD=str(self.t.root / "second.json"))
        self.assertEqual(r2.returncode, 1, "a second start is refused while the detached server runs")
        # The Python fake is not a process the check accepts as llama-server (framework
        # Python re-execs as Python.app, so its comm is "Python"), so here the port check
        # refuses; test_lock_held_by_detached_server covers the pid-file path.
        self.assertIn("already in use", r2.stderr)
        self.assertFalse((self.t.root / "second.json").exists())
        end(pid)

    def test_detach_server_exits(self):
        r = self.t.run("--detach", "--wait", "30", FAKE_MODE="exit", FAKE_EXIT="3")
        self.assertEqual(r.returncode, 1)
        self.assertIn("exited 3", r.stderr)
        self.assertIn("last lines of", r.stderr)
        self.assertFalse(self.t.pidfile().exists())
        self.assertFalse((self.t.logs / "serve.lock").exists())

    def test_detach_timeout(self):
        r = self.t.run("--detach", "--wait", "2", FAKE_MODE="sleep", FAKE_SLEEP="30")
        self.assertEqual(r.returncode, 1)
        self.assertIn("did not answer within 2 s", r.stderr)
        self.assertTrue(alive(int(self.t.pidfile().read_text())), "a slow server is left running")

    def test_detach_signal_during_health_wait_leaves_no_server(self):
        """A stub that never answers /health; serve.sh is signalled through this test's own
        subprocess handle (one pid). The child must be stopped and reaped, the pid file and
        lock removed, and serve.sh must exit 128+N saying so."""
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(signal=sig.name):
                proc = self.t.popen("--detach", "--wait", "120", FAKE_MODE="sleep", FAKE_SLEEP="120")
                child = None
                try:
                    child = self.wait_pidfile(proc)
                    self.assertTrue(alive(child))
                    time.sleep(0.5)
                    proc.send_signal(sig)
                    out, err = proc.communicate(timeout=60)
                    self.assertEqual(proc.returncode, 128 + sig, err)
                    self.assertIn("start interrupted by %s" % sig.name, err)
                    self.assertIn("no server left running", err)
                    self.assertFalse(alive(child), "the child does not outlive an interrupted start")
                    self.assertFalse(self.t.pidfile().exists(), "pid file removed")
                    self.assertFalse((self.t.logs / "serve.lock").exists(), "lock removed")
                    log = sorted(self.t.logs.glob("server-*.log"))[-1].read_text().splitlines()
                    self.assertEqual(log[-1], "serve.sh: start interrupted by %s; llama-server stopped" % sig.name)
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()
                    end(child)
                time.sleep(1.1)  # distinct log stamp

    def test_pid_file_write_failure_stops_the_child(self):
        """A directory where the pid file goes makes the write fail after the launch."""
        for mode in ("--detach", None):
            with self.subTest(mode=mode or "foreground"):
                self.t.logs.mkdir(exist_ok=True)
                self.t.pidfile().mkdir(exist_ok=True)
                args = [mode, "--wait", "30"] if mode else []
                r = self.t.run(*args, FAKE_MODE="sleep", FAKE_SLEEP="30")
                self.assertEqual(r.returncode, 1, r.stderr)
                m = re.search(r"cannot write .*llama-server\.pid; llama-server pid (\d+) was stopped", r.stderr)
                self.assertTrue(m, r.stderr)
                self.assertFalse(alive(int(m.group(1))), "the child does not outlive the failed start")
                self.assertFalse((self.t.logs / "serve.lock").exists())
                time.sleep(1.1)  # distinct log stamp

    def test_bad_arguments(self):
        for args in (["--bogus"], ["--detach", "--dry-run"], ["--wait", "x", "--dry-run"],
                     ["--reasoning", "maybe", "--dry-run"], ["--reasoning", "", "--dry-run"],
                     ["--reasoning", "OFF"], ["--dry-run", "--reasoning"]):
            with self.subTest(args=args):
                r = self.t.run(*args)
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(r.stdout, "")
                self.assertFalse(self.t.record.exists())
        self.entry_with(reasoning="auto")
        r = self.t.run("--dry-run", "--reasoning", "maybe")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--reasoning must be off, on or auto", r.stderr, "a bad flag is not replaced by the entry's value")
        r = self.t.run("--help")
        self.assertEqual(r.returncode, 2)
        self.assertIn("else the manifest entry's \"reasoning\", else off", r.stderr)


class StaticTest(unittest.TestCase):
    def test_only_one_validated_kill(self):
        text = SERVE.read_text()
        kills = [l.strip() for l in text.splitlines() if re.search(r"\bkill\b", l) and not l.strip().startswith("#")]
        self.assertEqual(kills, ['if [[ $CHILD =~ ^[1-9][0-9]*$ ]] && [ "$CHILD" -gt 1 ]; then kill -TERM "$CHILD" 2>/dev/null; fi'])
        for bad in ("pkill", "killall", "kill 0", "kill -1", "kill -- -", "sudo", "| sh", "| bash", "curl -o", "wget"):
            self.assertNotIn(bad, text)

    def test_no_tool_from_path(self):
        code = [l for l in SERVE.read_text().splitlines() if not l.strip().startswith("#")]
        lookups = re.compile(r"(^|[;&|(`]|\$\()\s*(command\s+-v|which|type\s+-P|hash)\s")
        self.assertEqual([l for l in code if lookups.search(l)], [])
        self.assertIn("PATH=/usr/bin:/bin:/usr/sbin:/sbin", code)

    def test_allow_list_pinned(self):
        text = SERVE.read_text()
        block = text.split("# ---- ALLOWED_FLAGS (begin) ----")[1].split("# ---- ALLOWED_FLAGS (end) ----")[0]
        found = {}
        for alts, kind in re.findall(r"^\s+([-a-z|]+)\) echo ([a-z-]+) ;;$", block, re.M):
            found.setdefault(kind, []).extend(alts.split("|"))
        self.assertEqual({k: sorted(v) for k, v in found.items()}, {k: sorted(v) for k, v in ALLOW_LIST.items()})
        arms = re.findall(r"^\s+(\S+)\)", block, re.M)
        self.assertEqual(arms[-1], "*", "anything else is refused")

    def test_allow_list_disjoint_from_refusal_lists(self):
        text = SERVE.read_text()
        fixed = re.search(r"^FIXED_FLAG_RE='(.*)'$", text, re.M).group(1)
        mac = re.search(r"^MAC_FLAG_RE='(.*)'$", text, re.M).group(1)
        for flags in ALLOW_LIST.values():
            for f in flags:
                self.assertIsNone(re.search(fixed, f.lower()), f)
                self.assertIsNone(re.search(mac, f.lower()), f)

    def test_refusal_regex_matches_reference_list(self):
        text = SERVE.read_text()
        m = re.search(r"^FIXED_FLAG_RE='(.*)'$", text, re.M)
        self.assertTrue(m)
        names = re.match(r"\^\(--\(([^)]*)\)\|-ag\|-hft\)\(=\|\$\)$", m.group(1))
        self.assertTrue(names, m.group(1))
        listed = ["--" + n for n in names.group(1).split("|")] + ["-ag", "-hft"]
        self.assertEqual(listed, REFUSED, "the ported serve.ps1 block stays verbatim")

    def test_mac_additions_block_pinned(self):
        text = SERVE.read_text()
        m = re.search(r"^MAC_FLAG_RE='(.*)'$", text, re.M)
        self.assertTrue(m)
        parts = re.match(r"\^\(--\(([^)]*)\)\|-\(([^)]*)\)\)\(=\|\$\)$", m.group(1))
        self.assertTrue(parts, m.group(1))
        longs = ["--" + n.replace("\\.", ".") for n in parts.group(1).split("|")]
        shorts = ["-" + n for n in parts.group(2).split("|")]
        self.assertEqual(sorted(longs + shorts), sorted(MAC_REFUSED))
        self.assertIn("# macOS build additions (NOT in serve.ps1)", text)
        self.assertEqual(set(REFUSED) & set(MAC_REFUSED), set(), "the two blocks do not overlap")

    def test_one_template_check_for_both_sources(self):
        """CHAT_TEMPLATE and a manifest entry's chat_template go through the same function."""
        code = "\n".join(l for l in SERVE.read_text().splitlines() if not l.strip().startswith("#"))
        self.assertEqual(code.count("check_template() {"), 1)
        calls = re.findall(r'check_template "([^"]*)" "(\$[A-Z_]+)"', code)
        self.assertEqual(calls, [("CHAT_TEMPLATE in $CONF", "$CFG_TEMPLATE"),
                                 ("chat_template of manifest entry $CFG_MODEL_ID", "$M_TEMPLATE")])
        self.assertEqual(code.count("--chat-template-file"), 1)
        self.assertEqual(code.count("re_rel="), 1)

    def test_xtrace_off_first(self):
        for script in (SERVE, REPO / "scripts" / "stop.sh"):
            lines = script.read_text().splitlines()
            self.assertEqual(lines[0], "#!/bin/bash")
            self.assertTrue(lines[1].startswith("set +xv  #"), (script.name, lines[1]))

    def test_offline_is_a_fixed_flag(self):
        text = SERVE.read_text()
        self.assertIn('CMD=("$LLAMA"\n    --offline\n    -m "$MODEL"\n', text)
        self.assertEqual(len(re.findall(r"^\s+--offline$", text, re.M)), 1)
        self.assertNotIn("--offline", sum(ALLOW_LIST.values(), []))

    def test_detach_traps_signals_before_launch(self):
        text = SERVE.read_text()
        det = text.split('if [ "$MODE" = detach ]; then')[1].split("\n# Foreground.")[0]
        for sig in ("INT", "TERM", "HUP"):
            self.assertIn("trap 'DETACH_SIG=${DETACH_SIG:-%s}' %s" % (sig, sig), det)
        self.assertLess(det.index("trap 'DETACH_SIG"), det.index('nohup "${CMD[@]}"'))
        self.assertGreaterEqual(det.count('[ -n "$DETACH_SIG" ] && abort_start'), 3)
        abort = det.split("abort_start() {")[1].split("\n    }\n")[0]
        self.assertLess(abort.index("stop_child"), abort.index("exit $((128 + n))"))

    def test_stale_lock_moved_aside_atomically(self):
        """The identity of the lock is captured BEFORE the liveness check; the stale lock is
        renamed (one atomic mv), the moved directory is compared with that captured
        identity, then deleted; serve.sh never deletes serve.lock in place except in
        release()."""
        text = SERVE.read_text()
        take = text.split("take_lock() {")[1].split("\n}\n")[0]
        capture = 'id=$(lock_id "$LOCKDIR")'
        self.assertEqual(take.count(capture), 1)
        self.assertEqual(take.count("lock_live"), 1)
        self.assertLess(take.index(capture), take.index("holder=$(lock_live)"), "identity read before the liveness check")
        self.assertLess(take.index("holder=$(lock_live)"), take.index('mv "$LOCKDIR" "$grave"'))
        self.assertEqual(take.count("id="), 1, "the identity is read once, not re-read after the check")
        self.assertIn('mv "$LOCKDIR" "$grave"', take)
        self.assertIn('[ "$(lock_id "$grave")" != "$id" ]', take)
        self.assertLess(take.index('mv "$LOCKDIR" "$grave"'), take.index('[ "$(lock_id "$grave")" != "$id" ]'))
        self.assertIn('rm -rf "$grave"', take)
        self.assertLess(take.index('mv "$LOCKDIR" "$grave"'), take.index('rm -rf "$grave"'))
        self.assertEqual(text.count('rm -rf "$LOCKDIR"'), 1)
        self.assertIn('rm -rf "$LOCKDIR"; fi', text.split("release() {")[1].split("\n}\n")[0])


if __name__ == "__main__":
    unittest.main(verbosity=1)
