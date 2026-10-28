# gcua — Windows GCUA harness for DSH (v1)
# Python 3.13, no daemon: the DSH agent IS the model; this file is the sandbox+ledger+guard.
# deps: pillow, pyautogui (present), pywinauto optional for UIA tree.
"""Usage:
  python gcua.py step code.py     # execute one <code> step in an isolated worker
  python gcua.py status           # todo ledger + stall history
  python gcua.py reset            # wipe state + step scripts (screenshots kept for audit)
  python gcua.py selftest         # smoke-check all pieces
State: ./.gcua/state.json, screenshots: ./.gcua/shot-NNN.png
"""
from __future__ import annotations
import atexit, base64, ctypes, hashlib, io, json, os, re, shutil, subprocess, sys, time
from pathlib import Path

RESULT_SENTINEL = "__GCUA_RESULT__"  # stdout protocol marker: model code writing raw fd-1 bytes can't break the JSON line

ROOT = Path(os.environ.get("GCUA_ROOT") or Path(__file__).parent)
GDIR = ROOT / ".gcua"
STATE = GDIR / "state.json"

# ---------- todo ledger (5-state machine, ported from OpenGCUA TodoLedger) ----------
KINDS = {"task", "next_step", "fact", "question"}
STATUSES = {"pending", "attempted", "changed_unverified", "claimed_verified", "blocked"}
_ORDER = {"blocked": 0, "pending": 1, "attempted": 2, "changed_unverified": 3, "claimed_verified": 4}


class Todo:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    def update(self, id: str, kind: str | None = None, content: str = "", status: str | None = None) -> str:
        id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(id)).strip("-")[:80]
        if not id:
            return "ignored: empty id"
        existing = self.items.get(id, {})
        status = status or existing.get("status", "pending")
        if status == "delete":
            self.items.pop(id, None)
            return f"deleted {id}"
        kind = kind or existing.get("kind", "task")
        content = str(content).strip() or existing.get("content", "")
        if status not in STATUSES or kind not in KINDS or not content:
            return f"ignored {id}: status must be in {sorted(STATUSES)}, kind in {sorted(KINDS)}, content required"
        self.items[id] = {"id": id, "kind": kind, "content": content[:400],
                          "status": status}
        return f"ok {id}={status}"

    def list(self) -> list[dict]:
        # stable sort: same-status keeps insertion order
        return sorted(self.items.values(), key=lambda i: _ORDER.get(i["status"], 9))

    def render(self, last_fp: str) -> tuple[str, str]:
        items = self.list()
        if not items:
            return "(ledger empty)", ""
        fp = "\n".join(f"{i['id']}\t{i['status']}\t{i['content']}" for i in items)
        if fp == last_fp:
            counts = {}
            for i in items:
                counts[i["status"]] = counts.get(i["status"], 0) + 1
            return f"ledger unchanged ({len(items)} items: " + \
                ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) + ")", fp
        lines = [f"- [{i['status']}] ({i['kind']}) {i['id']}: {i['content']}" for i in items]
        return "## TODO Ledger\n" + "\n".join(lines), fp


# ---------- stall guard (fingerprint = normalized code + failing outcome) ----------
class Stall:
    def __init__(self, warn: int = 3, stop: int = 6, window: int = 12) -> None:
        self.warn, self.stop, self.window = warn, stop, window

    @staticmethod
    def fingerprint(code: str, rc: int, stderr_tail: str, stdout_tail: str = "") -> str:
        # rc!=0: same code + same failure = no progress (upstream rule).
        # rc==0: fold an stdout digest in, so a *changing* result is progress even for identical code;
        # identical successful output still accumulates (true spin) - but worker() keeps ok/exit truthful.
        norm = "\n".join(l.rstrip() for l in code.strip().splitlines() if l.strip())
        if rc:
            outcome = f"rc={rc}|{stderr_tail.strip().splitlines()[-1][:200]}"
        else:
            outcome = "out=" + hashlib.sha1(stdout_tail.encode("utf-8", "replace")).hexdigest()[:16]
        return hashlib.sha1((norm + "\x00" + outcome).encode("utf-8", "replace")).hexdigest()

    def record(self, hist: list[str], fp: str) -> str | None:
        hist.append(fp)
        del hist[:-50]
        recent = hist[-self.window:]
        occ = recent.count(fp)
        streak = 0
        for e in reversed(hist):
            if e != fp:
                break
            streak += 1
        worst = max(streak, occ)
        if worst < self.warn:
            return None
        if worst >= self.stop:
            return f"STALL STOP: identical step repeated {worst}x without progress. Run halted."
        shape = f"{streak}x in a row" if streak >= self.warn else f"{occ}x in last {len(recent)}"
        return (f"STALL WARNING: repeated {shape}. Observe state, change approach, or terminate.")


