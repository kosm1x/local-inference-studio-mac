"""Chat template check against an ALREADY RUNNING llama-server (never starts or stops one).

Run from the repo root, with the server up (scripts/serve.sh --detach --wait 300):

    /opt/homebrew/bin/python3.13 -I scripts/tests/chat_template_check.py [--port N]
    /opt/homebrew/bin/python3.13 -I scripts/tests/chat_template_check.py --save-baseline FILE
    /opt/homebrew/bin/python3.13 -I scripts/tests/chat_template_check.py --baseline FILE

Claude Code sends conversations that llama-server turns into [system, user, system, ...]
(the later system messages carry hook and reminder text). A template that raises on a
system message after messages[0] makes every such turn an HTTP 500. This script renders
conversations through llama-server's POST /apply-template ({"messages": [...], "tools":
[...]} in, {"prompt": "..."} out; checked on llama-server 1.0.0, build 12359) and checks,
in both the tools and the no-tools branch of the template:

  (a) [system, user, system, user]: HTTP 200, one "<|im_start|>system" only, each system
      text exactly once in the prompt, and the whole leading system block exactly equal to
      the expected one. Expected = the leading system block of the SAME request rendered
      with its system messages removed (a second /apply-template call; in the tools branch
      that is the template's tools preamble), then a blank line, then the system texts
      joined by blank lines. When that second render has no system block (the no-tools
      branch of both shipped templates when no reasoning text is added), the expected block
      is the joined texts alone;
  (b) the same for [system, user, assistant, user, system];
  (c) a conversation without a system message still renders;
  (d) a system message with empty (or whitespace-only) content adds nothing;
  (e) one real POST /v1/chat/completions (max_tokens 8, with tools) of shape (a) is HTTP 200.

Byte-for-byte check of the unpatched path: --save-baseline FILE renders the BASELINE
conversations (at most one system message, at index 0) and writes them as JSON; run it
while the server uses the template to compare against (e.g. the GGUF's embedded one).
--baseline FILE then runs the checks above and also requires every BASELINE conversation
to render exactly as saved.

The API key is read from the first line of logs/api-key and sent as a header; it is never
printed or passed on a command line (it must be the 64 lowercase hex characters serve.sh
writes; anything else, an empty file included, stops the run with a message that does not
show the file's content). The port comes from config/lis.conf (PORT=) unless --port is
given, and must be a decimal integer 1-65535. GET /props must answer HTTP 200 with a string
chat_template: anything else is reported as a failure (with --save-baseline nothing is
written, since the template being saved could not be identified). Exit status 0 = all
checks passed.

No proxy is ever used: every request goes through OPENER (make_opener()), built with an
empty ProxyHandler, so neither http_proxy / HTTP_PROXY in the environment (which -I does
not clear) nor, on macOS, the system proxy settings (which urllib's getproxies() reads) can
route the request, and its Authorization header, to a proxy host. No redirect is ever
followed either (urllib would copy the Authorization header to the new host): a 3xx reply
is a counted FAIL for that request. So are a reply that is not UTF-8, an error reply whose
body cannot be read, and a 200 from /apply-template that is not a JSON object with a string
"prompt". The --save-baseline FILE's directory must exist and be writable (bench/results/
is gitignored, so on a fresh clone: mkdir -p bench/results); otherwise the run stops with
one FAIL line before anything is rendered. An empty or whitespace-only FILE (an unset shell
variable, say) stops the run with one FAIL line before the port or the key is read.

    /opt/homebrew/bin/python3.13 -I scripts/tests/chat_template_check.py --self-test

runs offline checks of the above (no server, no key file read) and exits 0 when they pass.
"""
import argparse
import hashlib
import http.client
import io
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONF = REPO / "config" / "lis.conf"
KEYFILE = REPO / "logs" / "api-key"

S1 = "SYS-ALPHA-7f3a first system text"
S2 = "SYS-BRAVO-91c2 later system text\nwith a second line"
S3 = "SYS-CHARLIE-d40e third system text"
SYS_OPEN = "<|im_start|>system\n"
END = "<|im_end|>"

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return the current time in a time zone.",
        "parameters": {"type": "object", "properties": {"tz": {"type": "string"}}, "required": ["tz"]},
    },
}]


