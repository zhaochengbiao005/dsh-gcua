# gcua — Windows GCUA harness for DSH (v1)
# Python 3.13, no daemon: the DSH agent IS the model; this file is the sandbox+ledger+guard.
# deps: pillow, pyautogui (present), pywinauto optional for UIA tree.
"""Usage:
  python gcua.py step code.py     # execute one <code> step in an isolated worker
  python gcua.py status           # todo ledger + stall history
  python gcua.py reset            # wipe state
  python gcua.py selftest         # smoke-check all pieces
State: ./.gcua/state.json, screenshots: ./.gcua/shot-NNN.png
"""
from __future__ import annotations
import atexit, hashlib, io, json, os, re, subprocess, sys, time
from pathlib import Path

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
    def fingerprint(code: str, rc: int, stderr_tail: str) -> str:
        norm = "\n".join(l.rstrip() for l in code.strip().splitlines() if l.strip())
        outcome = f"rc={rc}|{stderr_tail.strip().splitlines()[-1][:200]}" if rc else ""
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
    return {"todo": {}, "todo_fp": "", "stall": [], "step_no": 0, "steps": []}


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


def _acquire_lock(deadline_s: float = 10.0):
    """Single control session per working dir (upstream Authority/RESOURCE_BUSY analog):
    waits up to deadline_s, takes over a stale lock (>5min, crashed worker).
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
                if time.time() - lock.stat().st_mtime > 300:
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
            m = sc.grab(sc.monitors[1])
            mss.tools.to_png(m.rgb, m.size, output=str(p))
        shots.append(str(p))
        return str(p)

    def ps(cmd: str, timeout: int = 60) -> dict:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + cmd],
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
    ns: dict = {
        "print": lambda *a, **k: print(*a, file=buf_out, **k),
        "time": time, "os": os, "json": json, "re": re, "Path": Path,
        "subprocess": subprocess,
        "todo": todo, "phase": phase, "screenshot": screenshot, "ps": ps, "paste_text": paste_text,
        "pyautogui": pyautogui, "ImageGrab": __import__("PIL.ImageGrab", fromlist=["ImageGrab"]),
    }
    try:
        try:
            from pywinauto import Desktop as _D  # optional: UIA tree when installed
            ns["uia"] = _D(backend="uia")
        except ImportError:
            ns["uia"] = None
        exec(compile(code, "<step>", "exec"), ns)  # ponytail: full-trust exec, same model as OpenGCUA worker
        rc, err = 0, ""
    except Exception as e:
        rc, err = 1, f"{type(e).__name__}: {e}"
    rec["phases"].append({"p": "complete" if rc == 0 else "failed", "at": round(time.time() - t0, 2)})
    rec["status"] = "completed" if rc == 0 else "failed"
    rec["elapsed_s"] = round(time.time() - t0, 1)
    s.setdefault("steps", []).append(rec)
    s["steps"] = s["steps"][-200:]  # ponytail: cap in state; full replay = trajectory files later
    out = buf_out.getvalue()[-4000:]
    errbuf = (err + buf_err.getvalue())[-2000:]
    fp = Stall.fingerprint(code, rc, errbuf)
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
    return subprocess.run([sys.executable, __file__, "--worker"], input=code,
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, env=env)


def cmd_step(path: str) -> int:
    code = Path(path).read_text(encoding="utf-8")
    GDIR.mkdir(parents=True, exist_ok=True)
    p = _spawn(code, timeout=300)
    if p.returncode and not p.stdout.strip():
        print(json.dumps({"ok": False, "error": f"worker died rc={p.returncode}: {p.stderr[-800:]}"}))
        return 1
    print(p.stdout.strip() or json.dumps({"ok": False, "error": "no worker output"}))
    r = json.loads(p.stdout.strip())
    return 0 if r.get("ok") and not (r.get("stall") or "").startswith("STALL STOP") else 1


def cmd_status() -> int:
    s = _load()
    todo = Todo()
    todo.items = s.get("todo", {})
    ledger, _ = todo.render("")
    print(ledger)
    for r in s.get("steps", [])[-3:]:
        print(f"step {r['step']:03d} [{'→'.join(p['p'] for p in r['phases'])}] "
              f"{r['status']} {r.get('elapsed_s', '?')}s")
    print(f"stall history: {len(s.get('stall', []))} steps")
    return 0


def cmd_reset() -> int:
    lock = _acquire_lock()
    if lock is None:
        print("BUSY: a worker holds the lock; reset refused")
        return 1
    _save({})
    lock.unlink(missing_ok=True)
    return 0


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
        r = json.loads(p.stdout.strip())
        assert r["ok"], r
        assert "hello 2" in r["stdout"] and "claimed_verified" in r["ledger"], r
        assert r["images"] and Path(r["images"][0]).exists(), r
        assert r["step"] >= 1 and r["phase_trail"] == "execute→verify→complete", r
        # repeat same failing code 3x -> 3rd run must carry STALL WARNING (warn_at=3)
        bad = "raise ValueError('boom')"
        _spawn(bad, timeout=60)
        _spawn(bad, timeout=60)
        p2 = _spawn(bad, timeout=60)
        r2 = json.loads(p2.stdout.strip())
        assert not r2["ok"] and "STALL" in (r2.get("stall") or ""), r2
        # second concurrent worker must get BUSY, not corrupt shared state
        lock = GDIR / "state.lock"
        lock.write_text("0")  # fresh mtime: not stale, must be honored
        p3 = _spawn("print('blocked')", timeout=30)
        r3 = json.loads(p3.stdout.strip())
        assert not r3["ok"] and "BUSY" in r3["error"], r3
        lock.unlink()
        # corrupt state.json must self-heal, not brick the worker
        STATE.write_text("{corrupt", encoding="utf-8")
        s = _load()
        assert s["step_no"] == 0 and STATE.with_suffix(".json.bad").exists(), s
        print("selftest OK: exec, screenshot, ps, todo state machine, stall guard, busy lock, corruption heal")
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
        print(json.dumps(worker(code), ensure_ascii=False))
        return 0
    cmd = sys.argv[1] if len(sys.argv) > 1 else "selftest"
    arg = sys.argv[2] if len(sys.argv) > 2 else ""
    return {"step": lambda: cmd_step(arg), "status": cmd_status,
            "reset": cmd_reset, "selftest": cmd_selftest}.get(cmd, cmd_selftest)()


if __name__ == "__main__":
    sys.exit(main())
