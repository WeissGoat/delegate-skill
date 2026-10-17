"""Hand a self-contained task to a cheaper worker agent and keep only its report.

  python delegate.py run <name> <brief.md | -> [options]     (the word `run` may be left out)
  python delegate.py profiles                                 list profiles and whether their CLI is installed
  python delegate.py probe [--profile P]                      ten-second health check of each profile in the chain

Options for run:
  --profile P[,P2...]   worker profile chain; the next one is tried when a profile is rate limited, missing or
                        returns nothing (default: "default" in profiles.json, or env DELEGATE_PROFILE)
  --mode read|full      read = sandboxed, may not write anything (log triage, code or doc review);
                        full = may run programs and write scratch files (drive a build, run a sweep). Default full.
  --rules FILE          project rules appended to the base rules; may repeat. Also read from env DELEGATE_RULES.
  --cwd DIR             project root the worker runs in (default: the git root of the current directory)
  --out DIR             where reports go (default: <cwd>/tmp/agent_reports; must be git-ignored)
  --timeout MIN         kill the worker after this many minutes (default 30)
  --lock NAME           take an exclusive lock (e.g. unity, build) so two workers never drive the same thing
  --quiet               print only the report path and its verdict line

Writes <out>/<time>_<name>.md (the report), .task.md (what the worker got), .log (worker stdout/stderr) and, if
files changed while it ran, .changes.patch. Exit code: 0 PASS, 1 FAIL, 2 could not finish or no worker answered,
3 usage error or lock busy.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
RETRYABLE = re.compile(r"429|too many requests|rate.?limit|exceeded retry limit|quota|overloaded|503|502|"
                       r"unavailable|timed? ?out", re.I)
VERDICT = re.compile(r"^\s*(?:\*\*)?(?:结论|Verdict)(?:\*\*)?\s*[:：]\s*(?:\*\*)?\s*(PASS|FAIL|无法完成|BLOCKED)", re.I | re.M)

BASE_RULES = """You are a worker agent. A lead agent handed you the task below and will read only your final answer,
so the answer must stand on its own. The working directory is the project root.

Rules:
- {mode_rule}
- Do not commit, and do not touch git branches, stashes or the index.
- Stay inside the brief. If something blocks you, stop and report it instead of working around it or guessing.
- Every finding needs evidence: the exact command or file:line, and a short excerpt of what it printed or says.
  Say plainly what you observed and what you only infer.
- Screenshots, if any, go under {out} with absolute paths. List their paths in the answer but make no claims
  about what they show unless the brief asks; the lead agent looks at them itself. Get facts from text output.
- Do not narrate your process. The final answer is at most about 80 lines, in Chinese, in this shape:
  结论: PASS / FAIL / 无法完成 (one line why)
  发现: numbered list; each item = what you saw, the evidence, how sure you are (高/中/低).
  没做到的: what you could not check and why.
