// GCUA agent state machine — DSH Host plugin.
// Tools: gcua_step / gcua_status / gcua_reset. Engine: engine/gcua.py (Python).
// State lives in the session working directory under .gcua/state.json.
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ENGINE = path.join(path.dirname(fileURLToPath(import.meta.url)), 'engine', 'gcua.py');
const PYTHON = process.env.GCUA_PYTHON || 'python';
const STEP_TIMEOUT_MS = 360_000;

const stateOf = (root) => {
  try {
    return JSON.parse(fs.readFileSync(path.join(root, '.gcua', 'state.json'), 'utf8'));
  } catch {
    return null;
  }
};

const tail = (s, n) => (s.length > n ? '…' + s.slice(-n) : s);

// engine.kill() on Windows leaves the worker tree alive (it keeps the lock + moves the mouse).
// TerminateProcess has no propagation — kill the whole tree instead.
const killTree = (child) => {
  if (process.platform === 'win32' && child.pid) {
    spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true });
  } else {
    child.kill();
  }
};

function runEngine(args, root, signal, timeoutMs) {
  return new Promise((resolve) => {
    const child = spawn(PYTHON, [ENGINE, ...args], {
      cwd: root,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', GCUA_ROOT: root },
      windowsHide: true,
    });
    let out = '', err = '', done = false;
    const finish = (code, note) => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve({ code, stdout: out, stderr: err, note });
    };
    const timer = setTimeout(() => {
      killTree(child);
      finish(-1, `timeout after ${timeoutMs}ms`);
    }, timeoutMs);
    const onAbort = () => {
      killTree(child);
      finish(-1, 'aborted');
    };
    if (signal.aborted) return onAbort();
    signal.addEventListener('abort', onAbort, { once: true });
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (err += d));
    child.on('error', (e) => finish(-1, `spawn failed: ${e.message}`));
    child.on('close', (code) => {
      signal.removeEventListener('abort', onAbort);
      finish(code ?? -1);
    });
  });
}

const ledgerLines = (st) => {
  const items = Array.isArray(st?.todo)
    ? st.todo
    : Object.values(st?.todo ?? {});
  if (items.length === 0) return '(ledger empty)';
  const rank = { blocked: 0, pending: 1, attempted: 2, changed_unverified: 3, claimed_verified: 4 };
  return items
    .slice()
    .sort((a, b) => (rank[a.status] ?? 9) - (rank[b.status] ?? 9))
    .map((t) => `• [${t.status ?? '?'}] ${t.content ?? ''}${t.kind ? ` (${t.kind})` : ''}`)
    .join('\n');
};

const stepLines = (st) => {
  const steps = Array.isArray(st?.steps) ? st.steps : [];
  if (steps.length === 0) return '(no steps yet)';
  return steps
    .slice(-3)
    .map((r) => `step ${String(r.step).padStart(3, '0')} [${r.phases.map((p) => p.p).join('→')}] ${r.status} ${r.elapsed_s ?? '?'}s`)
    .join('\n');
};

export const inject = ['tools', 'systemPrompt', 'workingDirectory'];

