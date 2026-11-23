#!/usr/bin/env python3
"""The messages of a paramtrack run that only one side logged: a request or response one instrumented module's events
have, whose other side (the receiver's or the sender's) no module's events have. The matching is contexttrack's
analysis/match_messages.py (its docstring says how messages are reduced to types and paired); this script runs it on
step 5's events of every module in the run, each module's file as one component, with its loopback_within: a module's
sent request pairs with a request it received itself only if the request's host is loopback, or it received the
request from its own IP (a request to a loopback host pairs with other modules' types by type, as any request does).

Most unmatched messages are to or from components that aren't instrumented (etcd, kubelets, endpoints outside the
testbed, the concrete config's made-up hosts). --external names them, from a file of lines

    <glob> => <name>

(`#` comments and blank lines skipped). The glob (fnmatch: `*`, `?`, `[...]`) is tested against every identity of the
unmatched message's peer address, and the first line one matches names it:
- the peer: for a sent request, the host it was sent to (a response received: its request's); for a received
  request, each IP the server logged it from (match_messages' client_ips, from the server side's conn); for a
  response sent, each IP its responses went to. Runs whose events have no conn (before 2026-11-19) have no client
  IPs.
- its identities: the address as logged (e.g. `127.0.0.1:2379`, `loki.default.svc:3100`) and without its port; for an
  IP, the pods (but not host-network pods, whose IP is their node's), nodes and services the testbed's snapshot names
  for it (testbeds.Prombench.ADDRESSES, in
  <run>/testbed/5-matches/: e.g. `pod kube-system/etcd-prombench-control-plane`, `node prombench-worker`,
  `service default/loki`); for a Kubernetes service DNS name (<service>.<namespace>.svc[.<domain>]),
  `service <namespace>/<service>`, and for a bare host name, the services of that name; and `module <name>` if a
  module serves on the IP or behind the service (Peers.record_server; not on a node's IP, which host-network pods
  share).
An address no line matches is named from those identities (Peers.derived_name: a pod's workload or a service's
name, without the namespace, or a node), unless it is a module's: a message between modules that didn't pair is
unexplained.
A client is named instead from what it says it is (Peers.peer_name), unless its address is a module's: the testbed
workload's job, from a User-Agent (the server side's events' user_agent, since 2026-11-19) of
`paramtrack-testbed/<job>` (common.TESTBED_AGENT); the kubectl command that sent it (kubectl_command, since
2026-11-22); or, for a node's IP, the product its User-Agent names.
A message is external if every peer of it is named; the others are listed as unexplained, first.

match_messages pairs a sent and a received request only if their api_ids are equal, and an api_id names the library
that logged the message, so the two sides of a message between modules often differ (e.g. Grafana's datasource client,
github.com/grafana, and Prometheus' server, github.com/prometheus). --api-ids gives such messages a made-up api_id on
both sides, from a file of lines

    <METHOD or *> <path or route pattern> => <made-up api_id>

The file's format:
- `#` starts a comment, anywhere on a line (so a path can't contain `#`); blank lines are skipped.
- The first run of whitespace separates the method from the path; the path is the rest up to the first `=>`, trimmed.
- The method is compared exactly; `*` matches any.

A "Request sent" or "Request received" event gets the api_id of the first line that matches it, in place of its own;
responses take theirs from the request they answer (match_messages). A line matches an event if the methods match and
the line's path equals the event's concrete path (a sent request's request_id path, a received request's
req.URL.Path; never the receiver's route pattern), or matches it as a route pattern: match_messages.pattern_matches,
whose docstring lists what's special in a pattern (`{x}`, `{x...}`, `:name`, a trailing `/`, ...).

Usage: python3 unmatched_messages.py --run <paramtrack --out dir> --out <dir> [--api-ids <file>] [--external <file>]

Outputs, in the --out directory:
- all.csv: every type row, matched ones too (match_messages.FIELDS).
- unexplained.md: the unmatched types whose peer is a module or nothing names (per module that logged them), with
  their numbers of messages and peer addresses (what the testbed's address snapshot says they are).
- external.md: the unmatched types whose peer --external or the address snapshot names, per module, by that name.
- replaced_api_ids.md (with --api-ids): per made-up api_id, the real api_ids it replaced, the modules they were in,
  and the labels of those messages (as label_keys.csv labels them: argmatch-labels' join.py, from contexttrack's
  message_graph, normalized; without the "paths" field, and with the real api_id).
- disambiguated.md: where the api_id decided which module received a sent request: the sent request types for which
  routing by method and route pattern alone (match_messages.most_specific, per receiving module, as match_requests
  routes) reaches more receiver types than routing with the api_id too; and those for which the api_id ruled out every
  receiver type (so they're unmatched).
"""