"""
MODE_RULES = {
    "read": "Read only. Do not create, edit or delete any file, and do not start programs that change state.",
    "full": "Report only. Do not edit, create or delete files that git tracks. Scratch files go under {out} only.",
}


# ---------- profiles ----------

def load_profiles():
    data = json.loads((SKILL / "profiles.json").read_text(encoding="utf-8"))
    local = SKILL / "profiles.local.json"  # per-machine additions and overrides, not in git
    if local.exists():
        extra = json.loads(local.read_text(encoding="utf-8"))
        data["profiles"].update(extra.get("profiles", {}))
        if "default" in extra:
            data["default"] = extra["default"]
    return data


def chain_for(spec, data):
    spec = spec or os.environ.get("DELEGATE_PROFILE") or "default"
    names = []
    for part in spec.split(","):
        part = part.strip()
        names += data["default"] if part == "default" else [part]
    unknown = [n for n in names if n not in data["profiles"]]
    if unknown:
        sys.exit(f"Unknown profile(s): {', '.join(unknown)}. Known: {', '.join(data['profiles'])}")
    return names


def build_command(profile, prompt, report, cwd, mode):
    engine, model = profile["engine"], profile.get("model")
    if engine == "codex":
        cmd = ["codex", "exec", "-C", str(cwd), "--skip-git-repo-check", "--ephemeral", "-o", str(report),
               "-s", "read-only" if mode == "read" else "danger-full-access"]
        if model:
            cmd += ["-m", model]
        if profile.get("instructions"):
            # Replaces Codex's own ~20k-character system prompt; Gemini on the relay answers that prompt with
            # RESOURCE_EXHAUSTED, while the same task under a short prompt works.
            path = (SKILL / profile["instructions"]).as_posix()
            cmd += ["-c", f"model_instructions_file='{path}'"]
        for key, value in profile.get("config", {}).items():
            cmd += ["-c", f"{key}={value}"]
        return cmd + [prompt]
    if engine == "claude":
        return ["claude", "-p", "--model", model or "haiku",
                "--permission-mode", "plan" if mode == "read" else "bypassPermissions", prompt]
    if engine == "cmd":  # any other headless harness, e.g. opencode run -m {model} {prompt}
        args = profile["argv"] if mode == "full" else profile.get("argv_read", profile["argv"])
        return [a.format(prompt=prompt, model=model or "", cwd=cwd, report=report) for a in args]
    sys.exit(f"Unknown engine {engine}.")


def resolve(cmd):
    exe = shutil.which(cmd[0])  # codex, gemini, opencode are .cmd shims on Windows
    return [exe] + cmd[1:] if exe else None


# ---------- git snapshot ----------

def git(cwd, *args):
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out.stdout.strip() if out.returncode == 0 else None


def snapshot(cwd):
    if git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
        return None
    head = git(cwd, "rev-parse", "HEAD")
    # `stash create` records the working tree as a commit without touching anything; empty when the tree is clean.
    base = git(cwd, "stash", "create") or head
    untracked = {}
    for path in (git(cwd, "ls-files", "--others", "--exclude-standard", "-z") or "").split("\0"):
        if path:
            try:
                st = (Path(cwd) / path).stat()
                untracked[path] = (st.st_size, st.st_mtime_ns)
            except OSError:
                pass
    return {"head": head, "branch": git(cwd, "branch", "--show-current"), "base": base, "untracked": untracked,
            "raw": raw_diff(cwd, base)}


def raw_diff(cwd, base):
    # Measured against the same base before and after; only lines that differ count. Submodules whose checkout
    # differs from HEAD show up in both (stash create does not record their pointer), so they cancel out.
    return set((git(cwd, "diff", "--raw", "--no-abbrev", base) or "").splitlines()) if base else set()


def changes_since(cwd, before):
    after = snapshot(cwd)
    notes, patch = [], ""
    if after["head"] != before["head"] or after["branch"] != before["branch"]:
        notes.append(f"HEAD moved: {before['branch']}@{(before['head'] or '')[:7]} -> "
                     f"{after['branch']}@{(after['head'] or '')[:7]}")
    new = raw_diff(cwd, before["base"]) - before["raw"]
    paths = sorted({line.split("\t", 1)[1] for line in new if "\t" in line})
    if paths:
        notes += ["tracked " + path for path in paths]
        patch = git(cwd, "diff", "--binary", before["base"], "--", *paths) or ""
    for path, sig in after["untracked"].items():
        if before["untracked"].get(path) != sig:
            notes.append(("new untracked " if path not in before["untracked"] else "changed untracked ") + path)
    for path in before["untracked"].keys() - after["untracked"].keys():
        notes.append("removed untracked " + path)
    return notes, patch


# ---------- locks ----------

def pid_alive(pid):
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def take_lock(out, name, label):
    lock = out / f".lock-{name}"
    if lock.exists():
        try:
            held = json.loads(lock.read_text(encoding="utf-8"))
            if pid_alive(held["pid"]):
                return None, f"lock '{name}' is held by {held['label']} (pid {held['pid']}, since {held['since']})"
        except (ValueError, KeyError):
            pass
    lock.write_text(json.dumps({"pid": os.getpid(), "label": label, "since": time.strftime("%H:%M:%S")}),
                    encoding="utf-8")
    return lock, None


# ---------- running ----------

def kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.kill()


def run_worker(cmd, cwd, log, deadline):
    with log.open("ab") as sink:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=sink)
        try:
            stdout, _ = proc.communicate(timeout=max(1, deadline - time.time()))
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            stdout, _ = proc.communicate()
            timed_out = True
        sink.write(b"\n--- stdout ---\n" + (stdout or b""))
    return proc.returncode, stdout.decode("utf-8", errors="replace"), timed_out


def run(args):
    opts, pos = parse(args, {"--profile", "--mode", "--rules", "--cwd", "--out", "--timeout", "--lock"}, {"--quiet"})
    if len(pos) != 2:
        print(__doc__)
        return 3
    name, brief_arg = pos
    mode = opts.get("--mode", ["full"])[-1]
    if mode not in MODE_RULES:
        sys.exit("--mode is read or full")
    cwd = Path(opts["--cwd"][-1]).resolve() if "--cwd" in opts else Path(git(Path.cwd(), "rev-parse", "--show-toplevel") or Path.cwd())
    out = Path(opts["--out"][-1]).resolve() if "--out" in opts else cwd / "tmp" / "agent_reports"
    out.mkdir(parents=True, exist_ok=True)
    timeout = float(opts.get("--timeout", ["30"])[-1]) * 60
    data = load_profiles()
    chain = chain_for(opts.get("--profile", [None])[-1], data)

    brief = sys.stdin.buffer.read().decode("utf-8") if brief_arg == "-" else Path(brief_arg).read_text(encoding="utf-8")
    rule_files = opts.get("--rules", []) + [p for p in os.environ.get("DELEGATE_RULES", "").split(os.pathsep) if p]
    project_rules = "".join(f"\n# Project rules ({Path(p).name})\n" + Path(p).read_text(encoding="utf-8")
                            for p in rule_files)

    stem = f"{time.strftime('%Y%m%d_%H%M%S')}_{name}"
    report, log = out / f"{stem}.md", out / f"{stem}.log"
    if git(cwd, "check-ignore", "-q", str(report)) is None and git(cwd, "rev-parse", "--git-dir"):
        print(f"WARNING: {out} is not git-ignored; reports may end up in commits.", file=sys.stderr)

    lock = None
    if "--lock" in opts:
        lock, busy = take_lock(out, opts["--lock"][-1], stem)
        if busy:
            print(busy, file=sys.stderr)
            return 3
    try:
        # The task travels as a UTF-8 file: Chinese passed through Windows .cmd shims (argv or stdin) arrives
        # garbled, so the command line only carries an ASCII pointer to it.
        task = out / f"{stem}.task.md"
        rules = BASE_RULES.format(mode_rule=MODE_RULES[mode].format(out=out.as_posix()), out=out.as_posix())
        task.write_text(rules + project_rules + "\n# Brief\n" + brief, encoding="utf-8")
        try:
            pointer = task.relative_to(cwd).as_posix()
        except ValueError:
            pointer = task.as_posix()
        prompt = f"Read {pointer} (UTF-8) and do exactly what it says."

        before = snapshot(cwd)
        started, deadline = time.time(), time.time() + timeout
        attempts, timed_out = [], False
        for pname in chain:
            profile = data["profiles"][pname]
            cmd = resolve(build_command(profile, prompt, report, cwd, mode))
            if not cmd:
                attempts.append(f"{pname}: {profile['engine']} CLI not found")
                continue
            if time.time() >= deadline:
                attempts.append(f"{pname}: skipped, out of time")
                break
            with log.open("ab") as sink:
                sink.write(f"\n=== {pname} {time.strftime('%H:%M:%S')} ===\n".encode())
            t0 = time.time()
            code, stdout, timed_out = run_worker(cmd, cwd, log, deadline)
            if not report.exists() or not report.read_text(encoding="utf-8", errors="replace").strip():
                if stdout.strip() and code == 0:
                    report.write_text(stdout, encoding="utf-8")
            answered = report.exists() and report.read_text(encoding="utf-8", errors="replace").strip()
            attempts.append(f"{pname} ({profile['engine']} {profile.get('model') or 'default'}): exit={code} "
                            f"{int(time.time() - t0)}s" + (" TIMEOUT" if timed_out else ""))
            if answered or timed_out:
                break
            tail = log.read_text(encoding="utf-8", errors="replace")[-3000:]
            if not RETRYABLE.search(tail) and code == 0:
                break  # it ran and chose to say nothing; another model will not fix the brief

        answered = report.exists() and report.read_text(encoding="utf-8", errors="replace").strip()
        if not answered:
            tail = log.read_text(encoding="utf-8", errors="replace")[-2000:] if log.exists() else ""
            report.write_text("结论: 无法完成 (no worker produced an answer)\n\n```\n" + tail + "\n```\n",
                              encoding="utf-8")
        elif timed_out:
            report.write_text("结论: 无法完成 (worker timed out; partial answer below)\n\n" + answered, encoding="utf-8")

        footer = [f"\n\n---\nmode={mode} total={int(time.time() - started)}s"] + attempts
        if before:
            notes, patch = changes_since(cwd, before)
            if notes:
                if patch:
                    (out / f"{stem}.changes.patch").write_text(patch, encoding="utf-8")
                # Anyone's edits show up here, including the lead agent's own while the worker ran.
                footer.append("WARNING: the working tree changed during the run (worker or anyone else)"
                              + (f"; diff in {stem}.changes.patch" if patch else "") + ":")
                footer += ["  " + n for n in notes[:40]]
        with report.open("a", encoding="utf-8") as f:
            f.write("\n".join(footer) + "\n")
    finally:
        if lock:
            lock.unlink(missing_ok=True)

    text = report.read_text(encoding="utf-8")
    match = VERDICT.search(text)
    verdict = match.group(1).upper() if match else "?"
    print(report)
    if "--quiet" in opts:
        first = next((l for l in text.splitlines() if VERDICT.match(l)), "结论: ? (no verdict line)")
        print(first.strip())
        print("\n".join(l for l in footer if "WARNING" in l))
    else:
        print(text)
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


# ---------- other commands ----------

def profiles(_args):
    data = load_profiles()
    print("default chain:", ",".join(data["default"]))
    for name, p in data["profiles"].items():
        exe = {"codex": "codex", "claude": "claude"}.get(p["engine"]) or p.get("argv", ["?"])[0]
        found = "ok" if shutil.which(exe) else "MISSING " + exe
        print(f"  {name:14} {p['engine']:6} {p.get('model') or 'default':26} {found}  {p.get('note', '')}")
    return 0


def probe(args):
    opts, _ = parse(args, {"--profile"}, set())
    data = load_profiles()
    cwd = Path(git(Path.cwd(), "rev-parse", "--show-toplevel") or Path.cwd())
    out = Path(os.environ.get("TEMP", "/tmp")) / "delegate_probe"
    out.mkdir(parents=True, exist_ok=True)
    ok = False
    for pname in chain_for(opts.get("--profile", [None])[-1], data):
        report = out / f"{pname}.txt"
        report.unlink(missing_ok=True)
        cmd = resolve(build_command(data["profiles"][pname], "Reply with exactly: READY", report, cwd, "read"))
        if not cmd:
            print(f"{pname}: CLI not found")
            continue
        t0 = time.time()
        code, stdout, timed_out = run_worker(cmd, cwd, out / f"{pname}.log", time.time() + 120)
        answer = report.read_text(encoding="utf-8").strip() if report.exists() else stdout.strip()
        reason = "" if "READY" in answer else (RETRYABLE.search((out / f"{pname}.log").read_text(
            encoding="utf-8", errors="replace")[-3000:]) or [""])[0]
        print(f"{pname}: {'ready' if 'READY' in answer else 'FAILED ' + str(reason or 'exit=' + str(code))}"
              f" {int(time.time() - t0)}s")
        ok = ok or "READY" in answer
    return 0 if ok else 2


def parse(args, valued, flags):
    opts, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a in valued and i + 1 < len(args):
            opts.setdefault(a, []).append(args[i + 1])
            i += 2
        elif a in flags:
            opts[a] = [True]
            i += 1
        elif a.startswith("--") and a != "-":
            sys.exit(f"Unknown option {a}")
        else:
            pos.append(a)
            i += 1
    return opts, pos


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    commands = {"run": run, "profiles": profiles, "probe": probe}
    if argv[0] in commands:
        return commands[argv[0]](argv[1:])
    return run(argv)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