export function apply(ctx) {
  ctx.tools.register({
    name: 'gcua_step',
    description:
      'Execute one GCUA step: run a batched Python macro-action in the sandbox (fresh process per step; files and applications persist, variables do not). ' +
      'Available namespace: todo (Todo.update(id, kind, content, status) ledger), phase(name) mark workflow phase (plan/observe/verify/recovery…, execute→complete auto), screenshot() -> .gcua/shot-NNN.png, ps(cmd, timeout) powershell, paste_text(text) IME-proof clipboard typing, pyautogui, uia (pywinauto Desktop if installed), plus stdlib. ' +
      'Ledger and stall guard update automatically. Batch a whole macro-action (find -> act -> verify) into one step; poll Text-only at 0.3-0.5s; screenshot only at verdict.',
    parameters: {
      type: 'object',
      properties: {
        intent: { type: 'string', description: 'One line: what this step accomplishes.' },
        code: {
          type: 'string',
          description: 'Complete Python source for this step. No imports needed beyond stdlib.',
        },
      },
      required: ['intent', 'code'],
      additionalProperties: false,
    },
    output: {
      schema: { type: 'object' },
      render(args, value) {
        const v = value ?? {};
        const lines = [
          `intent: ${args?.intent ?? ''}`,
          `exit: ${v.exitCode}${v.note ? ` (${v.note})` : ''}`,
          '',
          v.stdout ? tail(String(v.stdout), 6000) : '(no stdout)',
        ];
        if (v.stderr && String(v.stderr).trim()) lines.push('', 'stderr: ' + tail(String(v.stderr), 2000));
        lines.push('', 'ledger:', ledgerLines(v.state));
        return [{ type: 'text', text: lines.join('\n') }];
      },
    },
    execute: async (args, exec) => {
      const root = await ctx.workingDirectory.ensure(exec.agent, exec.signal);
      const stepsDir = path.join(root, '.gcua', 'steps');
      fs.mkdirSync(stepsDir, { recursive: true });
      const stepFile = path.join(stepsDir, `step-${Date.now()}.py`);
      fs.writeFileSync(stepFile, String(args.code ?? ''), 'utf8');
      const r = await runEngine(['step', stepFile], root, exec.signal, STEP_TIMEOUT_MS);
      return {
        ok: r.code === 0,
        exitCode: r.code,
        ...(r.note ? { note: r.note } : {}),
        stdout: r.stdout,
        stderr: r.stderr,
        stepFile,
        state: stateOf(root),
      };
    },
    timeoutMs: STEP_TIMEOUT_MS + 5_000,
    isConcurrencySafe: () => false,
    presentCall: (args) => ({
      card: 'generic',
      title: `gcua_step: ${String(args?.intent ?? '').slice(0, 90)}`,
      kind: 'execute',
    }),
  });

  ctx.tools.register({
    name: 'gcua_status',
    description:
      'Read the GCUA state machine for the session working directory: todo ledger (kinds task|next_step|fact|question; statuses pending -> attempted -> changed_unverified -> claimed_verified | blocked) and stall history.',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: {
      schema: { type: 'object' },
      render(_args, value) {
        const st = value?.state ?? {};
        return [{ type: 'text', text: [ledgerLines(st), '', stepLines(st), `stall history: ${(st.stall ?? []).length} steps`].join('\n') }];
      },
    },
    execute: async (_args, exec) => {
      const root = await ctx.workingDirectory.ensure(exec.agent, exec.signal);
      return { state: stateOf(root) };
    },
    isConcurrencySafe: () => true,
  });

  ctx.tools.register({
    name: 'gcua_reset',
    description:
      'Clear the GCUA ledger and stall history for the session working directory (start a new task).',
    parameters: { type: 'object', properties: {}, additionalProperties: false },
    output: {
      schema: { type: 'object' },
      render(_args, value) {
        return [{ type: 'text', text: value?.reset ? 'GCUA state reset.' : 'No GCUA state to reset.' }];
      },
    },
    execute: async (_args, exec) => {
      const root = await ctx.workingDirectory.ensure(exec.agent, exec.signal);
      const file = path.join(root, '.gcua', 'state.json');
      const existed = fs.existsSync(file);
      if (existed) fs.rmSync(file);
      fs.rmSync(path.join(root, '.gcua', 'steps'), { recursive: true, force: true }); // step scripts are per-task scratch
      return { reset: existed, path: file };
    },
    isConcurrencySafe: () => false, // never overlap a running step: its final _save would resurrect the ledger
  });

  ctx.systemPrompt.section({
    name: 'gcua-machine',
    order: 850,
    text: [
      '## GCUA step machine (gcua tools)',
      '',
      'Drive real GUI automation through `gcua_step`. One step = one intent + one Python macro-action, batched. The sandbox runs it in a fresh process: files and applications persist, variables do not.',
      '',
      '- Ledger discipline: `todo.update(id, kind, content, status)` — kinds `task|next_step|fact|question`, statuses `pending -> attempted -> changed_unverified -> claimed_verified` (or `blocked`). Omitted fields inherit. Claim `claimed_verified` only after independent proof (read-back, UIA re-read, clipboard round-trip, pixel check).',
      '- A step that moves no ledger item to a new status is waste: batch more into it (find -> act -> verify in one step).',
      '- Poll with Text-only reads (uia dumps, ps output) at 0.3-0.5s. `screenshot()` only at verdict.',
      '- Never trust one observation source: take coordinates from UIA rects, not eyeballing; screenshots cannot capture GPU-composited windows; `get_value()` lies for rich editors (use `window_text()` or a round-trip).',
      '- Stall guard: an identical (code, result) fingerprint warns at 3 repeats and stops at 6 (window 12). On warn, change approach and `todo.update` the plan; never resubmit the same code.',
      '- Workflow phases: each step auto-records execute→complete|failed into state.json; call `phase("verify")` etc. inside the code to mark plan/observe/verify/recovery transitions — gcua_status renders the last 3 trails.',
      '- Web lane: no browser worker needed — drive agent-browser CLI via `ps()` (agent-browser open/snapshot -i/click @eN/fill/close); Playwright lane = same trick if agent-browser is absent.',
      '- Keep the ledger reachable: on plan change, update or delete stale items instead of abandoning them.',
    ].join('\n'),
  });
}