# ---------- worker: build helper namespace, exec model code, report JSON ----------
def _empty() -> dict:
    return {"todo": {}, "todo_fp": "", "stall": [], "step_no": 0, "steps": [], "steps_evicted": 0}


def _load() -> dict:
    if not STATE.exists():
        return _empty()
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # external corruption: quarantine the bad file, start fresh instead of bricking
        STATE.replace(STATE.with_suffix(".json.bad"))
        return _empty()


def _save(s: dict) -> None:
    GDIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, STATE)  # atomic: a crash can never leave a half-written state.json


def _cap_steps(s: dict) -> None:
    steps = s.setdefault("steps", [])
    if len(steps) > 200:
        # dropped-with-a-receipt (upstream ActionRiskIndex.evicted): silent cap = lying about history
        s["steps_evicted"] = s.get("steps_evicted", 0) + len(steps) - 200
        del steps[:-200]


def _kill_tree(pid: int) -> None:
    # taskkill /T takes the whole tree; a bare kill() would orphan the step's powershell/python grandchildren
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    # a lock is only stealable when BOTH its age exceeded STALE_S AND its owner pid is dead:
    # a legit long GUI step (up to 6min) must never have its state stolen out from under it
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)  # liveness probe only: no signal is delivered
            return True
        except OSError:
            return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            k32.CloseHandle(h)
            return True
        return False
    except Exception:  # non-Windows / weird: assume alive, never steal on a guess
        return True


def _acquire_lock(deadline_s: float = 10.0):
    """Single control session per working dir (upstream Authority/RESOURCE_BUSY analog):
    waits up to deadline_s, takes over only a stale lock (age > STALE_S 420s AND owner pid dead).
    STALE_S > every step timeout (engine 300s, host 360s): a slow-but-alive step is never declared dead.
    Ledger stays shared on purpose: one task board per dir."""
    GDIR.mkdir(parents=True, exist_ok=True)
    lock = GDIR / "state.lock"
    deadline = time.time() + deadline_s
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 420:
                    try:
                        dead = not _pid_alive(int(lock.read_text().strip() or "0"))
                    except (ValueError, OSError):
                        dead = True
                    if dead:
                        lock.unlink()
                        continue
            except FileNotFoundError:
                continue
            if time.time() > deadline:
                return None
            time.sleep(0.2)


