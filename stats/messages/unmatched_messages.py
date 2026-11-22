#!/usr/bin/env python3
"""The messages of a paramtrack run that only one side logged: a request or response one instrumented module's events
have, whose other side (the receiver's or the sender's) no module's events have. The matching is contexttrack's
analysis/match_messages.py (its docstring says how messages are reduced to types and paired); this script runs it on
step 5's events of every module in the run, each module's file as one component, with its loopback_within: a module's
sent request pairs with a request it received itself only if the request's host is loopback, or it received the
request from its own IP.

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
A client whose User-Agent (the server side's events' user_agent, since 2026-11-19) starts with
`paramtrack-testbed/<job>`, as the testbed's workload tags its requests (common.TESTBED_AGENT), is named
<testbed: <job>> instead.
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

It writes, per made-up api_id, the real api_ids it replaced, the modules they were in, and the
labels of those messages (as label_keys.csv labels them: argmatch-labels' join.py, from contexttrack's message_graph,
normalized; without the "paths" field, and with the real api_id) to <--out without .csv>.api_ids.md.

Usage: python3 unmatched_messages.py --run <paramtrack --out dir> --out <file.csv> [--api-ids <file>] [--external <file>]
writes every type row (matched ones too) to the CSV, and the unmatched ones, per module, to <--out without
.csv>.unmatched.md.
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
        """An unmatched row's peers, as (address, User-Agent or None), and whether they're the request's host (else
        its clients: their IPs, with their User-Agents if the events had them, match_messages' client_agents)"""
        sent_req = row["status"] == ("unmatched-sender" if row["message"] == "request" else "unmatched-receiver")
        if sent_req:  # (a sent request, or the response received to one: the host it went to)
            host = row["snd_host"] if row["message"] == "request" else row["rcv_host"]
            return ([(host, None)] if host else []), True
        if row.get("client_agents"):
            return [(ip, agent) for ip, agent in json.loads(row["client_agents"])], False
        return [(ip, None) for ip in row.get("client_ips", "").split(";") if ip], False

    def peer_name(self, addr, agent=None):
        """A peer's name: a testbed workload's job (<testbed: <job>>), from its User-Agent; else name(addr). Not the
        job for a module's address: a proxy passes its client's User-Agent on (e.g. Caddy the viewer's to Grafana), so
        it names who started the request, not the peer that sent it"""
        if agent and agent.startswith(common.TESTBED_AGENT) \
                and not any(i.startswith("module ") for i in self.identities(addr)):
            return f"<testbed: {agent[len(common.TESTBED_AGENT):]}>"
        return self.name(addr)

    def classify(self, row):
        """(external name or None, description of the peers)"""
        peers, is_host = self.of(row)
        if not peers:
            return None, "peer unknown" + ("" if is_host else " (no conn: no client IPs)")
        names = [self.peer_name(a, agent) for a, agent in peers]
        descs = []
        for a in dict.fromkeys(a for a, _ in peers):  # (an address once, with its User-Agents)
            ids = self.identities(a)[2:] + [f"User-Agent {agent}" for b, agent in peers if b == a and agent]
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
    ap.add_argument("--out", required=True, help="CSV of every type row (match_messages.FIELDS)")
    ap.add_argument("--api-ids", help="file of `<METHOD> <path> => <made-up api_id>` lines (see above)")
    ap.add_argument("--external", help="file of `<glob> => <name>` lines naming external peers (see above)")
    a = ap.parse_args()
    api_ids = ApiIds(read_api_ids(a.api_ids)) if a.api_ids else None
    peers = Peers(a.run, read_globs(a.external) if a.external else [])
    rows = match(a.run, api_ids, peers.record_server)
    with open(a.out, "w", newline="") as f:
        w = match_messages.csv.DictWriter(f, fieldnames=match_messages.FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {a.out}: {len(rows)} type rows; "
          + ", ".join(f"{k}: {n}" for k, n in sorted(collections.Counter(r['status'] for r in rows).items())))

    if api_ids:
        listing = f"{a.out[:-len('.csv')] if a.out.endswith('.csv') else a.out}.api_ids.md"  # (as .unmatched.md)
        api_ids.write(listing, a.api_ids)
        print(f"wrote {listing}")

    by_module = collections.defaultdict(list)
    for row in rows:
        if row["status"] == "unmatched-sender":
            by_module[row["snd_components"]].append(row)
        elif row["status"] == "unmatched-receiver":
            by_module[row["rcv_components"]].append(row)
    prefix = a.out[:-len(".csv")] if a.out.endswith(".csv") else a.out
    out = [f"# Messages only one side logged ({a.run}" + (f"; --api-ids {a.api_ids}" if a.api_ids else "")
           + (f"; --external {a.external}" if a.external else "") + ")", "",
           "Per module that logged them: first the unexplained ones, then the external ones, by the name --external "
           "gives their peer. Each line a match_messages type (its rows in "
           f"{os.path.basename(a.out)}), with its number of messages (x<n>), and its peer addresses [host: where a "
           "sent request went; from: the clients a received request came from], with what the testbed's address "
           "snapshot says they are.", ""]
    if not peers.addresses:
        out += ["(No address snapshot in this run: IPs aren't named.)", ""]
    totals = collections.Counter()
    for module, rs in sorted(by_module.items()):
        groups = collections.defaultdict(list)  # external name (None: unexplained) => lines
        for row in rs:
            name, peer = peers.classify(row)
            groups[name].append("- " + describe(row, peer))
        unexplained = len(groups.get(None, []))
        totals["unexplained"] += unexplained
        totals["external"] += len(rs) - unexplained
        out += [f"## {module}: {len(rs)} unmatched type(s), {unexplained} unexplained", ""]
        for name in sorted(groups, key=lambda n: (n is not None, n or "")):
            out += [f"### {'Unexplained' if name is None else 'External: ' + name} ({len(groups[name])})", ""]
            out += groups[name] + [""]
    open(f"{prefix}.unmatched.md", "w").write("\n".join(out))
    print(f"wrote {prefix}.unmatched.md: {totals['unexplained']} unexplained, {totals['external']} external")


if __name__ == "__main__":
    main()
