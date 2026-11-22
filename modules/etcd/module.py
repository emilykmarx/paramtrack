"""etcd: paramtrack's settings and hooks for this module (see ../__init__.py for what each is; documentedkeys/README.md
for what its config is and how the list was made).

Root: its YAML config file (--config-file). The file is also decoded into etcdmain.configProxy (the v2 proxy mode's
keys), which isn't a root: those keys are out of scope (ignore-config.txt). The testbed's kubeadm configures etcd by
flags only; the file can express them all. The testbed runs it as testbeds.Etcd, built from argmatch.Dockerfile here."""

import os

HERE = os.path.dirname(os.path.abspath(__file__))
DOCS = f"{HERE}/documentedkeys"

GO_MODULE = "go.etcd.io/etcd/server/v3"
# Upstream's v3.5.15, the version the testbed's kind cluster runs (registry.k8s.io/etcd:3.5.15-0); the server is its
# own Go module, in server/ (its main package too)
MODULE_DIR = "/home/emily/projects/config_tracing/etcd-paramtrack/server"
# The file is YAML, but sigs.k8s.io/yaml converts it to JSON and decodes it by the json tags (the types have no yaml
# tags), as kube-apiserver's: so the concrete configs are JSON (valid YAML), which paramtrack parses
FORMAT = "json"
CONFIG_SOURCES = {
    "config": {"root": "/embed.configYAML",  # --config-file: embed.ConfigFromFile
               "docs": f"{DOCS}/etcd_config_keys.txt", "ignore": f"{HERE}/ignore-config.txt"},
}
# argmatch's search caps per call (over testbeds.py's ARGMATCH_ARGS, which has none), as kube-apiserver's: every API
# request reaches etcd, and the cluster waits on it (a guess, before any run 2: revisit with its timings)
ARGMATCH_ARGS = {"MAXTIME": "2s", "MAXMEM": "1GB"}
GOLITS_PACKAGE = "."
GOLITS_TAGS = ""
TITLE = "etcd, root: its YAML config file"


class CustomKeys:
    """No keys picked by custom unmarshalers (flat config, decoded by json tags)"""

    names = {}
    parents = ()

    def __init__(self, module_dir):
        pass

    def of(self, fk):
        return None


def docs_kind(typ):
    """Kind of Go value we'd expect for a documented type (documentedkeys/README.md, "Types"): durations are integer
    nanoseconds in the file (time.Duration); comma-separated lists are single strings"""
    if typ == "<bool>":
        return "bool"
    if typ in ("<int>", "<duration>"):
        return "number"
    return "string"


# Documented flags whose file key is elsewhere (documentedkeys/README.md, "Known gaps"): the docs say a file key is
# its flag's name, but these are decoded at another place (user's decision, 2026-11-22: list them by their file key)
FILE_KEYS = {
    "socket-reuse-address": "socket-options.reuse-address",
    "socket-reuse-port": "socket-options.reuse-port",
    "client-cert-file": "client-transport-security.client-cert-file",
    "client-key-file": "client-transport-security.client-key-file",
    "peer-client-cert-file": "peer-transport-security.client-cert-file",
    "peer-client-key-file": "peer-transport-security.client-key-file",
}


def docs_entry(key, typ):
    """stats.read_docs' hook: a documented flag's file key (FILE_KEYS)"""
    return FILE_KEYS.get(key, key), typ


def graph_docs_reason(k, in_graph):
    """Why a key is only in the graph (in_graph) or only in the docs (the graph of 2026-11-22 vs the v3.5.15 docs,
    after FILE_KEYS; documentedkeys/README.md, "Known gaps")"""
    if in_graph:
        if k == "client-transport-security.allowed-cn":
            return "shared TLS section type (the allowed-CN flag is peer-only)"
        if k == "experimental-snapshot-catch-up-entries":
            return "undocumented"
    elif k in ("client-crl-file", "peer-crl-file", "experimental-peer-skip-client-san-verification",
               "raft-read-timeout", "raft-write-timeout"):
        return "flag only (no file key)"
    return "other: " + k.split(".")[-1]


UNSET_REASONS = []
