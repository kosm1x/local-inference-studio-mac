"""Chat template check against an ALREADY RUNNING llama-server (never starts or stops one).

Run from the repo root, with the server up (scripts/serve.sh --detach --wait 300):

    python3.13 -I scripts/tests/chat_template_check.py [--port N]
    python3.13 -I scripts/tests/chat_template_check.py --save-baseline FILE
    python3.13 -I scripts/tests/chat_template_check.py --baseline FILE

Claude Code sends conversations that llama-server turns into [system, user, system, ...]
(the later system messages carry hook and reminder text). A template that raises on a
system message after messages[0] makes every such turn an HTTP 500. This script renders
conversations through llama-server's POST /apply-template ({"messages": [...], "tools":
[...]} in, {"prompt": "..."} out; checked on llama-server 1.0.0, build 12359) and checks,
in both the tools and the no-tools branch of the template:

  (a) [system, user, system, user]: HTTP 200, both system texts exactly once, in order,
      inside the one leading system block, and no second "<|im_start|>system";
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
printed or passed on a command line. The port comes from config/lis.conf (PORT=) unless
--port is given. Exit status 0 = all checks passed.
"""
import argparse
import hashlib
import json
import sys
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


def read_port():
    for line in CONF.read_text().splitlines():
        if line.startswith("PORT="):
            return line[len("PORT="):].strip()
    sys.exit("FAIL: no PORT= line in config/lis.conf")


class Server:
    def __init__(self, port):
        self.base = "http://127.0.0.1:%s" % port
        key = KEYFILE.read_text().splitlines()[0].strip() if KEYFILE.exists() else ""
        if not key:
            sys.exit("FAIL: logs/api-key is missing or empty (start the server with scripts/serve.sh)")
        self._headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}

    def request(self, method, path, body=None, timeout=120):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self._headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            sys.exit("FAIL: no server at %s (%s); start it with scripts/serve.sh --detach --wait 300"
                     % (self.base, e))

    def render(self, body):
        status, text = self.request("POST", "/apply-template", body)
        if status != 200:
            return status, None, text
        return status, json.loads(text)["prompt"], text


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
    return prompt[len(SYS_OPEN):prompt.index(END)]


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
    pos = [block.find(t) for t in texts]
    c.check(name + " system texts in the block, in order", all(p >= 0 for p in pos) and pos == sorted(pos),
            "positions in block %s" % pos)
    c.check(name + " blank-line separated, block ends with the last text",
            block.endswith("\n\n".join(texts)), "block tail %r" % block[-120:])


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
    out = {}
    for name, body in BASELINE:
        status, prompt, raw = srv.render(body)
        if status != 200:
            sys.exit("FAIL: baseline case %s: HTTP %s: %s" % (name, status, raw[:300]))
        out[name] = prompt
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--save-baseline", metavar="FILE")
    g.add_argument("--baseline", metavar="FILE")
    args = ap.parse_args()
    srv = Server(args.port or read_port())

    status, raw = srv.request("GET", "/props")
    if status == 200:
        tpl = json.loads(raw).get("chat_template", "")
        print("server chat template: %d chars, md5 %s" % (len(tpl), hashlib.md5(tpl.encode("utf-8")).hexdigest()))

    if args.save_baseline:
        out = render_baseline(srv)
        Path(args.save_baseline).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
        print("saved %d baseline renderings to %s" % (len(out), args.save_baseline))
        return 0

    c = Checker()
    run_checks(c, srv)
    if args.baseline:
        saved = json.loads(Path(args.baseline).read_text())
        now = render_baseline(srv)
        for name, _ in BASELINE:
            if name not in saved:
                c.check("byte-for-byte " + name, False, "not in the baseline file")
                continue
            a, b = saved[name], now[name]
            at = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
            c.check("byte-for-byte %s (%d bytes)" % (name, len(b.encode("utf-8"))), a == b,
                    "first difference at char %d: saved %r / now %r" % (at, a[at:at + 60], b[at:at + 60]))
    print("PASS" if not c.failed else "FAIL: %d check(s) failed" % c.failed)
    return 1 if c.failed else 0


if __name__ == "__main__":
    sys.exit(main())