def sysm(text):
    return {"role": "system", "content": text}


def user(text):
    return {"role": "user", "content": text}


def asst(text, **extra):
    m = {"role": "assistant", "content": text}
    m.update(extra)
    return m


TOOL_CALL = {"id": "call_1", "type": "function",
             "function": {"name": "get_time", "arguments": json.dumps({"tz": "UTC"})}}

# Conversations with at most one system message, at index 0: the patched template must
# render these exactly as the original does. (name, request body)
BASELINE = []
for _branch, _extra in (("notools", {}), ("tools", {"tools": TOOLS})):
    BASELINE += [
        ("sys-user-" + _branch, dict(messages=[sysm(S1), user("hello")], **_extra)),
        ("user-only-" + _branch, dict(messages=[user("hello")], **_extra)),
        ("empty-sys-user-" + _branch, dict(messages=[sysm(""), user("hello")], **_extra)),
        ("padded-sys-user-" + _branch, dict(messages=[sysm("  \n" + S1 + " \n\n"), user("hello")], **_extra)),
        ("sys-parts-user-" + _branch, dict(messages=[
            {"role": "system", "content": [{"type": "text", "text": S1}, {"type": "text", "text": " more"}]},
            user("hello")], **_extra)),
        ("multi-turn-" + _branch, dict(messages=[
            sysm(S1), user("q1"), asst("a1", reasoning_content="thought one"), user("q2")], **_extra)),
        ("inline-think-" + _branch, dict(messages=[
            sysm(S1), user("q1"), asst("<think>\nhidden\n</think>\n\nshown"), user("q2")], **_extra)),
        ("no-gen-prompt-" + _branch, dict(messages=[sysm(S1), user("hello")], add_generation_prompt=False, **_extra)),
        ("thinking-off-" + _branch, dict(messages=[sysm(S1), user("hello")],
                                         chat_template_kwargs={"enable_thinking": False}, **_extra)),
    ]
BASELINE += [
    ("tool-round-trip-tools", dict(messages=[
        sysm(S1), user("time?"), asst("", tool_calls=[TOOL_CALL]),
        {"role": "tool", "tool_call_id": "call_1", "content": "12:00"},
        asst("It is 12:00."), user("thanks")], tools=TOOLS)),
    ("tool-pending-tools", dict(messages=[
        sysm(S1), user("time?"), asst("checking", tool_calls=[TOOL_CALL]),
        {"role": "tool", "tool_call_id": "call_1", "content": "12:00"}], tools=TOOLS)),
]


PORT_RE = re.compile(r"[1-9][0-9]{0,4}")  # ASCII digits only, no sign, no leading zero
KEY_RE = re.compile(r"[0-9a-f]{64}")


def read_port():
    for line in CONF.read_text().splitlines():
        if line.startswith("PORT="):
            return line[len("PORT="):].strip()
    sys.exit("FAIL: no PORT= line in config/lis.conf")


def valid_port(text):
    """The port as an int, or exit. Checked before any URL is built from it."""
    if not PORT_RE.fullmatch(text) or int(text) > 65535:
        sys.exit("FAIL: the port must be an integer 1-65535, got %r" % text)
    return int(text)


def read_key():
    """The first line of logs/api-key, or exit. The content is never shown in a message."""
    try:
        lines = KEYFILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    except (OSError, UnicodeDecodeError) as e:
        sys.exit("FAIL: cannot read logs/api-key (%s)" % e.__class__.__name__)
    key = lines[0].strip() if lines else ""
    if not key:
        sys.exit("FAIL: logs/api-key is missing or empty (start the server with scripts/serve.sh)")
    if not KEY_RE.fullmatch(key):
        sys.exit("FAIL: logs/api-key does not hold a 64-hex key (start the server with scripts/serve.sh)")
    return key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follows a redirect. urllib's HTTPRedirectHandler copies the request's headers,
    "Authorization: Bearer <key>" included, to the redirect target whatever its host; and
    llama-server does not redirect. Being a subclass, this replaces the default handler in
    build_opener. Every 3xx falls through to HTTPDefaultErrorHandler, so the open raises
    HTTPError with the 3xx code, which Server.request turns into BAD_REPLY."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

    def refuse(self, req, fp, code, msg, headers):
        return None

    http_error_301 = http_error_302 = http_error_303 = http_error_307 = http_error_308 = refuse


