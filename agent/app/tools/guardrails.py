"""Static safety policy for remediation scripts (M2). Enforced in code, not in a prompt.

Verdicts:
  BLOCK  -- never executable, cannot be approved (destructive, unscoped, secrets, no rollback)
  REVIEW -- executable only after explicit human approval (anything that mutates state)
  ALLOW  -- read-only diagnostics
Every approved script is still re-checked by the executor right before it runs.
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field

from ..redact import find_secrets

RISK_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

# Binaries a remediation may call. Anything else is BLOCKed (and the sandbox
# only provides stubs for these, so an unlisted binary cannot run there either).
ALLOWED_BINARIES = {
    # orchestration / data plane
    "kubectl", "psql", "redis-cli", "rndc", "unbound-control", "dig", "openssl", "etcdctl",
    "vtysh", "netconf-cli", "gcloud", "crictl", "systemctl", "logrotate", "journalctl",
    "curl", "ssh", "certbot", "jq",
    # read-only / text utilities
    "df", "du", "find", "sort", "head", "tail", "grep", "awk", "cut", "wc", "date", "sleep",
    "cat", "tr", "truncate", "uniq", "xargs", "tee", "hostname",
}
SHELL_WORDS = {
    "set", "echo", "printf", "export", "local", "readonly", "true", "false", "test", "[", "[[",
    "]]", "]", "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case",
    "esac", "in", "exit", "return", "shift", "read", "trap", "wait", ":", "!", "{", "}", "(", ")",
    "function", "declare", "unset", "command", "cd", "pwd",
}


@dataclass
class Finding:
    rule: str
    severity: str        # BLOCK | HIGH | MEDIUM | LOW | WARN
    line: int
    snippet: str
    message: str


@dataclass
class GuardrailReport:
    verdict: str                              # ALLOW | REVIEW | BLOCK
    risk: str                                 # LOW | MEDIUM | HIGH
    findings: list[Finding] = field(default_factory=list)
    binaries: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict == "BLOCK"

    def summary(self) -> str:
        worst = [f for f in self.findings if f.severity in ("BLOCK", "HIGH")]
        return f"{self.verdict}/{self.risk}" + (f": {worst[0].message}" if worst else "")


# (rule, regex over a logical line, severity, message)
DENY_RULES: list[tuple[str, re.Pattern[str], str, str]] = [
    ("rm_root", re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|/\*|~|\$HOME|\*|\"?\$\{?\w+\}?\"?/?\*?)(\s|$|;)"),
     "BLOCK", "recursive delete of a root, home, wildcard or unguarded variable path"),
    ("rm_rf", re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f|\brm\s+-[a-zA-Z]*f[a-zA-Z]*r"),
     "BLOCK", "recursive force delete"),
    ("mkfs", re.compile(r"\bmkfs(\.\w+)?\b|\bwipefs\b|\bfdisk\b|\bparted\b"), "BLOCK", "filesystem/partition rewrite"),
    ("dd_device", re.compile(r"\bdd\b.*\bof=/dev/"), "BLOCK", "raw write to a block device"),
    ("power", re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b"), "BLOCK", "host power action"),
    ("fork_bomb", re.compile(r":\(\)\s*\{\s*:\s*\|\s*:"), "BLOCK", "fork bomb"),
    ("chmod_world", re.compile(r"\bchmod\s+(-R\s+)?0?777\b"), "BLOCK", "world-writable permissions"),
    ("pipe_to_shell", re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k)?sh\b"), "BLOCK", "remote code piped to a shell"),
    ("firewall_flush", re.compile(r"\biptables\s+(-F|--flush|-X)\b|\bnft\s+flush\b|\bufw\s+disable\b"), "BLOCK", "firewall flush"),
    ("k8s_delete_broad", re.compile(r"\bkubectl\b.*\bdelete\b.*(\b(ns|namespace|namespaces|node|nodes|pv|pvc|crd|clusterrole\w*)\b|--all\b)"),
     "BLOCK", "cluster-scope or bulk Kubernetes delete"),
    ("k8s_delete", re.compile(r"\bkubectl\b.*\bdelete\b"), "HIGH", "Kubernetes delete"),
    ("k8s_drain", re.compile(r"\bkubectl\b.*\b(drain|cordon)\b"), "HIGH", "node drain/cordon"),
    ("gcloud_delete", re.compile(r"\bgcloud\b.*\b(delete|destroy)\b"), "BLOCK", "cloud resource deletion"),
    ("terraform_destroy", re.compile(r"\bterraform\s+(destroy|apply\s+.*-destroy)\b"), "BLOCK", "infrastructure destroy"),
    ("git_force", re.compile(r"\bgit\s+push\b.*(--force|-f)\b"), "BLOCK", "force push"),
    ("sql_drop", re.compile(r"(?i)\bDROP\s+(DATABASE|SCHEMA|TABLE|USER|ROLE)\b"), "BLOCK", "SQL DROP of a database object"),
    # SQL keyword in caps or `truncate table`; coreutils `truncate -s` and prose don't match.
    ("sql_truncate", re.compile(r"\bTRUNCATE\s+(TABLE\s+)?[A-Za-z_\"][\w.\"]*|(?i:\btruncate\s+table\b)"),
     "BLOCK", "SQL TRUNCATE"),
    ("sql_delete_unscoped", re.compile(r"(?i)\bDELETE\s+FROM\s+[\w.\"]+\s*(;|\"|'|$)"), "BLOCK", "DELETE without WHERE"),
    ("sql_update_unscoped", re.compile(r"(?i)\bUPDATE\s+[\w.\"]+\s+SET\b(?![^;]*\bWHERE\b)"), "BLOCK", "UPDATE without WHERE"),
    ("pg_terminate_unscoped", re.compile(r"(?i)pg_terminate_backend\s*\((?![^;]*\bWHERE\b)"), "BLOCK",
     "pg_terminate_backend without a WHERE filter would kill every session"),
    ("pg_terminate", re.compile(r"(?i)pg_terminate_backend|pg_cancel_backend"), "HIGH", "terminates database sessions"),
    ("sql_alter_system", re.compile(r"(?i)\bALTER\s+(SYSTEM|ROLE|DATABASE)\b"), "HIGH", "database-wide configuration change"),
    ("redis_flush", re.compile(r"(?i)\b(FLUSHALL|FLUSHDB)\b"), "BLOCK", "wipes the cache"),
    ("redis_config", re.compile(r"(?i)\bCONFIG\s+SET\b"), "HIGH", "live Redis configuration change"),
    ("bgp_reset_hard", re.compile(r"\bclear\s+(ip\s+)?bgp\b(?!.*\bsoft\b)"), "BLOCK", "hard BGP reset drops all routes"),
    ("etcd_delete", re.compile(r"\betcdctl\b.*\b(del|delete)\b"), "BLOCK", "etcd key deletion"),
    ("sudo", re.compile(r"(^|[;&|]\s*)sudo\b"), "HIGH", "privilege escalation"),
]

MUTATION_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("k8s_mutation", re.compile(r"\bkubectl\b.*\b(scale|patch|apply|set|annotate|label|rollout\s+(restart|undo)|exec)\b"), "MEDIUM"),
    ("sql_write", re.compile(r"(?i)\b(INSERT|UPDATE|CREATE|ALTER|GRANT|REVOKE)\b"), "MEDIUM"),
    ("service_restart", re.compile(r"\bsystemctl\s+(restart|reload|stop|start)\b"), "MEDIUM"),
    ("file_mutation", re.compile(r"\b(truncate|logrotate|crictl\s+rmi|journalctl\s+--vacuum)"), "MEDIUM"),
    ("network_change", re.compile(r"\b(vtysh|netconf-cli|ssh)\b"), "MEDIUM"),
    ("cloud_change", re.compile(r"\bgcloud\b.*\b(update|create|set)\b"), "MEDIUM"),
    ("dns_change", re.compile(r"\b(rndc\s+(sign|reload)|unbound-control\s+(flush|reload))"), "MEDIUM"),
    ("etcd_change", re.compile(r"\betcdctl\b.*\b(compact|defrag|move-leader)\b"), "MEDIUM"),
]

IDEMPOTENCY_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("relative_arithmetic", re.compile(r"\$\(\(\s*\$?\w+\s*[*+]"), "value derived from current state; a rerun compounds it"),
    ("kubectl_create", re.compile(r"\bkubectl\b.*\bcreate\b(?!.*--dry-run)"), "kubectl create fails on rerun; use apply"),
    ("create_without_guard", re.compile(r"(?i)\bCREATE\s+(UNIQUE\s+)?(INDEX|TABLE)\b(?!\s+(CONCURRENTLY\s+)?IF\s+NOT\s+EXISTS)(?!\s+CONCURRENTLY\s+IF)"),
     "CREATE without IF NOT EXISTS fails on rerun"),
    ("append_redirect", re.compile(r"[^>]>>\s*[\w/$\"]"), "appending output grows on every rerun"),
]


def logical_lines(script: str) -> list[tuple[int, str]]:
    """Join backslash continuations, drop comments and blank lines; keep the first line number."""
    out: list[tuple[int, str]] = []
    buf, start = "", 0
    for n, raw in enumerate(script.splitlines(), start=1):
        line = raw.rstrip()
        if not buf:
            start = n
            if line.lstrip().startswith("#"):
                continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        if buf.strip():
            out.append((start, buf.strip()))
        buf = ""
    if buf.strip():
        out.append((start, buf.strip()))
    return out


def command_words(line: str) -> list[str]:
    """First word of every simple command on a line (split on ; && || | and $( )."""
    line = re.sub(r"\$\(\(.*?\)\)", "0", line)  # arithmetic is not a command
    # Command substitutions are commands too: lift them out (innermost first).
    inner: list[str] = []
    for rx in (re.compile(r"\$\(([^()]*)\)"), re.compile(r"`([^`]*)`")):
        while (m := rx.search(line)):
            inner.append(m.group(1))
            line = line[:m.start()] + "SUBST" + line[m.end():]
    words = [w for sub in inner for w in command_words(sub)]
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        toks = line.split()
    expect = True
    for t in toks:
        if t and set(t) <= set(";&|"):
            expect = True
            continue
        if expect:
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):  # VAR=value prefix
                continue
            words.append(t)
            expect = False
    return words


def check_script(script: str, *, rollback: str | None = None, require_rollback: bool = True) -> GuardrailReport:
    findings: list[Finding] = []
    binaries: set[str] = set()
    lines = logical_lines(script or "")

    if not lines:
        findings.append(Finding("empty", "BLOCK", 0, "", "script is empty"))

    for label in find_secrets(script or ""):
        findings.append(Finding("hardcoded_secret", "BLOCK", 0, label, f"script embeds a credential ({label})"))

    if not re.search(r"set\s+-[a-z]*e[a-z]*u[a-z]*o\s+pipefail|set\s+-euo\s+pipefail", script or ""):
        findings.append(Finding("strict_mode", "WARN", 1, "", "missing `set -euo pipefail`"))

    for n, line in lines:
        for word in command_words(line):
            base = word.rsplit("/", 1)[-1]
            if base in SHELL_WORDS or base.startswith("$") or re.match(r"^[\[\]{}!]+$", base):
                continue
            binaries.add(base)
            if base not in ALLOWED_BINARIES:
                findings.append(Finding("binary_not_allowed", "BLOCK", n, line[:120],
                                        f"`{base}` is not on the remediation allowlist"))
        for rule, rx, sev, msg in DENY_RULES:
            if rx.search(line):
                findings.append(Finding(rule, sev, n, line[:120], msg))
        for rule, rx, sev in MUTATION_RULES:
            if rx.search(line):
                findings.append(Finding(rule, sev, n, line[:120], "mutates state"))
        for rule, rx, msg in IDEMPOTENCY_RULES:
            if rx.search(line):
                findings.append(Finding(rule, "WARN", n, line[:120], msg))

    # A weaker rule already covered by a BLOCK on the same line is noise.
    blocked_lines = {f.line for f in findings if f.severity == "BLOCK"}
    findings = [f for f in findings if f.severity == "BLOCK" or f.line not in blocked_lines or f.severity == "WARN"]

    if require_rollback:
        if not rollback or not logical_lines(rollback):
            findings.append(Finding("no_rollback", "BLOCK", 0, "", "a state-changing fix needs a rollback"))
        else:
            rb = check_script(rollback, require_rollback=False)
            for f in rb.findings:
                if f.severity == "BLOCK":
                    findings.append(Finding(f"rollback_{f.rule}", "BLOCK", f.line, f.snippet, f"rollback: {f.message}"))

    return _verdict(findings, sorted(binaries))


def _verdict(findings: list[Finding], binaries: list[str]) -> GuardrailReport:
    if any(f.severity == "BLOCK" for f in findings):
        return GuardrailReport("BLOCK", "HIGH", findings, binaries)
    sev = {f.severity for f in findings}
    risk = "HIGH" if "HIGH" in sev else "MEDIUM" if "MEDIUM" in sev else "LOW"
    verdict = "ALLOW" if risk == "LOW" else "REVIEW"
    return GuardrailReport(verdict, risk, findings, binaries)