def worker(code: str) -> dict:
    t0 = time.time()
    lock = _acquire_lock()
    if lock is None:
        return {"ok": False, "rc": 1, "error": f"BUSY: another gcua worker holds {GDIR / 'state.lock'} (RESOURCE_BUSY)",
                "stdout": "", "images": [], "ledger": "", "stall": None,
                "elapsed_s": round(time.time() - t0, 1), "step": None}
    atexit.register(lambda: lock.unlink(missing_ok=True))  # crash net: python-level death never orphans the lock
    s = _load()
    todo = Todo()
    todo.items = s.get("todo", {})
    shots: list[str] = []
    n = len(list(GDIR.glob("shot-*.png"))) if GDIR.exists() else 0
    buf_out, buf_err = io.StringIO(), io.StringIO()
    # step-level workflow FSM (ported from OpenGCUA run_agent checkpoint phases)
    s["step_no"] = s.get("step_no", 0) + 1
    rec = {"step": s["step_no"], "started": time.strftime("%H:%M:%S"),
           "phases": [{"p": "execute", "at": round(time.time() - t0, 2)}], "status": "running"}

    def phase(name: str) -> str:
        name = re.sub(r"[^a-z0-9_-]", "", str(name).lower())[:24]
        if not name:
            return "ignored: empty phase"
        rec["phases"].append({"p": name, "at": round(time.time() - t0, 2)})
        return f"phase → {name}"


    def screenshot() -> str:
        nonlocal n
        n += 1
        import mss
        p = GDIR / f"shot-{n:03d}.png"
        GDIR.mkdir(parents=True, exist_ok=True)
        with mss.mss() as sc:
            mons = sc.monitors
            # single screen: monitors[1] == the physical display; multi-screen: monitors[0]
            # is the full virtual-desktop union, matching pyautogui absolute coordinates
            m = sc.grab(mons[0] if len(mons) > 2 else mons[1])
            mss.tools.to_png(m.rgb, m.size, output=str(p))
        shots.append(str(p))
        return str(p)

    def ps(cmd: str, timeout: int = 60) -> dict:
        # -EncodedCommand (base64 UTF-16LE): the command string never passes through
        # argument-quote parsing, so there is no quoting/escaping/injection surface at all
        full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + str(cmd)
        enc = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
        r = subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", enc],
                           capture_output=True, text=True, timeout=timeout)
        return {"stdout": r.stdout, "stderr": r.stderr, "rc": r.returncode}

    def paste_text(text: str) -> None:
        # IME-proof text entry: clipboard + Ctrl+V (typewrite is mangled by Chinese IMEs)
        tmp = GDIR / "paste.tmp.txt"
        GDIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8-sig")
        safe = str(tmp).replace("'", "''")  # PowerShell: doubled quote escapes a literal quote
        ps(f"Get-Content -Raw '{safe}' | Set-Clipboard")
        tmp.unlink()
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.3)  # ProseMirror/async editors swallow Enter fired before the paste lands

    import pyautogui  # present on this machine; worker fails loud if GUI session missing
    pyautogui.FAILSAFE = True  # corner-abort retained: physical escape hatch
    try:
        _ig = __import__("PIL.ImageGrab", fromlist=["ImageGrab"])  # pillow optional: headless runners lack it
    except ImportError:
        _ig = None
    ns: dict = {
        "print": lambda *a, **k: print(*a, file=buf_out, **k),
        "time": time, "os": os, "json": json, "re": re, "Path": Path,
        "subprocess": subprocess,
        "todo": todo, "phase": phase, "screenshot": screenshot, "ps": ps, "paste_text": paste_text,
        "pyautogui": pyautogui, "ImageGrab": _ig,
    }
    class _MissingUIA:  # fail loud at first touch, not a silent None with a misleading NoneType error
        def __getattr__(self, _n):
            raise ImportError("pywinauto is not installed - run `pip install pywinauto` to use the uia lane")

    try:
        try:
            from pywinauto import Desktop as _D  # optional: UIA tree when installed
            ns["uia"] = _D(backend="uia")
        except ImportError:
            ns["uia"] = _MissingUIA()
        exec(compile(code, "<step>", "exec"), ns)  # ponytail: full-trust exec, same model as OpenGCUA worker
        rc, err = 0, ""
    except Exception as e:
        rc, err = 1, f"{type(e).__name__}: {e}"
    rec["phases"].append({"p": "complete" if rc == 0 else "failed", "at": round(time.time() - t0, 2)})
    rec["status"] = "completed" if rc == 0 else "failed"
    rec["elapsed_s"] = round(time.time() - t0, 1)
    s.setdefault("steps", []).append(rec)
    _cap_steps(s)  # ponytail: cap in state; full replay = trajectory files later
    out = buf_out.getvalue()[-4000:]
    errbuf = (err + buf_err.getvalue())[-2000:]
    fp = Stall.fingerprint(code, rc, errbuf, out)
    notice = Stall().record(s.setdefault("stall", []), fp)
    ledger, s["todo_fp"] = todo.render(s.get("todo_fp", ""))
    s["todo"] = todo.items
    _save(s)
    lock.unlink(missing_ok=True)  # release only after our state is durably saved
    trail = "→".join(p["p"] for p in rec["phases"])
    return {"ok": rc == 0, "rc": rc, "error": errbuf, "stdout": out, "images": shots,
            "ledger": ledger, "stall": notice, "elapsed_s": round(time.time() - t0, 1),
            "step": rec["step"], "phase_trail": trail, "step_status": rec["status"]}