def make_opener():
    """The opener every request goes through. urllib's default opener (urlopen) adds a
    ProxyHandler built from getproxies(): http_proxy / HTTP_PROXY in the environment
    (python -I does not clear them) or, on macOS when neither is set, the system proxy
    settings; proxy_bypass("127.0.0.1") is false while such a variable is set, so the
    request, with its "Authorization: Bearer <key>" header, would go to the proxy host. An
    empty ProxyHandler replaces that default one (no proxy is ever used), and NoRedirect
    replaces the default HTTPRedirectHandler (no redirect is ever followed)."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


OPENER = make_opener()

# The status reported in place of an HTTP code when a reply cannot be used. Every caller
# compares the status with 200, so such a reply is a counted FAIL for its case.
BAD_REPLY = "(unusable reply)"


def http_error_reply(e, method, path):
    """(status, text) for an HTTPError from OPENER.open. A 3xx (a redirect, never followed)
    and an error body that cannot be read are BAD_REPLY. No message built here includes the
    key, the headers or the redirect target."""
    try:
        if isinstance(e.code, int) and 300 <= e.code < 400:
            return BAD_REPLY, "HTTP %d %s %s: a redirect, not followed (llama-server does not redirect)" % (
                e.code, method, path)
        try:
            raw = e.read()
        except (http.client.HTTPException, OSError) as x:
            return BAD_REPLY, "HTTP %s %s %s: the error body could not be read (%s)" % (
                e.code, method, path, x.__class__.__name__)
        return e.code, raw.decode("utf-8", "replace")
    finally:
        try:
            e.close()
        except (http.client.HTTPException, OSError):
            pass


class Server:
    def __init__(self, port, key):
        self.base = "http://127.0.0.1:%d" % port
        self._headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}

    def request(self, method, path, body=None, timeout=120):
        """(status, text). status is the HTTP code, or BAD_REPLY with a reason in text when
        the body is not UTF-8, the reply is a redirect or an error body cannot be read. No
        message built here includes the key or the headers."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self._headers)
        try:
            with OPENER.open(req, timeout=timeout) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            return http_error_reply(e, method, path)
        except (urllib.error.URLError, OSError) as e:
            sys.exit("FAIL: no server at %s (%s); start it with scripts/serve.sh --detach --wait 300"
                     % (self.base, e))
        except http.client.HTTPException as e:
            sys.exit("FAIL: malformed HTTP reply from %s%s (%s)" % (self.base, path, e.__class__.__name__))
        try:
            return status, raw.decode("utf-8")
        except UnicodeDecodeError as e:
            return BAD_REPLY, "HTTP %d %s %s: the body (%d bytes) is not UTF-8 (byte %d)" % (
                status, method, path, len(raw), e.start)

    def render(self, body):
        """(status, prompt, text). prompt is None unless status is 200 and the reply is a
        JSON object with a string "prompt"; otherwise status is the HTTP code or BAD_REPLY."""
        status, text = self.request("POST", "/apply-template", body)
        if status != 200:
            return status, None, text
        try:
            reply = json.loads(text)
        except (ValueError, RecursionError):
            return BAD_REPLY, None, "HTTP 200 /apply-template: the body is not JSON: " + text[:200]
        if not isinstance(reply, dict) or not isinstance(reply.get("prompt"), str):
            return BAD_REPLY, None, 'HTTP 200 /apply-template: no string "prompt" in the reply: ' + text[:200]
        return status, reply["prompt"], text


class Checker:
    def __init__(self):
        self.failed = 0

    def check(self, name, ok, detail=""):
        print("%s %s%s" % ("PASS" if ok else "FAIL", name, "" if ok or not detail else ": " + detail))
        if not ok:
            self.failed += 1
        return ok


def system_block(prompt):
    """The leading system block's text, or None when the prompt has none."""
    if not prompt.startswith(SYS_OPEN):
        return None
    end = prompt.find(END)
    # No end marker: the rest of the prompt, which then fails the exact comparison.
    return prompt[len(SYS_OPEN):] if end < 0 else prompt[len(SYS_OPEN):end]


