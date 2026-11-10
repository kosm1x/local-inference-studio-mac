"""Tests for scripts/stop.sh.

Run from the repo root: python3.13 -I scripts/tests/stop_test.py

Each test copies stop.sh into a temp checkout and writes logs/llama-server.pid by hand.
stop.sh must refuse every pid file that is not a positive pid > 1 of a live process whose
comm is llama-server AND whose executable file (lsof txt) is the expected llama-server,
and must leave such a process running. The positive cases run /bin/sleep through a
symlink named llama-server, with the test-only override LIS_TEST_LLAMA_SERVER naming
that symlink (so the expected executable is /bin/sleep); no real server or model is
needed. The spoof cases expect another executable, or the installed llama-server.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STOP = REPO / "scripts" / "stop.sh"
PY = shutil.which("python3.13") or sys.executable
INSTALLED = next((p for p in ("/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server") if os.path.exists(p)), None)


def alive(pid):
    out = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip()
    return bool(out) and "Z" not in out


def exe_of(pid):
    out = subprocess.run(["/usr/sbin/lsof", "-w", "-a", "-p", str(pid), "-d", "txt", "-Fn"],
                         capture_output=True, text=True).stdout
    return next((l[1:] for l in out.splitlines() if l.startswith("n")), "")


class StopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        (self.root / "scripts").mkdir()
        shutil.copy2(STOP, self.root / "scripts" / "stop.sh")
        self.logs = self.root / "logs"
        self.logs.mkdir(mode=0o700)
        self.pidfile = self.logs / "llama-server.pid"
        (self.root / "bin").mkdir()
        # /bin/sleep run as llama-server: a symlink, since macOS ps reports the invoked path
        # as comm (measured 2026-11-10). A plain copy of the platform binary is killed at
        # exec by code signing, which would leave a zombie and make these tests vacuous.
        self.fake = self.root / "bin" / "llama-server"
        os.symlink("/bin/sleep", self.fake)
        # Another program's stand-in, for "expected executable is something else".
        self.other = self.root / "other" / "llama-server"
        self.other.parent.mkdir()
        os.symlink("/bin/cat", self.other)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=10)
        self.tmp.cleanup()

    def spawn(self, argv, comm=None):
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        want = comm or os.path.basename(argv[0])
        for _ in range(50):  # until exec has happened and ps names the program
            out = subprocess.run(["ps", "-p", str(p.pid), "-o", "comm="], capture_output=True, text=True).stdout
            if os.path.basename(out.strip()) == want:
                break
            time.sleep(0.05)
        return p

    def stop(self, **env):
        e = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LIS_TEST_LLAMA_SERVER": str(self.fake)}
        e.update(env)
        e = {k: v for k, v in e.items() if v is not None}
        return subprocess.run(["/bin/bash", str(self.root / "scripts" / "stop.sh")], capture_output=True, text=True,
                              timeout=60, cwd="/", env=e)

    def assert_refused(self, content, reason=None, **env):
        """reason pins WHICH guard refused, so each guard is tested on its own, not only
        the layered whole."""
        self.pidfile.write_text(content)
        r = self.stop(**env)
        self.assertEqual(r.returncode, 1, (content, r.stderr))
        self.assertIn("refusing", r.stderr)
        if reason:
            self.assertIn(reason, r.stderr)
        self.assertEqual(self.pidfile.read_text(), content, "a refused pid file is left as it is")
        return r

    def test_garbage_refused(self):
        for content in ("garbage\n", "12abc\n", "$(id)\n", "12 34\n", " 123\n", "0x1f\n", "+5\n"):
            with self.subTest(content=content):
                self.assert_refused(content, "not a pid")

    def test_pid_one_refused(self):
        self.assert_refused("1\n", "holds pid 1")

    def test_negative_refused(self):
        self.assert_refused("-1\n", "not a pid")
        self.assert_refused("-0\n", "not a pid")

    def test_empty_refused(self):
        self.assert_refused("", "not a pid")
        self.assert_refused("\n", "not a pid")

    def test_zero_and_leading_zero_refused(self):
        self.assert_refused("0\n", "leading zero")
        self.assert_refused("007\n", "leading zero")

    def test_huge_refused(self):
        self.assert_refused("99999999999999999999\n", "too long")

    def test_live_non_llama_process_refused(self):
        for argv in ([PY, "-I", "-c", "import time; time.sleep(60)"], ["/bin/sleep", "60"]):
            with self.subTest(argv=argv[0]):
                p = self.spawn(argv)
                r = self.assert_refused("%d\n" % p.pid)
                self.assertIn("not llama-server", r.stderr)
                time.sleep(0.3)
                self.assertTrue(alive(p.pid), "the non-llama process must still run")

    def test_own_shell_refused(self):
        r = self.assert_refused("%d\n" % os.getpid())
        self.assertIn("not llama-server", r.stderr)

    def test_exec_a_spoof_refused(self):
        """comm says llama-server, the executable is /bin/sleep."""
        p = self.spawn(["/bin/bash", "-c", "exec -a llama-server /bin/sleep 60"], comm="llama-server")
        comm = subprocess.run(["ps", "-p", str(p.pid), "-o", "comm="], capture_output=True, text=True).stdout.strip()
        self.assertEqual(comm, "llama-server", "the spoof passes the comm check")
        self.assertEqual(exe_of(p.pid), "/bin/sleep")
        r = self.assert_refused("%d\n" % p.pid, "runs /bin/sleep, not the installed llama-server",
                                LIS_TEST_LLAMA_SERVER=str(self.other))
        self.assertIn(os.path.realpath(self.other), r.stderr)
        if INSTALLED:
            r = self.assert_refused("%d\n" % p.pid, "runs /bin/sleep, not the installed llama-server %s"
                                    % os.path.realpath(INSTALLED), LIS_TEST_LLAMA_SERVER=None)
            self.assertNotIn("TEST OVERRIDE", r.stderr)
        time.sleep(0.3)
        self.assertTrue(alive(p.pid), "the spoofing process must still run")

    def test_symlink_named_llama_server_to_another_binary_refused(self):
        p = self.spawn([str(self.fake), "60"])
        self.assert_refused("%d\n" % p.pid, "runs /bin/sleep, not the installed llama-server",
                            LIS_TEST_LLAMA_SERVER=str(self.other))
        self.assertTrue(alive(p.pid))

    def test_symlink_pid_file_refused(self):
        target = self.root / "elsewhere.pid"
        target.write_text("12345\n")
        self.pidfile.symlink_to(target)
        r = self.stop()
        self.assertEqual(r.returncode, 1)
        self.assertIn("symlink", r.stderr)

    def test_missing_pid_file_is_nothing_to_stop(self):
        r = self.stop()
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing to stop", r.stderr)

    def test_dead_pid_removes_stale_file(self):
        p = subprocess.Popen(["/usr/bin/true"])
        p.wait()
        self.pidfile.write_text("%d\n" % p.pid)
        r = self.stop()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not running", r.stderr)
        self.assertFalse(self.pidfile.exists())

    def start_fake(self, log_ref=None):
        p = self.spawn([str(self.fake), "60"])
        comm = subprocess.run(["ps", "-p", str(p.pid), "-o", "comm="], capture_output=True, text=True).stdout.strip()
        self.assertEqual(os.path.basename(comm), "llama-server")
        self.assertTrue(alive(p.pid), "the fake llama-server must be running for this test to mean anything")
        if log_ref is not None:
            (self.logs / "serve.lock").mkdir(exist_ok=True)
            (self.logs / "serve.lock" / "log").write_text("%s\n" % log_ref)
        self.pidfile.write_text("%d\n" % p.pid)
        return p

    def test_stops_the_expected_executable(self):
        for name in ("server-20261110T000000Z.log", "server-20261110T000000Z-4242.log"):
            with self.subTest(log=name):
                log = self.logs / name
                log.write_text("cmdline\n")
                p = self.start_fake(log)
                r = self.stop()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("TEST OVERRIDE: LIS_TEST_LLAMA_SERVER=", r.stderr)
                self.assertEqual(p.wait(timeout=10), -15, "stopped by SIGTERM")
                self.assertFalse(self.pidfile.exists())
                lines = log.read_text().splitlines()
                self.assertRegex(lines[1], r"^\[stop\.sh\] stop requested \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
                self.assertRegex(lines[2], r"^\[stop\.sh\] stopped PID %d " % p.pid)

    def test_inherited_xtrace_is_turned_off(self):
        """An inherited SHELLOPTS=xtrace/verbose traces nothing past set +xv on line 2."""
        for opts in ("xtrace", "xtrace:verbose"):
            with self.subTest(SHELLOPTS=opts):
                p = self.start_fake()
                r = self.stop(SHELLOPTS=opts, BASH_XTRACEFD="2")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(p.wait(timeout=10), -15)
                traced = [l for l in r.stderr.splitlines() if l.startswith("+")]
                self.assertLessEqual(set(traced), {"+ set +xv"}, traced)

    def test_exe_override_is_compared_as_is(self):
        p = self.start_fake()
        r = self.stop(LIS_TEST_LLAMA_SERVER=None, LIS_TEST_LLAMA_EXE="/bin/sleep")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("TEST OVERRIDE: LIS_TEST_LLAMA_EXE=/bin/sleep", r.stderr)
        self.assertEqual(p.wait(timeout=10), -15)

    def test_log_reference_outside_logs_ignored(self):
        victim = self.root / "victim.txt"
        victim.write_text("untouched\n")
        p = self.start_fake(victim)
        r = self.stop()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("stopped pid %d" % p.pid, r.stderr)
        self.assertEqual(victim.read_text(), "untouched\n")

    def test_log_reference_with_dotdot_or_slash_ignored(self):
        victim = self.root / "victim.log"
        (self.logs / "server-x").mkdir()
        (self.logs / "server-20261110T000000Z").mkdir()
        refs = [
            "%s/server-x/../../victim.log" % self.logs,                       # matches server-*.log
            "%s/server-20261110T000000Z/../../victim.log" % self.logs,
            "%s/server-..log" % self.logs,
            "%s/server-notastamp.log" % self.logs,
        ]
        (self.logs / "server-..log").write_text("untouched\n")
        (self.logs / "server-notastamp.log").write_text("untouched\n")
        for ref in refs:
            with self.subTest(ref=ref):
                victim.write_text("untouched\n")
                p = self.start_fake(ref)
                r = self.stop()
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(p.wait(timeout=10), -15)
                self.assertEqual(victim.read_text(), "untouched\n")
                self.assertEqual((self.logs / "server-..log").read_text(), "untouched\n")
                self.assertEqual((self.logs / "server-notastamp.log").read_text(), "untouched\n")


class StaticTest(unittest.TestCase):
    def test_single_quoted_term_kill_only(self):
        text = STOP.read_text()
        code = [l.strip() for l in text.splitlines() if not l.strip().startswith("#")]
        kills = [l for l in code if re.search(r"\bkill\b", l)]
        self.assertEqual(kills, ['kill -TERM "$pid" || refuse "kill -TERM $pid failed"'])
        for bad in ("pkill", "killall", "kill 0", "kill -1", "kill --", "kill -9", "KILL", "sudo"):
            self.assertFalse(any(bad in l for l in code), bad)

    def test_executable_checked_before_the_kill(self):
        text = STOP.read_text()
        self.assertLess(text.index('[ "$exe" = "$want" ] || refuse'), text.index('kill -TERM "$pid"'))
        self.assertIn("PATH=/usr/bin:/bin:/usr/sbin:/sbin", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