# ---------- parent side ----------
def _spawn(code: str, timeout: int) -> subprocess.CompletedProcess:
    # worker stdout must be UTF-8: Windows pipes default to cp936 and JSON would mangle
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "GCUA_ROOT": str(ROOT)}
    proc = subprocess.Popen([sys.executable, __file__, "--worker"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", env=env)
    try:
        out, err = proc.communicate(code, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        _kill_tree(proc.pid)  # run()-style kill() only reaps the direct child; grandchildren keep running
        out, err = proc.communicate()  # reap the corpse so no zombie/pipe survives
        e.output, e.stderr, e.pid = out[-4000:], err[-1500:], proc.pid
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def cmd_step(path: str) -> int:
    code = Path(path).read_text(encoding="utf-8")
    GDIR.mkdir(parents=True, exist_ok=True)
    timeout = max(1, int(os.environ.get("GCUA_STEP_TIMEOUT") or 300))  # override for tests/narrow boxes
    try:
        p = _spawn(code, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        # outcome_unknown (upstream action_journal semantics): killed-at-timeout is NOT "failed" and NOT
        # "didn't run" - clicks/writes/subprocesses may have landed before the kill. No lock taken here:
        # our worker is dead and its lock reaped below; a *different* live worker would only race the
        # atomic save (last-writer-wins), never corrupt it.
        lock = GDIR / "state.lock"
        try:
            if int(lock.read_text().strip()) == e.pid:
                lock.unlink(missing_ok=True)  # hard-killed worker's atexit never ran; reap OUR lock only
        except (OSError, ValueError):
            pass
        s = _load()
        s["step_no"] = s.get("step_no", 0) + 1
        s.setdefault("steps", []).append(
            {"step": s["step_no"], "started": time.strftime("%H:%M:%S"),
             "phases": [{"p": "execute", "at": 0.0}], "status": "timeout_unknown",
             "elapsed_s": timeout, "partial_stdout": (e.output or "")[-1500:]})
        _cap_steps(s)
        _save(s)
        print(json.dumps({"ok": False, "step": s["step_no"], "step_status": "timeout_unknown",
                          "error": f"step killed at {timeout}s; outcome UNKNOWN - side effects may have "
                                   f"landed; verify on-screen state before any retry",
                          "stdout": (e.output or "")[-4000:]}, ensure_ascii=False))
        return 1
    if p.returncode and not p.stdout.strip():
        print(json.dumps({"ok": False, "error": f"worker died rc={p.returncode}: {p.stderr[-800:]}"}))
        return 1
    cands = [l for l in p.stdout.splitlines() if l.startswith(RESULT_SENTINEL)]
    if cands:
        r = json.loads(cands[-1][len(RESULT_SENTINEL):])
    else:  # protocol breach (raw fd-1 noise, died mid-emit): report as unparseable WITH the raw bytes
        print(json.dumps({"ok": False, "error": "unparseable worker output (no result line)",
                          "raw_stdout": p.stdout[-1500:], "raw_stderr": p.stderr[-800:]}))
        return 1
    print(json.dumps(r, ensure_ascii=False))  # re-emit ONE clean JSON line for the host tool
    return 0 if r.get("ok") else 1  # exit mirrors the step's real outcome; stall notices never flip it


def cmd_status() -> int:
    s = _load()
    todo = Todo()
    todo.items = s.get("todo", {})
    ledger, _ = todo.render("")
    print(ledger)
    for r in s.get("steps", [])[-3:]:
        print(f"step {r['step']:03d} [{'→'.join(p['p'] for p in r['phases'])}] "
              f"{r['status']} {r.get('elapsed_s', '?')}s")
    unknown = sum(1 for r in s.get("steps", []) if r.get("status") == "timeout_unknown")
    print(f"stall history: {len(s.get('stall', []))} steps | timeout_unknown: {unknown} | "
          f"steps evicted by cap: {s.get('steps_evicted', 0)}")
    return 0


def cmd_reset() -> int:
    lock = _acquire_lock()
    if lock is None:
        print("BUSY: a worker holds the lock; reset refused")
        return 1
    _save(_empty())  # full empty state (not a bare {}): all keys present for downstream .get()s
    shutil.rmtree(GDIR / "steps", ignore_errors=True)  # one reset semantics everywhere: state + step scripts,
    lock.unlink(missing_ok=True)                        # screenshots kept on purpose (audit evidence)
    print("reset: state + step scripts cleared (screenshots kept for audit)")
    return 0


def _parse_worker(stdout: str) -> dict:
    lines = [l for l in stdout.splitlines() if l.startswith(RESULT_SENTINEL)]
    if not lines:
        raise RuntimeError(f"no sentinel line in worker output: {stdout[:300]!r}")
    return json.loads(lines[-1][len(RESULT_SENTINEL):])


def cmd_selftest() -> int:
    global ROOT, GDIR, STATE
    import shutil, tempfile
    tmp = Path(tempfile.mkdtemp(prefix="gcua-selftest-"))
    ROOT, GDIR, STATE = tmp, tmp / ".gcua", tmp / ".gcua" / "state.json"
    try:
        demo = '''
todo.update(id="s1", content="selftest step", status="attempted")
phase("verify")
print("hello", 1+1)
p = screenshot()
assert Path(p).exists() and Path(p).stat().st_size > 1000, "screenshot too small"
r = ps("echo gcua-ok")
assert r["rc"] == 0 and "gcua-ok" in r["stdout"], r
todo.update(id="s1", status="claimed_verified")
'''
        GDIR.mkdir(parents=True, exist_ok=True)
        p = _spawn(demo, timeout=120)
        if p.returncode != 0 or not p.stdout.strip():
            raise RuntimeError(f"worker boot failed: rc={p.returncode} "
                               f"stdout={p.stdout[:300]!r} stderr={p.stderr[-800:]!r}")
        r = _parse_worker(p.stdout)
        assert r["ok"], r
        assert "hello 2" in r["stdout"] and "claimed_verified" in r["ledger"], r
        assert r["images"] and Path(r["images"][0]).exists(), r
        assert r["step"] >= 1 and r["phase_trail"] == "execute→verify→complete", r
        # repeat same failing code 3x -> 3rd run must carry STALL WARNING (warn_at=3)
        bad = "raise ValueError('boom')"
        _spawn(bad, timeout=60)
        _spawn(bad, timeout=60)
        p2 = _spawn(bad, timeout=60)
        r2 = _parse_worker(p2.stdout)
        assert not r2["ok"] and "STALL" in (r2.get("stall") or ""), r2
        # second concurrent worker must get BUSY, not corrupt shared state
        lock = GDIR / "state.lock"
        lock.write_text("0")  # fresh mtime: not stale, must be honored
        p3 = _spawn("print('blocked')", timeout=30)
        r3 = _parse_worker(p3.stdout)
        assert not r3["ok"] and "BUSY" in r3["error"], r3
        lock.unlink()
        # corrupt state.json must self-heal, not brick the worker
        STATE.write_text("{corrupt", encoding="utf-8")
        s = _load()
        assert s["step_no"] == 0 and STATE.with_suffix(".json.bad").exists(), s
        # sentinel protocol: raw fd-1 writes in model code must not lose a successful result
        noisy = "import os\nos.write(1, b'raw-noise\\n')\nprint('after noise')"
        p4 = _spawn(noisy, timeout=60)
        line4 = [l for l in p4.stdout.splitlines() if l.startswith(RESULT_SENTINEL)]
        assert line4 and json.loads(line4[-1][len(RESULT_SENTINEL):])["ok"], p4.stdout[-400:]
        # identical SUCCEEDING steps: stall may fire STOP, but ok/exit must stay truthful
        for _ in range(6):
            last = _spawn("print('same')", timeout=60)
        r5 = json.loads([l for l in last.stdout.splitlines() if l.startswith(RESULT_SENTINEL)][-1][len(RESULT_SENTINEL):])
        assert r5["ok"] and "STALL STOP" in (r5.get("stall") or ""), r5
        # stale-lock takeover needs BOTH age > 420s AND a dead pid
        assert _pid_alive(os.getpid()) and not _pid_alive(999999), "pid liveness broken"
        # (a) timeout must kill the WHOLE tree, not just the worker: a spawned grandchild must die too
        (tmp / "grand.py").write_text(
            "import os, time\nopen(r'%s', 'w').write(str(os.getpid()))\ntime.sleep(60)\n" % (tmp / "grand.pid"),
            encoding="utf-8")
        (tmp / "slow.py").write_text(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, r'%s'])\n"
            "time.sleep(60)\n" % (tmp / "grand.py"),
            encoding="utf-8")
        try:
            _spawn((tmp / "slow.py").read_text(encoding="utf-8"), timeout=10)
            raise AssertionError("slow step did not time out")
        except subprocess.TimeoutExpired as te:
            assert isinstance(te.pid, int) and te.pid > 0, "killed pid not attached"
            for _ in range(40):  # grandchild writes its pid within ~1s of start
                if (tmp / "grand.pid").exists():
                    break
                time.sleep(0.25)
            gpid = int((tmp / "grand.pid").read_text().strip())
            for _ in range(20):  # taskkill needs a beat to land on the grandchild
                if not _pid_alive(gpid):
                    break
                time.sleep(0.25)
            assert not _pid_alive(gpid), f"grandchild {gpid} survived the tree kill"
        (GDIR / "state.lock").unlink(missing_ok=True)  # direct _spawn kill leaves the leaked lock
        # (b) cmd_step timeout: clean JSON, timeout_unknown bookkeeping, lock reaped, state recorded
        os.environ["GCUA_STEP_TIMEOUT"] = "5"
        try:
            import contextlib
            sbuf = io.StringIO()
            with contextlib.redirect_stdout(sbuf):
                rc_t = cmd_step(str(tmp / "slow.py"))
        finally:
            os.environ.pop("GCUA_STEP_TIMEOUT", None)
        rt = json.loads(sbuf.getvalue().strip().splitlines()[-1])
        assert rc_t == 1 and rt["step_status"] == "timeout_unknown" and "UNKNOWN" in rt["error"], rt
        s_t = _load()
        assert s_t["steps"][-1]["status"] == "timeout_unknown", s_t["steps"][-1]
        assert not (GDIR / "state.lock").exists(), "killed worker's lock was not reaped"
        print("selftest OK: exec, screenshot, ps, todo state machine, stall guard, busy lock, corruption heal, "
              "sentinel protocol, truthful stall exit, pid-aware lock, timeout outcome_unknown + tree kill")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


def main() -> int:
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if len(sys.argv) >= 2 and sys.argv[1] == "--worker":
        code = sys.stdin.read()
        print(RESULT_SENTINEL + json.dumps(worker(code), ensure_ascii=False))  # sentinel-prefixed: noise-proof line
        return 0
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    return {"step": lambda: cmd_step(arg), "status": cmd_status,
            "reset": cmd_reset, "selftest": cmd_selftest}.get(cmd, cmd_selftest)()


if __name__ == "__main__":
    sys.exit(main())