def without_system(body):
    """The same request body with every system message removed (all other keys kept)."""
    out = dict(body)
    out["messages"] = [m for m in body["messages"] if m.get("role") != "system"]
    return out


def first_diff(a, b):
    return next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))


def check_merged(c, srv, name, body, texts):
    status, prompt, raw = srv.render(body)
    if not c.check(name + " HTTP 200", status == 200, "HTTP %s: %s" % (status, raw[:300])):
        return
    block = system_block(prompt)
    c.check(name + " one leading system block", block is not None and prompt.count("<|im_start|>system") == 1,
            "system openers: %d, starts with system: %s" % (prompt.count("<|im_start|>system"), block is not None))
    if block is None:
        return
    c.check(name + " system texts once each", all(prompt.count(t) == 1 for t in texts),
            "counts %s" % [prompt.count(t) for t in texts])
    # The expected block is derived, not hard-coded: what the template puts in the block
    # for this request without any system message (the tools preamble in the tools branch,
    # usually nothing in the no-tools branch), then the system texts, blank-line separated.
    rstatus, rprompt, rraw = srv.render(without_system(body))
    if not c.check(name + " reference render without system messages HTTP 200", rstatus == 200,
                   "HTTP %s: %s" % (rstatus, rraw[:300])):
        return
    base = system_block(rprompt)
    if "tools" in body and not c.check(name + " reference render has a tools system block", base is not None,
                                       "starts %r" % rprompt[:120]):
        return
    joined = "\n\n".join(texts)
    expected = joined if base is None else base + "\n\n" + joined
    at = first_diff(block, expected)
    c.check(name + " system block exactly as expected (%d chars)" % len(expected), block == expected,
            "lengths %d/%d, first difference at char %d: got %r / expected %r"
            % (len(block), len(expected), at, block[max(0, at - 20):at + 60], expected[max(0, at - 20):at + 60]))


def same_render(c, srv, name, body_a, body_b):
    sa, pa, ra = srv.render(body_a)
    sb, pb, rb = srv.render(body_b)
    if not c.check(name + " HTTP 200", sa == 200 and sb == 200, "HTTP %s/%s: %s" % (sa, sb, (ra if sa != 200 else rb)[:300])):
        return
    c.check(name + " renders as without it", pa == pb, "with %r / without %r" % (pa[:200], pb[:200]))


def run_checks(c, srv):
    for branch, extra in (("no-tools", {}), ("tools", {"tools": TOOLS})):
        # (a)
        check_merged(c, srv, "(a) [system, user, system, user] " + branch,
                     dict(messages=[sysm(S1), user("u1"), sysm(S2), user("u2")], **extra), [S1, S2])
        check_merged(c, srv, "(a) three system messages, list content " + branch,
                     dict(messages=[sysm(S1), user("u1"),
                                    {"role": "system", "content": [{"type": "text", "text": S2}]},
                                    user("u2"), sysm(S3), user("u3")], **extra), [S1, S2, S3])
        check_merged(c, srv, "(a) no leading system, [user, system, user] " + branch,
                     dict(messages=[user("u1"), sysm(S2), user("u2")], **extra), [S2])
        # (b)
        check_merged(c, srv, "(b) [system, user, assistant, user, system] " + branch,
                     dict(messages=[sysm(S1), user("u1"), asst("a1"), user("u2"), sysm(S2)], **extra), [S1, S2])
        # (c)
        status, prompt, raw = srv.render(dict(messages=[user("u1"), asst("a1"), user("u2")], **extra))
        if c.check("(c) no system message " + branch + " HTTP 200", status == 200, "HTTP %s: %s" % (status, raw[:300])):
            if extra:
                c.check("(c) no system message " + branch + " one tools-only system block",
                        prompt.count("<|im_start|>system") == 1 and system_block(prompt) is not None
                        and system_block(prompt).endswith("</IMPORTANT>"), repr(prompt[:120]))
            else:
                c.check("(c) no system message " + branch + " no system block",
                        "<|im_start|>system" not in prompt, repr(prompt[:120]))
        # (d)
        for label, empty in (("empty", ""), ("whitespace-only", " \n\t ")):
            same_render(c, srv, "(d) later %s system message %s" % (label, branch),
                        dict(messages=[sysm(S1), user("u1"), sysm(empty), user("u2")], **extra),
                        dict(messages=[sysm(S1), user("u1"), user("u2")], **extra))
            same_render(c, srv, "(d) later %s system message, no leading system %s" % (label, branch),
                        dict(messages=[user("u1"), sysm(empty), user("u2")], **extra),
                        dict(messages=[user("u1"), user("u2")], **extra))
    # (e) the real endpoint, as a Claude Code turn would reach it.
    status, raw = srv.request("POST", "/v1/chat/completions", dict(
        messages=[sysm(S1), user("Say ok."), sysm(S2), user("Say ok.")], tools=TOOLS, max_tokens=8))
    c.check("(e) /v1/chat/completions [system, user, system, user] with tools HTTP 200", status == 200,
            "HTTP %s: %s" % (status, raw[:300]))


