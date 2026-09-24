"""Dry-run a remediation in isolation before anyone is asked to approve it.

- `bash -n` syntax check of script and rollback.
- Script runs twice against stateful stubs (PATH holds *only* stubs for the
  allowlisted binaries; no credentials in the environment). Identical calls on
  both runs = converges; different calls (e.g. replicas 6 then 12) = compounds.
- Rollback runs once afterwards and must succeed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from agent.app.redact import redact
from agent.app.tools.guardrails import ALLOWED_BINARIES

STUB = Path(__file__).resolve().parent / "stub.py"


class SandboxUnavailable(RuntimeError):
    pass


@dataclass
class RunResult:
    exit_code: int
    stdout: str
    stderr: str
    calls: list[dict] = field(default_factory=list)
    timed_out: bool = False


@dataclass
class SandboxReport:
    syntax_ok: bool
    syntax_error: str
    runs: list[RunResult]
    rollback: RunResult | None
    idempotent: bool
    missing_binaries: list[str]

    @property
    def passed(self) -> bool:
        return (self.syntax_ok and all(r.exit_code == 0 for r in self.runs)
                and self.idempotent and (self.rollback is None or self.rollback.exit_code == 0))

    def summary(self) -> str:
        if not self.syntax_ok:
            return f"syntax error: {self.syntax_error}"
        bad = [i + 1 for i, r in enumerate(self.runs) if r.exit_code != 0]
        parts = [f"runs ok={not bad}", f"idempotent={self.idempotent}"]
        if self.rollback is not None:
            parts.append(f"rollback ok={self.rollback.exit_code == 0}")
        if self.missing_binaries:
            parts.append(f"missing={','.join(self.missing_binaries)}")
        return " ".join(parts)


def find_bash() -> str:
    explicit = os.environ.get("SANDBOX_BASH")
    if explicit:
        return explicit
    if os.name == "nt":  # prefer Git Bash; System32\bash.exe is WSL and would escape the sandbox.
        # usr\bin\bash.exe, not the bin\bash.exe launcher (which prepends /usr/bin to PATH).
        for cand in (r"C:\Program Files\Git\usr\bin\bash.exe", r"C:\Program Files\Git\bin\bash.exe"):
            if Path(cand).exists():
                return cand
    found = shutil.which("bash")
    if not found or "system32" in found.lower():
        raise SandboxUnavailable("no suitable bash found (set SANDBOX_BASH)")
    return found


def _posix(p: Path) -> str:
    s = p.resolve().as_posix()
    if os.name == "nt" and len(s) > 1 and s[1] == ":":
        return f"/{s[0].lower()}{s[2:]}"
    return s


class Sandbox:
    def __init__(self, timeout_s: float = 60.0):
        self.bash = find_bash()
        self.timeout_s = timeout_s

    def _prepare(self, root: Path) -> dict[str, str]:
        bindir = root / "bin"
        bindir.mkdir()
        py = _posix(Path(sys.executable))
        stub = _posix(STUB)
        for b in ALLOWED_BINARIES:
            f = bindir / b
            f.write_text(f'#!/bin/sh\nexec "{py}" "{stub}" {b} "$@"\n', encoding="utf-8", newline="\n")
            f.chmod(0o755)
        env = {
            "PATH": _posix(bindir),
            "SANDBOX_PATH": _posix(bindir),
            "HOME": _posix(root),
            "TMPDIR": _posix(root),
            "SANDBOX_STATE": str(root / "state.json"),
            "SANDBOX_LOG": str(root / "calls.jsonl"),
            "LANG": "C.UTF-8",
            "DRY_RUN": "1",
        }
        if os.name == "nt":  # the Windows loader needs these to start python.exe
            for k in ("SYSTEMROOT", "WINDIR", "COMSPEC"):
                if k in os.environ:
                    env[k] = os.environ[k]
        return env

    def _exec(self, root: Path, env: dict[str, str], name: str, body: str, run_tag: str) -> RunResult:
        path = root / name
        path.write_text(body, encoding="utf-8", newline="\n")
        log = Path(env["SANDBOX_LOG"])
        before = log.read_text(encoding="utf-8").count("\n") if log.exists() else 0
        try:
            # Re-pin PATH inside the shell in case a bash launcher prepended system dirs.
            cmd = [self.bash, "--noprofile", "--norc", "-c",
                   'PATH="$SANDBOX_PATH"; export PATH; hash -r; . "$1"', "sandbox", _posix(path)]
            p = subprocess.run(cmd, cwd=root, env={**env, "SANDBOX_RUN": run_tag},
                               capture_output=True, text=True, timeout=self.timeout_s)
            code, out, err, timed_out = p.returncode, p.stdout, p.stderr, False
        except subprocess.TimeoutExpired as exc:
            code, out, err, timed_out = 124, exc.stdout or "", f"timed out after {self.timeout_s}s", True
        lines = log.read_text(encoding="utf-8").splitlines()[before:] if log.exists() else []
        calls = [json.loads(l) for l in lines]
        return RunResult(code, redact(str(out)[-4000:]), redact(str(err)[-4000:]),
                         [{"bin": c["bin"], "args": c["args"]} for c in calls], timed_out)

    def check_syntax(self, body: str) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "check.sh"
            f.write_text(body, encoding="utf-8", newline="\n")
            p = subprocess.run([self.bash, "-n", _posix(f)], capture_output=True, text=True, timeout=30)
            return p.returncode == 0, p.stderr.strip()[:500]

    def dry_run(self, script: str, rollback: str | None, runs: int = 2) -> SandboxReport:
        ok, err = self.check_syntax(script)
        if ok and rollback:
            ok, err = self.check_syntax(rollback)
            err = f"rollback: {err}" if not ok else err
        if not ok:
            return SandboxReport(False, err, [], None, False, [])

        with tempfile.TemporaryDirectory(prefix="sre-sandbox-") as d:
            root = Path(d)
            env = self._prepare(root)
            results = [self._exec(root, env, f"run{i}.sh", script, str(i)) for i in range(1, runs + 1)]
            rb = self._exec(root, env, "rollback.sh", rollback, "rollback") if rollback else None

        sequences = [[(c["bin"], tuple(c["args"])) for c in r.calls] for r in results]
        idempotent = all(s == sequences[0] for s in sequences[1:])
        missing = sorted({
            line.split(":")[-2].strip() for r in [*results, *([rb] if rb else [])]
            for line in r.stderr.splitlines() if "command not found" in line and ":" in line
        })
        return SandboxReport(True, "", results, rb, idempotent, missing)
