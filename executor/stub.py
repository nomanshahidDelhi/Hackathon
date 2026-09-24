"""Stand-in for every allowlisted binary inside the sandbox.

Records each call and keeps a little state (replica counts, config values) so a
script run twice reveals whether it converges or compounds. Never touches the
real system: it only reads/writes files under $SANDBOX_DIR.
"""
import json
import os
import re
import sys

name, args = sys.argv[1], sys.argv[2:]
state_path = os.environ["SANDBOX_STATE"]
log_path = os.environ["SANDBOX_LOG"]
run = os.environ.get("SANDBOX_RUN", "0")

try:
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)
except (OSError, ValueError):
    state = {}

with open(log_path, "a", encoding="utf-8") as f:
    f.write(json.dumps({"run": run, "bin": name, "args": args}) + "\n")


def deployment(argv):
    for i, a in enumerate(argv):
        m = re.match(r"^(deploy|deployment|statefulset|daemonset)(?:\.apps)?/(.+)$", a)
        if m:
            return m.group(2)
        if a in ("deploy", "deployment", "statefulset", "daemonset") and i + 1 < len(argv):
            return argv[i + 1]
    return "default"


out = ""
if name == "kubectl":
    dep = deployment(args)
    key = f"replicas:{dep}"
    if "get" in args and any("replicas" in a for a in args):
        out = str(state.get(key, 3))
    elif "scale" in args:
        for a in args:
            if a.startswith("--replicas="):
                state[key] = int(a.split("=", 1)[1])
        out = f"{dep} scaled"
    elif "rollout" in args and "status" in args:
        out = f'deployment "{dep}" successfully rolled out'
    elif "get" in args:
        out = f"{dep}-pod-0"
    else:
        out = f"{dep} configured"
elif name == "psql":
    sql = " ".join(args)
    out = "42/200" if "count(*)" in sql.lower() and "/" in sql else "0"
elif name == "redis-cli":
    if "CONFIG" in args and "SET" in args and len(args) >= 2:
        state[f"redis:{args[-2]}"] = args[-1]
    out = "OK"
elif name == "df":
    out = "Filesystem Size Used Avail Use% Mounted on\n/dev/sda1 100G 60G 40G 60% /var"
elif name == "date":
    out = "1700000000"
elif name in ("sleep", "true"):
    out = ""
else:
    out = f"[{name}] ok"

with open(state_path, "w", encoding="utf-8") as f:
    json.dump(state, f)
if out:
    print(out)