def render_baseline(srv):
    """({name: prompt}, {name: why}) for the BASELINE cases; a case that does not render
    is in the second dict, not the first."""
    out, bad = {}, {}
    for name, body in BASELINE:
        status, prompt, raw = srv.render(body)
        if status != 200:
            bad[name] = "HTTP %s: %s" % (status, raw[:300])
        else:
            out[name] = prompt
    return out, bad


def baseline_dir_problem(path):
    """Why FILE cannot be written, judged on its directory before anything is rendered,
    or None. The write itself is still checked (save_baseline)."""
    d = Path(path).parent
    if not d.is_dir():
        return "directory %s does not exist (mkdir -p it first; bench/results/ is gitignored)" % d
    if not os.access(d, os.W_OK | os.X_OK):
        return "directory %s is not writable" % d
    return None


def save_baseline(path, out):
    """Write the baseline; True, or False after one FAIL line."""
    try:
        Path(path).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    except OSError as e:
        print("FAIL: cannot write the baseline file %s (%s: %s)" % (path, e.__class__.__name__, e.strerror))
        return False
    return True


def load_baseline(path):
    """The saved {name: prompt} dict, or exit with one FAIL line."""
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        sys.exit("FAIL: cannot read the baseline file %s (%s: %s)" % (path, e.__class__.__name__, e.strerror))
    except (ValueError, RecursionError):
        sys.exit("FAIL: the baseline file %s is not JSON" % path)
    if not isinstance(saved, dict) or not all(isinstance(v, str) for v in saved.values()):
        sys.exit("FAIL: the baseline file %s is not a JSON object of strings" % path)
    return saved


# ---------------------------------------------------------------- --self-test (offline)
class FakeServer(Server):
    """A Server whose replies come from a list; request() is the real one for raw replies."""
    def __init__(self, replies):
        Server.__init__(self, 1, "0" * 64)
        self.replies = list(replies)

    def request(self, method, path, body=None, timeout=120):
        status, raw = self.replies.pop(0)
        if isinstance(raw, bytes):
            try:
                return status, raw.decode("utf-8")
            except UnicodeDecodeError:
                return BAD_REPLY, "not UTF-8"
        return status, raw


