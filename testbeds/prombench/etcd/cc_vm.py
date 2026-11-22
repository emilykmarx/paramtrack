#!/usr/bin/env python3
"""Runs on the prombench VM: check and apply concrete configs on etcd (the skill's TESTBED.md, next to this file). Copy
this whole directory to the VM (check uses check_config/). etcd reads its config only at start, so applying one is a
restart, as paramtrack's testbed does it (testbeds.Etcd): the config, with NODE_IP replaced by the node's IP, at
/argmatch-out/etcd/config/config.json in the node, and the static pod's command `etcd --config-file=<that>` (keeping
the image it runs). While etcd restarts, the cluster's API is down.

  cc_vm.py files <dir>        copy each file in dir to /argmatch-out/etcd/files/<name> (the config refers to them by
                              that path), symlinks as symlinks
  cc_vm.py check <config>     load and validate the config as etcd does (check_config/), with NODE_IP replaced
  cc_vm.py restart <config>   restart etcd with the config; wait until etcd is healthy and the API serves
  cc_vm.py apply <config>     the same as restart (no reload)
  cc_vm.py health             etcd's /health, the API's /readyz, and pods with restarts or not running
  cc_vm.py shipped            put back kubeadm's manifest (its flags, upstream's image)
"""
import os
import re
import subprocess
import sys
import tempfile
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
CTX = "kind-prombench"
NODE = "prombench-control-plane"
MANIFEST = "/etc/kubernetes/manifests/etcd.yaml"
ORIGINAL = "/etc/kubernetes/paramtrack-original/etcd.yaml"  # (testbeds.Etcd saves kubeadm's there)
HOST_OUT = "/home/emily/projects/config_tracing/dump_ancestors/argmatch-out/etcd"  # /argmatch-out/etcd in the node
POD_CONFIG = "/argmatch-out/etcd/config/config.json"
HEALTH = "http://127.0.0.1:2381/health"
GO = "/usr/local/go/bin/go"


def run(cmd, timeout=120, check=True, input=None):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)
    if check and p.returncode != 0:
        sys.exit(f"{' '.join(cmd)[:200]}: exit {p.returncode}\n{p.stderr[-3000:]}")
    return p


def node(*cmd, input=None, check=True, timeout=120):
    return run(["docker", "exec", *(["-i"] if input is not None else []), NODE, *cmd], input=input, check=check,
               timeout=timeout).stdout


def original():
    out = node("sh", "-c", f"cat {ORIGINAL} 2>/dev/null || cat {MANIFEST}")
    return out


def node_ip():
    m = re.search(r"--advertise-client-urls=https://([0-9.]+):2379", original())
    if not m:
        sys.exit("no --advertise-client-urls in kubeadm's manifest")
    return m.group(1)


def filled(config):
    return open(config).read().replace("NODE_IP", node_ip())


def healthy():
    return '"health":"true"' in node("curl", "-s", "-m", "10", HEALTH, check=False, timeout=30)


def serving():
    return run(["kubectl", "--context", CTX, "get", "--raw", "/readyz"], check=False, timeout=60).returncode == 0


def replace_manifest(text, limit=900):
    """Remove the running manifest, wait for etcd's container to go, write text, wait for etcd and the API"""
    node("sh", "-c", f"mkdir -p {os.path.dirname(ORIGINAL)} && {{ [ -e {ORIGINAL} ] || cp {MANIFEST} {ORIGINAL}; }}")
    node("rm", "-f", MANIFEST)
    start = time.time()
    while node("crictl", "ps", "-q", "--name", "^etcd$").strip():
        if time.time() - start > 300:
            node("sh", "-c", f"cp {ORIGINAL} {MANIFEST}")
            sys.exit("etcd still running 300s after its manifest was removed; put kubeadm's back")
        time.sleep(3)
    node("sh", "-c", f"cat > {MANIFEST}", input=text)
    start = time.time()
    while not healthy():
        if time.time() - start > limit:
            tail = node("sh", "-c", "crictl logs --tail 40 $(crictl ps -a -q --name '^etcd$' | head -1) 2>&1",
                        check=False)
            sys.exit(f"etcd not healthy {limit}s after the restart (the manifest stays, for a look; `cc_vm.py "
                     f"shipped` puts kubeadm's back); its log ends:\n{tail}")
        time.sleep(3)
    print(f"etcd healthy after {time.time() - start:.0f}s", flush=True)
    while not serving():
        if time.time() - start > limit:
            sys.exit(f"the API isn't serving {limit}s after etcd's restart")
        time.sleep(3)
    print(f"the API serves after {time.time() - start:.0f}s", flush=True)


def restart(config):
    run(["sudo", "mkdir", "-p", os.path.dirname(f"{HOST_OUT}/config/x")])
    run(["sudo", "tee", f"{HOST_OUT}/config/config.json"], input=filled(config))
    pod = yaml.safe_load(node("cat", MANIFEST, check=False) or original())
    c = pod["spec"]["containers"][0]
    c["command"] = ["etcd", f"--config-file={POD_CONFIG}"]
    if not any(m["name"] == "argmatch-out" for m in c.get("volumeMounts") or []):
        c.setdefault("volumeMounts", []).append({"name": "argmatch-out", "mountPath": "/argmatch-out"})
        pod["spec"].setdefault("volumes", []).append(
            {"name": "argmatch-out", "hostPath": {"path": "/argmatch-out", "type": "Directory"}})
    replace_manifest(yaml.safe_dump(pod, sort_keys=False))


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "files":
        run(["sudo", "mkdir", "-p", f"{HOST_OUT}/files"])
        for n in sorted(os.listdir(args[0])):
            run(["sudo", "cp", "-P", os.path.join(args[0], n), f"{HOST_OUT}/files/{n}"])  # (symlinks as symlinks)
        print(f"copied {len(os.listdir(args[0]))} files to /argmatch-out/etcd/files/")
    elif cmd == "check":
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            # (loading opens the log files: /argmatch-out in the node is HOST_OUT's parent here)
            f.write(filled(args[0]).replace("/argmatch-out/", f"{os.path.dirname(HOST_OUT)}/"))
            f.flush()
            p = subprocess.run([GO, "run", ".", f.name], cwd=f"{HERE}/check_config", capture_output=True, text=True,
                               timeout=600)
        print(p.stdout.strip() or p.stderr.strip()[-2000:])
        sys.exit(p.returncode)
    elif cmd in ("restart", "apply"):
        restart(args[0])
    elif cmd == "health":
        print("etcd /health:", "ok" if healthy() else "NOT HEALTHY")
        print("API /readyz:", "ok" if serving() else "NOT SERVING")
        pods = run(["kubectl", "--context", CTX, "get", "pods", "-A", "--no-headers"]).stdout.splitlines()
        bad = [p for p in pods if p.split()[3] not in ("Running", "Completed") or p.split()[4] != "0"]
        print("pods with restarts or not running:" + ("".join(f"\n  {p}" for p in bad) or " none"))
    elif cmd == "shipped":
        replace_manifest(node("cat", ORIGINAL))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