import argparse
import collections
import fnmatch
import functools
import glob
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.."))
import common  # noqa: E402  (paramtrack's: where the conftamer checkout is)

sys.path.insert(0, f"{common.CHECKOUTS['conftamer']}/contexttrack/analysis")
import match_messages  # noqa: E402
import message_graph  # noqa: E402  (contexttrack's: message labels)

sys.path.insert(0, common.ARGMATCH_LABELS)
import join  # noqa: E402  (argmatch-labels': label normalization, as label_keys.csv's labels)
from events import read as read_events  # noqa: E402  (paramtrack's: events cut at the run's cutoff)

STEP = "5-matches"


def read_api_ids(path):
    """--api-ids' lines, in order: (method or "*", path or route pattern, made-up api_id)"""
    out = []
    for n, line in enumerate(open(path), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        left, sep, made_up = line.partition("=>")
        parts = left.split(None, 1)
        if not sep or len(parts) != 2 or not made_up.strip():
            sys.exit(f"{path}:{n}: expected `<METHOD> <path> => <api_id>`: {line}")
        out.append((parts[0], parts[1].strip(), made_up.strip()))
    return out


# match_messages.collect prepares each module's events with dedup_responses and _stamp_routes; also attribute each
# response to its request (its api_id), so they're labelled as message_graph.messages, and join.py (the matrices),
# label them. (Matching doesn't change: response types take their api_id from their request's type.) ApiIds wraps
# this in turn, so its replaced api_ids don't reach the responses.
match_messages._stamp_routes = message_graph.messages

ORIGINAL_API_ID = "api_id_before_api_ids"  # (an event's api_id before ApiIds replaced it; None if it had none)


class ApiIds:
    """Replaces the api_ids of the request events --api-ids' lines match, after match_messages' collect has stamped
    routes (and with them the received requests' api_ids: message_graph._stamp_routes), by wrapping the
    _stamp_routes it calls. Records what it replaced: made-up api_id => (real api_id, module, kind) => the messages'
    labels"""

    def __init__(self, lines):
        self.lines = lines
        self.module = None  # (set before each module's collect)
        self.replaced = collections.defaultdict(lambda: collections.defaultdict(set))
        stamp = match_messages._stamp_routes

        def stamp_and_replace(events):
            stamp(events)
            for ev in events:
                self.replace(ev)
        match_messages._stamp_routes = stamp_and_replace

    def replace(self, ev):
        kind = ev.get("kind")
        if kind not in ("Request sent", "Request received"):
            return
        msg = ev.get("message") or {}
        rid = ev.get("request_id") or {}
        method = rid.get("method") or msg.get("req.Method", "")
        path = rid.get("path") or msg.get("req.URL.Path", "")
        for m, pattern, made_up in self.lines:
            if m in ("*", method) and (pattern == path or match_messages.pattern_matches(pattern, path)):
                # (the api_id match_messages would have used: for a sent request, past gRPC interceptors)
                real = match_messages._api_id(ev) if kind == "Request sent" else ev.get("api_id", "")
                # (its label as label_keys.csv has it, before the replacement; no "paths" field)
                label = join.normalize(message_graph._msg_label(ev))
                self.replaced[made_up][(real or "(none)", self.module, kind)].add(label)
                ev[ORIGINAL_API_ID] = ev.get("api_id")  # (for labels as contexttrack gives them: message_paths)
                ev["api_id"] = made_up
                return

    def write(self, path, source):
        """The listing: per made-up api_id, the real api_ids it replaced, and per module and kind the messages' labels"""
        out = [f"# Made-up api_ids ({source}): the real api_ids they replaced, where, and the messages' labels", ""]
        for made_up, c in sorted(self.replaced.items()):
            out += [f"## {made_up}", ""]
            last = None
            for (real, module, kind), labels in sorted(c.items()):
                if real != last:
                    out.append(f"- {real}")
                    last = real
                out.append(f"  - {module}, {kind}:")
                out += [f"    - `{label}`" for label in sorted(labels)]
            out.append("")
        unused = {made_up for _, _, made_up in self.lines} - self.replaced.keys()
        if unused:
            out.append(f"Lines that matched no message: {', '.join(sorted(unused))}")
        open(path, "w").write("\n".join(out) + "\n")


def read_globs(path):
    """--external's lines, in order: (glob, name)"""
    out = []
    for n, line in enumerate(open(path), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, sep, name = (x.strip() for x in line.partition("=>"))
        if not sep or not pattern or not name:
            sys.exit(f"{path}:{n}: expected `<glob> => <name>`: {line}")
        out.append((pattern, name))
    return out


class Peers:
    """Names unmatched messages' peer addresses: --external's lines, then names derived from the testbed's address
    snapshot (derived_name)"""

    SERVICE_DNS = re.compile(r"^([a-z0-9-]+)\.([a-z0-9-]+)\.svc(\..*)?$")
    # The random suffixes Kubernetes gives a workload's pods (k8s.io/apimachinery's rand.String and SafeEncodeString
    # alphabet): a Deployment's <name>-<pod-template-hash>-<5>, a DaemonSet's or Job's <name>-<5>, a StatefulSet's
    # <name>-<ordinal>
    _K = "[bcdfghjklmnpqrstvwxz2456789]"
    POD_SUFFIX = re.compile(rf"-(?:{_K}{{6,10}}-{_K}{{5}}|{_K}{{5}}|[0-9]+)$")
    NODE = "<a node: kubelet, or a pod on its host network>"

    def __init__(self, run, lines):
        self.lines = lines
        path = f"{run}/testbed/{STEP}/addresses.json"  # (testbeds.Prombench.ADDRESSES)
        snapshot = json.load(open(path)) if os.path.exists(path) else {}
        self.addresses = snapshot.get("addresses", {})
        self.backends = snapshot.get("backends", {})  # service => its pods' IPs (snapshots since 2026-11-19)
        self.modules = collections.defaultdict(set)  # IP => the modules serving on it (record_server)
        # service name => "service <namespace>/<name>", for a bare host name (resolved through the DNS search path)
        self.services = collections.defaultdict(list)
        for names in self.addresses.values():
            for n in names:
                if n.startswith("service "):
                    self.services[n.split("/", 1)[1]].append(n)

    def record_server(self, module, side, typ, ev):
        """An on_message (match): records the IP each module serves on, its received requests' local address (not
        loopback), so that a peer that is a module isn't named as external"""
        local = (ev.get("conn") or {}).get("local") if side == "rcv_req" else None
        if local and not match_messages.is_loopback(local):
            self.modules[match_messages._ip(local)].add(module)

    def identities(self, addr):
        """The address as logged, without its port, the modules serving on it (record_server), and what the snapshot
        or its DNS name says it is"""
        host = match_messages._ip(addr)
        # (not a host-network pod's name: its IP is its node's, shared by every such pod and the kubelet, so it
        # only names the node)
        out = [addr, host] + [n for n in self.addresses.get(host, []) if not n.endswith("(host network)")]
        m = self.SERVICE_DNS.match(host)
        if m:
            out.append(f"service {m.group(2)}/{m.group(1)}")
        elif "." not in host:
            out += self.services.get(host, [])
        # The modules serving on it, or behind a service of it: not a node's IP, which a host-network module (e.g.
        # kube-apiserver) shares with the kubelet and every other host-network pod
        if not any(i.startswith("node ") for i in out):
            ips = {host} | {ip for i in out if i.startswith("service ") for ip in self.backends.get(i[8:], ())}
            out += sorted({f"module {mod}" for ip in ips for mod in self.modules.get(ip, ())})
        return list(dict.fromkeys(out))

    def derived_name(self, ids):
        """A name from the snapshot's identities: a service's name or a pod's workload (its name without POD_SUFFIX),
        without the namespace, or NODE for a node's IP; or None, and for a module's IP (a message between modules that didn't
        pair is unexplained)"""
        if any(i.startswith("module ") for i in ids):
            return None
        for i in ids:
            kind, _, rest = i.partition(" ")
            if kind == "service":
                return f"<{rest.split('/', 1)[1]}>"
            if kind == "pod":
                return f"<{self.POD_SUFFIX.sub('', rest.split('/', 1)[1])}>"
            if kind == "node":
                return self.NODE
        return None

    def name(self, addr):
        """The first --external line one of addr's identities matches, else derived_name: its name, or None"""
        ids = self.identities(addr)
        for pattern, name in self.lines:
            if any(fnmatch.fnmatchcase(i, pattern) for i in ids):
                return name
        return self.derived_name(ids)

    @staticmethod
    def of(row):
        """An unmatched row's peers, as (address, User-Agent or None, kubectl command or None), and whether they're
        the request's host (else its clients: their IPs, with their User-Agents and kubectl commands if the events
        had them, match_messages' client_agents)"""
        sent_req = row["status"] == ("unmatched-sender" if row["message"] == "request" else "unmatched-receiver")
        if sent_req:  # (a sent request, or the response received to one: the host it went to)
            host = row["snd_host"] if row["message"] == "request" else row["rcv_host"]
            return ([(host, None, None)] if host else []), True
        if row.get("client_agents"):
            # (entries before 2026-11-22 are [ip, user_agent], without the command)
            return [(c[0], c[1] or None, (c[2] if len(c) > 2 else "") or None)
                    for c in json.loads(row["client_agents"])], False
        return [(ip, None, None) for ip in row.get("client_ips", "").split(";") if ip], False

    def peer_name(self, addr, agent=None, command=None):
        """A peer's name, from what the client says it is, else from its address (name):
        - a testbed workload's job, <testbed: <job>>, from its User-Agent (common.testbed_agent);
        - else the kubectl command that sent it, <kubectl <command>>, e.g. <kubectl patch> (its Kubectl-Command
          header: kubectl's User-Agent is the same for every command);
        - else, for a node's IP (the kubelet and every pod on the node's host network share it), the product its
          User-Agent names, e.g. <kube-controller-manager>, <kube-probe> (the kubelet's probes);
        - else name(addr).
        Not from what the client says for a module's address: a proxy passes its client's headers on (e.g. Caddy the
        viewer's to Grafana), so they name who started the request, not the peer that sent it"""
        if any(i.startswith("module ") for i in self.identities(addr)):
            return self.name(addr)
        if agent and agent.startswith(common.TESTBED_AGENT):
            return f"<testbed: {agent[len(common.TESTBED_AGENT):]}>"
        if command:
            return f"<{command}>"
        name = self.name(addr)
        if name == self.NODE and agent:
            return f"<{agent.split('/')[0].split(' ')[0]}>"
        return name

    def classify(self, row):
        """(external name or None, description of the peers)"""
        peers, is_host = self.of(row)
        if not peers:
            return None, "peer unknown" + ("" if is_host else " (no conn: no client IPs)")
        names = [self.peer_name(a, agent, command) for a, agent, command in peers]
        descs = []
        for a in dict.fromkeys(a for a, _, _ in peers):  # (an address once, with its User-Agents and commands)
            ids = self.identities(a)[2:] + [f"User-Agent {agent}" for b, agent, _ in peers if b == a and agent] + \
                [f"{command}" for b, _, command in peers if b == a and command]
            descs.append(a + (f" ({'; '.join(ids)})" if ids else ""))
        return (" + ".join(sorted(set(names))) if all(names) else None), ("host " if is_host else "from ") + ", ".join(descs)


def module_events(run):
    """Module name => its step 5 events file, for every module of the run (<out>/<module>/5-matches/events.jsonl)"""
    return {p.split("/")[-3]: p for p in sorted(glob.glob(f"{run}/*/{STEP}/events.jsonl")) if os.path.getsize(p)}


def match(run, api_ids=None, on_message=None, sides=None):
    """match_messages' type rows for the run's modules, each module one component (its name); api_ids: an ApiIds.
    on_message(module, side name, type, event): called for each message event (match_messages.collect's on_message).
    sides: a dict to fill with match_messages' Sides (side name => Side), for their types"""
    sides = {} if sides is None else sides
    sides.update({k: match_messages.Side(k) for k in ("snd_req", "rcv_req", "snd_resp", "rcv_resp")})
    with tempfile.TemporaryDirectory() as d:
        for module, path in module_events(run).items():
            # (match_messages names a component after its file: <module>.jsonl; the events cut at the run's cutoff,
            # events.py)
            link = Path(d, f"{module}.jsonl")
            print(f"reading {module} ({path})", file=sys.stderr)
            link.write_text("".join(json.dumps(e) + "\n" for e in read_events(path)))
            if api_ids:
                api_ids.module = module
            match_messages.collect(link, sides, on_message and functools.partial(on_message, module))
    # (each module is one process: it sends to itself only through loopback)
    return match_messages.build_rows(sides, loopback_within=True)


def describe(row, peer=""):
    """One line for an unmatched type row: the side that logged it"""
    side = "snd" if row["status"] == "unmatched-sender" else "rcv"
    host = f" [{peer}]" if peer else ""
    code = f" code={row[side + '_code']}" if row.get(side + "_code") else ""
    other = f" (other side's api_ids for this method+path: {row['same_path_other_api_ids']})" \
        if row.get("same_path_other_api_ids") else ""
    return (f"{row['message']} {'sent' if side == 'snd' else 'received'}: api_id={row[side + '_api_id']} "
            f"{row[side + '_method']} {row[side + '_path']}{code}{host} x{row[side + '_n']}{other}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="a paramtrack run's --out directory (all its modules)")
    ap.add_argument("--out", required=True, help="output directory (Outputs above)")
    ap.add_argument("--api-ids", help="file of `<METHOD> <path> => <made-up api_id>` lines (see above)")
    ap.add_argument("--external", help="file of `<glob> => <name>` lines naming external peers (see above)")
    a = ap.parse_args()
    api_ids = ApiIds(read_api_ids(a.api_ids)) if a.api_ids else None
    peers = Peers(a.run, read_globs(a.external) if a.external else [])
    sides = {}
    rows = match(a.run, api_ids, peers.record_server, sides)
    os.makedirs(a.out, exist_ok=True)
    path = f"{a.out}/all.csv"
    with open(path, "w", newline="") as f:
        w = match_messages.csv.DictWriter(f, fieldnames=match_messages.FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path}: {len(rows)} type rows; "
          + ", ".join(f"{k}: {n}" for k, n in sorted(collections.Counter(r['status'] for r in rows).items())))

    if api_ids:
        api_ids.write(f"{a.out}/replaced_api_ids.md", a.api_ids)
        print(f"wrote {a.out}/replaced_api_ids.md")

    by_module = collections.defaultdict(list)
    for row in rows:
        if row["status"] == "unmatched-sender":
            by_module[row["snd_components"]].append(row)
        elif row["status"] == "unmatched-receiver":
            by_module[row["rcv_components"]].append(row)
    source = (f"{a.run}" + (f"; --api-ids {a.api_ids}" if a.api_ids else "")
              + (f"; --external {a.external}" if a.external else ""))
    how = ("Each line a match_messages type (its rows in all.csv), with its number of messages (x<n>), and its peer "
           "addresses [host: where a sent request went; from: the clients a received request came from], with what "
           "the testbed's address snapshot says they are.")
    unexplained = [f"# Unmatched types with no named peer ({source})", "",
                   "Messages only one side logged, whose other side is a module (a message between modules that didn't "
                   "pair) or nothing names; per module that logged them. " + how, ""]
    external = [f"# Unmatched types with a named peer ({source})", "",
                "Messages only one side logged, whose other side --external or the testbed's address snapshot names "
                "(a component that isn't instrumented, or outside the testbed); per module that logged them, by that "
                "name. " + how, ""]
    if not peers.addresses:
        unexplained += ["(No address snapshot in this run: IPs aren't named.)", ""]
        external += ["(No address snapshot in this run: IPs aren't named.)", ""]
    totals = collections.Counter()
    for module, rs in sorted(by_module.items()):
        groups = collections.defaultdict(list)  # external name (None: unexplained) => lines
        for row in rs:
            name, peer = peers.classify(row)
            groups[name].append("- " + describe(row, peer))
        n_unexplained = len(groups.get(None, []))
        totals["unexplained"] += n_unexplained
        totals["external"] += len(rs) - n_unexplained
        if n_unexplained:
            unexplained += [f"## {module} ({n_unexplained})", ""] + groups[None] + [""]
        if len(rs) - n_unexplained:
            external += [f"## {module} ({len(rs) - n_unexplained})", ""]
            for name in sorted(n for n in groups if n is not None):
                external += [f"### {name} ({len(groups[name])})", ""] + groups[name] + [""]
    open(f"{a.out}/unexplained.md", "w").write("\n".join(unexplained))
    open(f"{a.out}/external.md", "w").write("\n".join(external))
    print(f"wrote {a.out}/unexplained.md ({totals['unexplained']} types), {a.out}/external.md "
          f"({totals['external']} types)")

    made_up = {m for _, _, m in api_ids.lines} if api_ids else set()
    n = write_disambiguated(f"{a.out}/disambiguated.md", sides, made_up, source)
    print(f"wrote {a.out}/disambiguated.md: {n[0]} sent request types narrowed by the api_id, {n[1]} with every "
          "receiver ruled out")


# match_requests' settings as build_rows uses them here (match)
ROUTING = {"loopback_within": True} if "loopback_within" in match_messages.match_requests.__code__.co_varnames else {}


def write_disambiguated(path, sides, made_up, source):
    """disambiguated.md (Outputs above); returns how many sent request types were narrowed and how many had every
    receiver type ruled out"""
    snd, rcv = sides["snd_req"], sides["rcv_req"]

    def without_api_ids(side):  # (a copy of a Side with every type's api_id blanked; type => its originals)
        out, of = match_messages.Side(side.name), collections.defaultdict(list)
        for t, n in side.n.items():
            b = ("",) + t[1:]
            of[b].append(t)
            out.n[b] += n
            out.components[b] |= side.components[t]
            out.paths[b] |= side.paths[t]
            for attr in ("self_sent", "routed"):  # (the matching's per-type sets, where it has them)
                if t in getattr(side, attr, ()):
                    getattr(out, attr).add(b)
        return out, of
    # Routing by method and pattern alone: match_requests itself, on the types with their api_ids blanked
    bsnd, _ = without_api_ids(snd)
    brcv, rcv_of = without_api_ids(rcv)
    by_route = collections.defaultdict(list)  # blanked sent type => the receiver types it routes to
    for status, bs, br in match_messages.match_requests(bsnd, brcv, **ROUTING):
        if status.startswith("matched"):
            by_route[bs] += rcv_of[br]
    with_api = collections.defaultdict(list)  # sent type => the receiver types it pairs with
    for status, s, r in match_messages.match_requests(snd, rcv, **ROUTING):
        if status.startswith("matched"):
            with_api[s].append(r)

    def api(a):
        return f"{a or '(none)'}" + (" (made up)" if a in made_up else "")

    def line(r):
        return f"    - {';'.join(sorted(rcv.components[r]))}, api_id {api(r[0])}: `{r[3]}`"
    narrowed, ruled_out = [], []
    for s in sorted(snd.n, key=lambda s: (sorted(snd.components[s]), s[3], s[0])):
        routed_to = by_route[("",) + s[1:]]
        kept_r = with_api[s]
        if not set(routed_to) - set(kept_r):
            continue
        head = (f"- sent by {';'.join(sorted(snd.components[s]))}, api_id {api(s[0])}: {s[1]} `{s[3]}`"
                f" (host {s[2] or '-'}, x{snd.n[s]})")
        kept = [line(r) for r in kept_r]
        dropped = [line(r) for r in routed_to if r not in kept_r and r[0] != s[0]]
        if not dropped:
            continue
        if kept_r:
            narrowed += [head, "  - kept:"] + kept + ["  - ruled out:"] + dropped
        else:
            ruled_out += [head, "  - ruled out:"] + dropped
    out = [f"# Where the api_id decided the receiver ({source})", "",
           "Sent request types for which routing by method and route pattern alone (match_requests itself, on the "
           "types with their api_ids blanked) reaches receiver types that the api_id rules out (match_requests pairs "
           "only equal api_ids). A made-up api_id is one "
           "--api-ids gave (replaced_api_ids.md).", "",
           f"## The api_id chose among the receivers ({sum(1 for x in narrowed if x.startswith('- '))})", ""]
    out += narrowed or ["(none)"]
    out += ["", f"## The api_id ruled out every receiver: unmatched ({sum(1 for x in ruled_out if x.startswith('- '))})",
            "", "(A sent request whose path some module routes, under another api_id: an over-narrow or missing "
            "--api-ids line, or a request that truly went elsewhere.)", ""]
    out += ruled_out or ["(none)"]
    open(path, "w").write("\n".join(out) + "\n")
    return sum(1 for x in narrowed if x.startswith("- ")), sum(1 for x in ruled_out if x.startswith("- "))


if __name__ == "__main__":
    main()