def self_test():
    """Offline checks: no server, no key file. Exit status 0 = all passed."""
    global OPENER  # swapped for a stub once below, and put back
    c = Checker()
    # build_opener drops its default (environment) ProxyHandler because one is passed, and
    # keeps the passed empty one out of the chain since it handles no scheme: so OPENER
    # has no handler that proxies anything, and http/https go straight to HTTP(S)Handler.
    def proxying(opener):
        return [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler) and h.proxies]

    def openers_of(opener, scheme):
        return [type(h).__name__ for h in opener.handle_open.get(scheme, [])]

    c.check("OPENER has no ProxyHandler with proxies", proxying(OPENER) == [],
            repr([h.proxies for h in proxying(OPENER)]))
    for scheme, want in (("http", ["HTTPHandler"]), ("https", ["HTTPSHandler"])):
        c.check("OPENER opens %s with %s only" % (scheme, want[0]), openers_of(OPENER, scheme) == want,
                repr(openers_of(OPENER, scheme)))
    names = Server.request.__code__.co_names
    c.check("Server.request opens through OPENER, never urlopen", "OPENER" in names and "urlopen" not in names,
            repr(names))
    # With a proxy variable set: what the default would do, and that OPENER ignores it.
    saved_env = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY",
                                                 "no_proxy", "NO_PROXY")}
    try:
        for k in saved_env:
            os.environ.pop(k, None)
        os.environ["http_proxy"] = "http://127.0.0.1:9"
        c.check("with http_proxy set, the environment names a proxy for http",
                urllib.request.getproxies().get("http") == "http://127.0.0.1:9", repr(urllib.request.getproxies()))
        c.check("with http_proxy set, 127.0.0.1 is not bypassed by default",
                not urllib.request.proxy_bypass("127.0.0.1"))
        c.check("with http_proxy set, a default ProxyHandler would proxy http",
                urllib.request.ProxyHandler().proxies.get("http") == "http://127.0.0.1:9")
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
        default = urllib.request.build_opener()
        c.check("with http_proxy set, the default opener (urlopen's) would proxy http",
                "ProxyHandler" in openers_of(default, "http"), repr(openers_of(default, "http")))
        # make_opener() is called here, with the variables set, so a make_opener that left
        # the environment's ProxyHandler in would fail this even when the run started
        # without any proxy variable (OPENER, built at import, would then pass vacuously).
        fresh = make_opener()
        c.check("with http_proxy / HTTP_PROXY set, make_opener() has no ProxyHandler with proxies",
                proxying(fresh) == [], repr([h.proxies for h in proxying(fresh)]))
        c.check("with http_proxy / HTTP_PROXY set, make_opener() opens http with HTTPHandler only",
                openers_of(fresh, "http") == ["HTTPHandler"], repr(openers_of(fresh, "http")))
        c.check("with http_proxy set, OPENER opens http with HTTPHandler only",
                proxying(OPENER) == [] and openers_of(OPENER, "http") == ["HTTPHandler"])
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    # Replies that cannot be used are counted FAILs (BAD_REPLY), never a traceback.
    for label, reply in (("not JSON", (200, "<html>oops")),
                         ("JSON without prompt", (200, '{"other": 1}')),
                         ("prompt not a string", (200, '{"prompt": 7}')),
                         ("JSON array", (200, '["prompt"]')),
                         ("deeply nested JSON", (200, "[" * 100000 + "]" * 100000)),
                         ("body not UTF-8", (200, b"\xff\xfe{\"prompt\": \"x\"}"))):
        status, prompt, raw = FakeServer([reply]).render({"messages": []})
        c.check("render: %s is BAD_REPLY" % label, status == BAD_REPLY and prompt is None, repr((status, raw[:80])))
    status, prompt, _ = FakeServer([(200, '{"prompt": "ok"}')]).render({"messages": []})
    c.check("render: a good reply passes", status == 200 and prompt == "ok")
    # The real request(): a non-UTF-8 body becomes BAD_REPLY with a message without the key.
    key = "ab" * 32

    class Resp:
        status = 200

        def read(self):
            return b"\xff\xfe"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Op:
        def open(self, req, timeout=None):
            return Resp()

    real = OPENER
    OPENER = Op()
    try:
        status, text = Server(1, key).request("POST", "/apply-template", {"messages": []})
    finally:
        OPENER = real
    c.check("request: a non-UTF-8 body is BAD_REPLY", status == BAD_REPLY, repr(status))
    c.check("request: its message does not contain the key", key not in text and "Bearer" not in text, text)
    # Redirects are never followed. Structure: NoRedirect in place of the default handler.
    for label, opener in (("OPENER", OPENER), ("make_opener()", make_opener())):
        plain = [h for h in opener.handlers if type(h) is urllib.request.HTTPRedirectHandler]
        ours = [h for h in opener.handlers if isinstance(h, NoRedirect)]
        c.check("%s has NoRedirect and no default HTTPRedirectHandler" % label, plain == [] and len(ours) == 1,
                repr([type(h).__name__ for h in opener.handlers]))
    # Behaviour, with no network: hand a 3xx reply to an opener's error chain, with its open()
    # replaced by a recorder. The default opener follows it, Authorization header included;
    # make_opener()'s raises HTTPError with the 3xx code and opens nothing.
    def drive_redirect(opener, code):
        opened = []
        opener.open = lambda r, *a, **k: opened.append(r) or "followed"
        req = urllib.request.Request("http://127.0.0.1:1/props", headers={"Authorization": "Bearer " + key})
        req.timeout = 5
        hdrs = http.client.HTTPMessage()
        hdrs["Location"] = "http://elsewhere.invalid/next"
        try:
            result = opener.error("http", req, io.BytesIO(b"moved"), code, "Moved", hdrs)
        except urllib.error.HTTPError as e:
            result = e
        return result, opened
    result, opened = drive_redirect(urllib.request.build_opener(), 302)
    c.check("the default opener would follow a 302 with the Authorization header",
            len(opened) == 1 and opened[0].get_header("Authorization") == "Bearer " + key, repr(result))
    for code in (301, 302, 303, 307, 308, 300):
        result, opened = drive_redirect(make_opener(), code)
        c.check("make_opener() refuses a %d: HTTPError %d, nothing opened" % (code, code),
                opened == [] and isinstance(result, urllib.error.HTTPError) and result.code == code,
                repr((type(result).__name__, len(opened))))

    class Raiser:
        def __init__(self, exc):
            self.exc = exc

        def open(self, req, timeout=None):
            raise self.exc

    class BadBody(io.RawIOBase):
        def __init__(self, exc):
            self.exc = exc

        def read(self, *a):
            raise self.exc

    def request_with(exc):
        global OPENER
        real = OPENER
        OPENER = Raiser(exc)
        try:
            return Server(1, key).request("GET", "/props")
        except SystemExit as e:
            return "exit", str(e)
        except Exception as e:  # a traceback here is a counted FAIL of the check below
            return "raised", e.__class__.__name__
        finally:
            OPENER = real
    for code in (301, 302, 307, 308):
        status, text = request_with(urllib.error.HTTPError(
            "http://127.0.0.1:1/props", code, "Moved", http.client.HTTPMessage(), io.BytesIO(b"")))
        c.check("request: a %d reply is BAD_REPLY, message without the key" % code,
                status == BAD_REPLY and key not in text and "Bearer" not in text and "elsewhere" not in text,
                repr((status, text)))
    for exc in (http.client.IncompleteRead(b"par", 10), OSError("reset")):
        status, text = request_with(urllib.error.HTTPError(
            "http://127.0.0.1:1/props", 500, "Error", http.client.HTTPMessage(), BadBody(exc)))
        c.check("request: an error body that fails to read (%s) is BAD_REPLY" % exc.__class__.__name__,
                status == BAD_REPLY and key not in text and "Bearer" not in text, repr((status, text)))
    status, text = request_with(urllib.error.HTTPError(
        "http://127.0.0.1:1/props", 500, "Error", http.client.HTTPMessage(), io.BytesIO(b"boom")))
    c.check("request: a readable error body keeps its HTTP code", (status, text) == (500, "boom"), repr((status, text)))
    # An empty or whitespace-only baseline path is refused, not taken as "not given".
    for save, base, want in (("", None, "--save-baseline"), (None, "", "--baseline"), (" \t", None, "--save-baseline"),
                             (None, "\n", "--baseline"), (None, None, None), ("b.json", None, None),
                             (None, "b.json", None)):
        got = blank_path_flag(argparse.Namespace(save_baseline=save, baseline=base))
        c.check("blank_path_flag(save=%r, baseline=%r) is %r" % (save, base, want), got == want, repr(got))
    # check_merged on bad replies counts FAILs and returns.
    inner = Checker()
    sys_stdout, sys.stdout = sys.stdout, open(os.devnull, "w")
    try:
        check_merged(inner, FakeServer([(200, "not json")]), "x", {"messages": []}, [S1])
        same_render(inner, FakeServer([(200, b"\xff"), (200, '{"prompt": "p"}')]), "y", {}, {})
        _, bad = render_baseline(FakeServer([(200, "nope")] * len(BASELINE)))
    finally:
        sys.stdout.close()
        sys.stdout = sys_stdout
    c.check("check_merged and same_render count bad replies as FAIL", inner.failed == 2, "failed %d" % inner.failed)
    c.check("render_baseline reports each bad case", len(bad) == len(BASELINE), "%d of %d" % (len(bad), len(BASELINE)))
    # The baseline directory.
    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "nope", "b.json")
        c.check("a missing baseline directory is reported", baseline_dir_problem(missing) is not None)
        ro = os.path.join(tmp, "ro")
        os.mkdir(ro, 0o500)
        try:
            if os.geteuid() != 0:
                c.check("an unwritable baseline directory is reported",
                        baseline_dir_problem(os.path.join(ro, "b.json")) is not None)
                sys_stdout, sys.stdout = sys.stdout, open(os.devnull, "w")
                try:
                    ok = save_baseline(os.path.join(ro, "b.json"), {"a": "b"})
                finally:
                    sys.stdout.close()
                    sys.stdout = sys_stdout
                c.check("a failed baseline write returns False", ok is False)
        finally:
            os.chmod(ro, 0o700)
        good = os.path.join(tmp, "b.json")
        c.check("a writable baseline directory passes", baseline_dir_problem(good) is None)
        c.check("save_baseline writes the file", save_baseline(good, {"a": "b"}) and load_baseline(good) == {"a": "b"})
    print("PASS" if not c.failed else "FAIL: %d check(s) failed" % c.failed)
    return 1 if c.failed else 0


