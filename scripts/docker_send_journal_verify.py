#!/usr/bin/env python3
"""Driver for docker_send_journal_verify.sh (see that script's header)."""
import argparse
import http.cookiejar
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--password", required=True)
ap.add_argument("--container", required=True)
ap.add_argument("--artifacts", required=True)
ap.add_argument("--mode", default="full")
ap.add_argument("--commit", default="")
ap.add_argument("--browser-session", default="sjv")
args = ap.parse_args()

ART = Path(args.artifacts)
BASE = f"http://127.0.0.1:{args.port}"
PROJ = "/home/tester/proj"
APP = "/home/tester/.local/share/codoxear"
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
report: dict = {"commit": args.commit, "mode": args.mode, "scenarios": {}, "evidence": {}}
T0 = time.time()


def log(msg):
    print(f"[{time.time() - T0:6.1f}s] {msg}", flush=True)


def http(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, raw.decode(errors="replace")
    except (ConnectionError, urllib.error.URLError, TimeoutError):
        return 0, None


def dx(cmd, timeout=60):
    p = subprocess.run(["sudo", "docker", "exec", args.container, "sh", "-lc", cmd], capture_output=True, text=True, timeout=timeout)
    return p.stdout + p.stderr


PROC_LISTER = """
import glob
for p in glob.glob('/proc/[0-9]*/cmdline'):
    try:
        c = open(p, 'rb').read().replace(b'\\0', b' ').decode(errors='replace')
    except Exception:
        continue
    if 'tmux' not in c and c.strip():
        print(p.split('/')[2], c.strip())
"""


def ab(*a, timeout=60):
    env = {"AGENT_BROWSER_SESSION": args.browser_session, "PATH": subprocess.os.environ["PATH"], "HOME": subprocess.os.environ["HOME"]}
    p = subprocess.run(["agent-browser", *a], capture_output=True, text=True, timeout=timeout, env=env)
    return p.stdout + p.stderr


def ab_eval(js):
    out = ab("eval", js, "--json")
    try:
        return json.loads(out)["data"]["result"]
    except Exception:
        return {"_raw": out}


def verdict(name, ok, **evidence):
    report["scenarios"][name] = {"pass": bool(ok), **evidence}
    log(f"{'PASS' if ok else 'FAIL'} {name}")


def wait(pred, timeout, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


# ---- server up + login
wait(lambda: http("GET", "/api/me")[0] == 401, 90) or sys.exit("server never came up")
st, _ = http("POST", "/api/login", {"password": args.password})
assert st == 200, st

# ---- launch via the web launch path (what New session posts), tmux transport
t_launch = time.time()
st, launch = http("POST", "/api/sessions", {
    "cwd": PROJ, "agent_backend": "pi", "model_provider": "fake", "model": "fake-1", "create_in_tmux": True,
})
t_post_returned = time.time()
report["evidence"]["launch_response"] = launch
log(f"POST /api/sessions -> {st} in {t_post_returned - t_launch:.2f}s: {json.dumps(launch)[:300]}")
if st != 200:
    verdict("1_trust_dialog_suppressed", False, reason=f"launch failed {st} {launch}")
    (ART / "report.json").write_text(json.dumps(report, indent=2))
    sys.exit(1)


def find_row():
    st, d = http("GET", "/api/sessions")
    for r in d.get("sessions", []) if isinstance(d, dict) else []:
        if r.get("cwd") == PROJ and r.get("agent_backend") == "pi":
            return r
    return None


row = wait(find_row, 90)
t_row = time.time()
assert row, "web-launched session never appeared in /api/sessions"
sid = row["session_id"]
report["evidence"].update(session_id=sid, launch_post_seconds=round(t_post_returned - t_launch, 2),
                          launch_to_listed_seconds=round(t_row - t_launch, 2))
log(f"session {sid} listed {t_row - t_launch:.2f}s after launch POST; tmux={row.get('tmux_session')}:{row.get('tmux_window')}")
target = f"{row.get('tmux_session')}:{row.get('tmux_window')}"


def pane():
    return dx(f"tmux capture-pane -p -t '{target}' -S -60")


# Let the TUI settle, sampling the pane.
panes = []
for i in range(14):
    time.sleep(1)
    panes.append(pane())
(ART / "pane-after-launch.txt").write_text(panes[-1])
(ART / "pane-samples.json").write_text(json.dumps(panes))
trust_seen = any(re.search(r"trust", p, re.I) for p in panes)
report["evidence"]["pane_tail_after_launch"] = panes[-1].rstrip()[-1500:]
log("pane after launch:\n" + panes[-1].rstrip()[-900:])

# ---- send a distinctive prompt through the API
MARK1 = f"SJV-first-{int(time.time())}-π漢"
sends = []
for _ in range(30):
    st, resp = http("POST", f"/api/sessions/{sid}/send", {"text": MARK1})
    sends.append((st, resp))
    if st == 200:
        break
    time.sleep(1)
report["evidence"]["send1_attempts"] = [(s, r if isinstance(r, dict) else str(r)[:200]) for s, r in sends]
log(f"send1 -> {sends[-1][0]} {json.dumps(sends[-1][1])[:240]}")


def log_has(mark):
    out = dx(f"grep -rl --include='*.jsonl' -F -- '{mark}' /home/tester/.pi/agent/sessions 2>/dev/null | head -3")
    return out.strip() or None


# user-message row in the Pi jsonl
def user_row(mark):
    path = log_has(mark)
    if not path:
        return None
    for line in dx(f"cat {path.splitlines()[0]}").splitlines():
        try:
            o = json.loads(line)
        except Exception:
            continue
        m = o.get("message") or {}
        if o.get("type") == "message" and m.get("role") == "user" and mark in json.dumps(m, ensure_ascii=False):
            return {"file": path.splitlines()[0], "row": o}
    return None


landed = wait(lambda: user_row(MARK1), 40)
pane_after_send = pane()
(ART / "pane-after-send.txt").write_text(pane_after_send)
report["evidence"]["pane_tail_after_send"] = pane_after_send.rstrip()[-1500:]
report["evidence"]["pi_log_user_row_1"] = landed
log(f"send1 landed in Pi log: {bool(landed)}")
verdict("1_trust_dialog_suppressed",
        (not trust_seen) if args.mode == "full" else trust_seen,
        trust_dialog_text_in_pane=trust_seen, send_http_status=sends[-1][0], send_landed_in_pi_log=bool(landed))
if args.mode == "full":
    report["scenarios"]["1_trust_dialog_suppressed"]["pass"] = bool(report["scenarios"]["1_trust_dialog_suppressed"]["pass"] and landed)
else:
    # negative control: dialog present AND send did not land
    report["scenarios"]["1_trust_dialog_suppressed"]["pass"] = bool(trust_seen and not landed)
    report["scenarios"]["1_trust_dialog_suppressed"]["note"] = "baseline: PASS means the pre-fix commit shows the dialog and eats the send"

if args.mode == "full":
    # ---- 4. argv + latency
    argv = subprocess.run(["sudo", "docker", "exec", "-i", args.container, "python3", "-"], input=PROC_LISTER, capture_output=True, text=True).stdout
    shim_log = dx("cat $HOME/pi-argv.log")
    report["evidence"]["pi_argv_log"] = shim_log
    argv = argv + "\n--- pi-argv.log (shim) ---\n" + shim_log
    report["evidence"]["pi_processes"] = argv
    log("pi argv:\n" + argv)
    help_t = time.time(); dx("pi --help >/dev/null 2>&1"); help_s = time.time() - help_t
    probe = dx("cd /workspace && python3 -c \"import time; from codoxear.broker_launch import _pi_supports_approve as f; t=time.time(); r=f('pi'); print(r, round(time.time()-t,3))\"")
    report["evidence"]["supports_approve_probe"] = probe.strip()
    verdict("4_approve_in_argv", any("--approve" in ln and "--provider" in ln and "--help" not in ln for ln in shim_log.splitlines()), argv=argv.strip(), pi_help_probe_seconds=round(help_s, 2), supports_approve_probe=probe.strip(),
            launch_post_seconds=report["evidence"]["launch_post_seconds"],
            launch_to_listed_seconds=report["evidence"]["launch_to_listed_seconds"])

    # ---- 2. journal
    def read_journal():
        out = dx(f"cat {APP}/send_journal/{sid}.jsonl 2>/dev/null")
        rows = []
        for line in out.splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
        return rows

    def settle_idle():
        st, d = http("GET", "/api/sessions")
        r = next((x for x in d["sessions"] if x["session_id"] == sid), {})
        return not r.get("busy")

    wait(settle_idle, 40)
    rows = read_journal()
    sub1 = next((r for r in rows if r.get("type") == "submit" and r.get("display_text") == MARK1), None)
    out1 = next((r for r in rows if r.get("type") == "outcome" and sub1 and r.get("id") == sub1["id"]), None)
    st, hist = http("GET", f"/api/sessions/{sid}/sent-history")
    MARK2 = f"SJV-second-{int(time.time())}"
    st2, r2 = http("POST", f"/api/sessions/{sid}/send", {"text": MARK2})
    landed2 = wait(lambda: user_row(MARK2), 40)
    wait(settle_idle, 40)
    st, hist2 = http("GET", f"/api/sessions/{sid}/sent-history")
    texts2 = [e["text"] for e in hist2.get("entries", [])]
    rows = read_journal()
    sub2 = next((r for r in rows if r.get("type") == "submit" and r.get("display_text") == MARK2), None)
    out2 = next((r for r in rows if r.get("type") == "outcome" and sub2 and r.get("id") == sub2["id"]), None)
    report["evidence"]["journal_rows"] = rows
    report["evidence"]["sent_history_after_first"] = hist
    report["evidence"]["sent_history_after_second"] = hist2
    verdict("2a_journal_submit_outcome_history",
            bool(sub1 and sub1.get("text") and sub1.get("source") == "send" and out1 and out1.get("outcome") == "ack"
                 and [e["text"] for e in hist.get("entries", [])][:1] == [MARK1]),
            submit_row=sub1, outcome_row=out1)
    verdict("2b_second_send_ordering",
            bool(st2 == 200 and landed2 and sub2 and out2 and out2.get("outcome") == "ack" and texts2[:2] == [MARK2, MARK1]),
            second_send_status=st2, second_landed_in_pi_log=bool(landed2), history_texts=texts2)

    # ---- enqueue path
    MARK3 = f"SJV-enqueue-{int(time.time())}"
    st3, r3 = http("POST", f"/api/sessions/{sid}/enqueue", {"text": MARK3})
    log(f"enqueue -> {st3} {json.dumps(r3)[:200]}")
    landed3 = wait(lambda: user_row(MARK3), 60)
    def enq_rows():
        rs = read_journal()
        s = [r for r in rs if r.get("type") == "submit" and r.get("display_text") == MARK3]
        o = [r for r in rs if r.get("type") == "outcome" and s and r.get("id") == s[0]["id"]]
        return (s, o) if s and o else None
    so = wait(enq_rows, 30)
    s3 = [r for r in read_journal() if r.get("type") == "submit" and r.get("display_text") == MARK3]
    o3 = [r for r in read_journal() if r.get("type") == "outcome" and s3 and r.get("id") == s3[0]["id"]]
    verdict("2c_enqueue_journal", bool(st3 == 200 and len(s3) == 1 and s3[0].get("source") == "enqueue" and o3),
            enqueue_status=st3, enqueue_response=r3, submit_rows=s3, outcome_rows=o3, landed_in_pi_log=bool(landed3))
    MARK3_ok = bool(landed3)

    # ---- 3. composer recall in a real browser
    ab("open", BASE + "/")
    ab("fill", "#pw", args.password)
    ab("click", "#loginBtn")
    ab("wait", "2500")
    ab("click", f'#sessions .session[data-session-id="{sid}"]')
    ab("wait", "2500")
    val = lambda: ab_eval('(() => { const n = document.querySelector("#msg"); return {v: n ? n.value : null, focused: document.activeElement === n, pickerOpen: !!document.querySelector("#modelPicker") && getComputedStyle(document.querySelector("#modelPicker")).display !== "none", start: n && n.selectionStart, end: n && n.selectionEnd}; })()')
    steps = {"initial": val()}
    ab("focus", "#msg")
    steps["focused"] = val()
    ab("press", "ArrowUp"); ab("wait", "1200")
    steps["up1"] = val()
    ab("press", "ArrowUp"); ab("wait", "1200")
    steps["up2"] = val()
    ab("press", "ArrowUp"); ab("wait", "1200")
    steps["up3"] = val()
    ab("keyboard", "type", "Z"); ab("wait", "400")
    steps["after_char"] = val()
    ab("press", "ArrowUp"); ab("wait", "800")
    steps["up_after_char"] = val()
    ab("screenshot", "--full", str(ART / "recall-final.png"))
    report["evidence"]["recall_steps"] = steps
    newest = MARK3 if MARK3_ok else MARK2
    hist_now = [e["text"] for e in http("GET", f"/api/sessions/{sid}/sent-history")[1].get("entries", [])]
    report["evidence"]["history_at_recall_time"] = hist_now
    v = lambda k: steps[k].get("v") if isinstance(steps.get(k), dict) else None
    ok = (v("initial") == "" and v("up1") == hist_now[0] and v("up2") == hist_now[1] and
          v("up3") == hist_now[2] and v("after_char") is not None and "Z" in v("after_char")
          and v("after_char").replace("Z", "") == hist_now[2] and v("up_after_char") == v("after_char"))
    verdict("3_composer_arrowup_recall", ok, steps=steps, history_newest_first=hist_now)

(ART / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
log(f"total wall clock {time.time() - T0:.1f}s")
print(json.dumps({k: v["pass"] for k, v in report["scenarios"].items()}, indent=2))
sys.exit(0 if all(v["pass"] for v in report["scenarios"].values()) else 1)