def blank_path_flag(args):
    """The first baseline flag given with an empty or whitespace-only path, or None."""
    for flag, value in (("--save-baseline", args.save_baseline), ("--baseline", args.baseline)):
        if value is not None and not value.strip():
            return flag
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--save-baseline", metavar="FILE")
    g.add_argument("--baseline", metavar="FILE")
    g.add_argument("--self-test", action="store_true", help="offline checks only (no server, no key file)")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    flag = blank_path_flag(args)
    if flag:
        print("FAIL: %s needs a file path, got an empty one" % flag)
        return 1
    if args.save_baseline is not None:
        why = baseline_dir_problem(args.save_baseline)
        if why:
            print("FAIL: cannot save the baseline to %s: %s" % (args.save_baseline, why))
            return 1
    saved = load_baseline(args.baseline) if args.baseline is not None else None
    port = valid_port(args.port if args.port is not None else read_port())
    srv = Server(port, read_key())

    c = Checker()
    status, raw = srv.request("GET", "/props")
    tpl, why = None, "HTTP %s: %s" % (status, raw[:300])
    if status == 200:
        try:
            props = json.loads(raw)
        except (ValueError, RecursionError):
            props = None
        if not isinstance(props, dict):
            why = "HTTP 200 but the body is not a JSON object"
        elif not isinstance(props.get("chat_template"), str):
            why = "HTTP 200 but chat_template is missing or not a string"
        else:
            tpl = props["chat_template"]
    if c.check("GET /props HTTP 200 with a chat_template string", tpl is not None, why):
        print("server chat template: %d chars, md5 %s" % (len(tpl), hashlib.md5(tpl.encode("utf-8")).hexdigest()))

    if args.save_baseline is not None:
        if c.failed:
            print("FAIL: not saving a baseline: the server's chat template could not be identified")
            return 1
        out, bad = render_baseline(srv)
        for name in sorted(bad):
            c.check("baseline case " + name, False, bad[name])
        if bad:
            print("FAIL: not saving a baseline: %d case(s) did not render" % len(bad))
            return 1
        if not save_baseline(args.save_baseline, out):
            return 1
        print("saved %d baseline renderings to %s" % (len(out), args.save_baseline))
        return 0

    run_checks(c, srv)
    if args.baseline is not None:
        now, bad = render_baseline(srv)
        for name, _ in BASELINE:
            if name not in saved:
                c.check("byte-for-byte " + name, False, "not in the baseline file")
                continue
            if name in bad:
                c.check("byte-for-byte " + name, False, bad[name])
                continue
            a, b = saved[name], now[name]
            at = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
            c.check("byte-for-byte %s (%d bytes)" % (name, len(b.encode("utf-8"))), a == b,
                    "first difference at char %d: saved %r / now %r" % (at, a[at:at + 60], b[at:at + 60]))
    print("PASS" if not c.failed else "FAIL: %d check(s) failed" % c.failed)
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
