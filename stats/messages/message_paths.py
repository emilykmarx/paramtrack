#!/usr/bin/env python3
"""The longest paths messages take through a paramtrack run's modules, from step 5's events of every module.

A node is a message between two endpoints: one type row of unmatched_messages.py's matching (match_messages: a sent
type and the received type it pairs with; a type only one side logged has its other side named by --external, as in
unmatched_messages.py), annotated with sender -> receiver. So a proxied message is two nodes: as the proxy received it,
and as the proxy sent it. External endpoints aren't nodes: they appear only as a message's sender or receiver.

Edges, X -> Y:
- within a module M, X received by M and Y sent by M: the received-message matrices' cell rule (evidence_matrix.py's
  shared_ids: some event of X and some event of Y in M share a context ID, but not only as one exchange's request and
  response), applied to the nodes' events rather than to the matrices' labels;
- a request and its own response (which the matrices leave out): X a request M received and Y M's response to it
  (match_messages' response types name their request's type); or, when the responder is external, X a request M sent
  and Y the response M received to it.

Prints every maximal path (from a node with no predecessor, or any node of a cycle nothing enters, until it can't go on
without repeating a node) of at least --min-length nodes, longest first, a line each: <length>: <node> => <node> => ...;
a message node is
<sender>-><receiver> req|resp <method> <path> [<code>] [(host <host>)] (the path: the receiver's route pattern when it
logged one; the host: the one the client sent the request to, when the client side was logged).

Usage: uv run --with pandas --with networkx --with tabulate --with matplotlib --with pyyaml --with numpy
python message_paths.py --run <paramtrack --out dir> [--api-ids f] [--external f] [--min-length N] [--grep regex]
"""

import argparse
import collections
import re
import sys

import networkx as nx

import unmatched_messages as um  # (next to this file: the matching)
import evidence_matrix  # (next to this file: the received matrices' cell rule)

SIDE_KIND = {"snd_req": "Request sent", "rcv_req": "Request received",
             "snd_resp": "Response sent", "rcv_resp": "Response received"}

Node = collections.namedtuple("Node", "message snd rcv stype rtype")  # stype/rtype: match_messages types or None


def build(run, api_ids=None, external=()):
    """The graph (networkx DiGraph) of a run's messages. Its graph attributes (g.graph) keep, for path_stats.py:
    "rows", unmatched_messages.py's type rows; "unmatched", (row, its peer's name or None if unexplained) for each
    unmatched row; "events", (module, side, type) => [number of events, first ts, last ts] (ts: Unix ns), each
    type as the module logged it (match_messages' sides: snd_req, rcv_req, snd_resp, rcv_resp); "nodes_of",
    (side, type) => the nodes it is a side of"""
    events = []  # (module, side, type, context ID, (method, path), ts)

    peers = um.Peers(run, external)

    def on_message(module, side, typ, ev):
        peers.record_server(module, side, typ, ev)
        cid = (ev.get("context") or {}).get("context_id")
        m = ev.get("message") or {}
        events.append((module, side, typ, cid, (m.get("req.Method"), m.get("req.URL.Path")), ev.get("ts")))

    sides = {}
    rows = um.match(run, api_ids, on_message, sides)
    by_id = {f"{name}#{i}": typ for name, side in sides.items() for typ, i in side.ids.items()}
    # The nodes, and the nodes of each (side, type)
    nodes_of = collections.defaultdict(list)
    unmatched = []
    for row in rows:
        req = row["message"] == "request"
        s, r = by_id.get(row.get("snd_type_id")), by_id.get(row.get("rcv_type_id"))
        if row["status"].startswith("matched"):
            snd, rcv = row["snd_components"], row["rcv_components"]
        else:
            name, desc = peers.classify(row)
            unmatched.append((row, name))
            name = name or f"<unexplained: {desc}>"
            snd, rcv = (row["snd_components"], name) if s else (name, row["rcv_components"])
        n = Node(row["message"], snd, rcv, s, r)
        if s:
            nodes_of[("snd_req" if req else "snd_resp", s)].append(n)
        if r:
            nodes_of[("rcv_req" if req else "rcv_resp", r)].append(n)

    g = nx.DiGraph(rows=rows, unmatched=unmatched)
    g.add_nodes_from(n for ns in nodes_of.values() for n in ns)
    # Each type's events per module: how many, and when the first and last were logged
    stats = {}
    for module, side, typ, _, _, ts in events:
        c = stats.setdefault((module, side, typ), [0, ts, ts])
        c[0] += 1
        if ts is not None:
            c[1] = ts if c[1] is None else min(c[1], ts)
            c[2] = ts if c[2] is None else max(c[2], ts)
    g.graph["events"], g.graph["nodes_of"] = stats, dict(nodes_of)

    # Within each module: the received matrices' cells, over (event kind, node) keys
    d = {"ids": collections.defaultdict(set), "exchanges": collections.defaultdict(set)}
    by_cid = collections.defaultdict(lambda: collections.defaultdict(set))  # module => cid => keys
    for module, side, typ, cid, ex, _ in events:
        if not cid:
            continue
        for n in nodes_of[(side, typ)]:
            key = (SIDE_KIND[side], n, module)
            d["ids"][key].add(cid)
            d["exchanges"][key].add((cid, ex))
            by_cid[module][cid].add(key)
    for module, cids in by_cid.items():
        pairs = {(a, b) for keys in cids.values() for a in keys if "received" in a[0]
                 for b in keys if "sent" in b[0]}
        for r, c in pairs:
            if evidence_matrix.shared_ids(d, r, c)[0]:
                g.add_edge(r[1], c[1])

    # A request and its own response
    reqs = [n for n in g if n.message == "request"]
    for n in [n for n in g if n.message == "response"]:
        if n.stype:  # (the responder logged it: its request as received)
            for q in reqs:
                if q.rtype == n.stype[2] and (q.snd, q.rcv) == (n.rcv, n.snd):
                    g.add_edge(q, n)
        else:  # (an external responder: its request as sent)
            for q in reqs:
                if q.stype == n.rtype[2] and (q.snd, q.rcv) == (n.rcv, n.snd):
                    g.add_edge(q, n)
    return g


def label(n):
    t = n.stype or n.rtype
    req = t if n.message == "request" else t[2]
    path = (n.rtype if n.message == "request" else n.rtype[2])[3] if n.rtype else req[3]
    code = f" {t[1]}" if n.message == "response" else ""
    # The client side's request type has the host it was sent to (a sent request's; a received response's request's)
    client = n.stype if n.message == "request" else (n.rtype[2] if n.rtype else None)
    host = f" (host {client[2]})" if client and client[2] else ""
    return f"{n.snd}->{n.rcv} {'req' if n.message == 'request' else 'resp'} {req[1]} {path}{code}{host}"


def starts(g):
    """Where maximal paths start: every node with no predecessor, and every node of a cycle no edge enters (e.g.
    Grafana's plugin downloads, going back and forth between grafana.com and storage.googleapis.com), which no such
    node reaches"""
    cond = nx.condensation(g)
    out = [n for n in g if g.in_degree(n) == 0]
    for c in cond:
        members = cond.nodes[c]["members"]
        if cond.in_degree(c) == 0 and len(members) > 1:
            out += sorted(members, key=label)
    return out


def maximal_paths(g):
    """Every simple path that starts at one of starts(g) and can't be extended (its last node has no successor off
    the path), by depth-first search"""
    succ = {n: list(g.successors(n)) for n in g}
    for s in starts(g):
        stack, on = [(s, iter(succ[s]))], {s}
        while stack:
            nxt = next((x for x in stack[-1][1] if x not in on), None)
            if nxt is None:
                on.discard(stack.pop()[0])
                continue
            on.add(nxt)
            stack.append((nxt, iter(succ[nxt])))
            if all(x in on for x in succ[nxt]):
                yield [n for n, _ in stack]


def add_args(ap):
    """The arguments build needs (also path_stats.py's and sample_paths.py's)"""
    ap.add_argument("--run", required=True, help="a paramtrack run's --out directory (all its modules)")
    ap.add_argument("--api-ids", help="as unmatched_messages.py's")
    ap.add_argument("--external", help="as unmatched_messages.py's")


def build_from_args(a):
    """build, from add_args' arguments"""
    g = build(a.run, um.ApiIds(um.read_api_ids(a.api_ids)) if a.api_ids else None,
              um.read_globs(a.external) if a.external else [])
    print(f"graph: {g.number_of_nodes()} nodes, {g.number_of_edges()} edges", file=sys.stderr)
    return g


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_args(ap)
    ap.add_argument("--min-length", type=int, default=4, help="shortest path printed, in nodes (default %(default)s)")
    ap.add_argument("--grep", help="only paths whose text matches this regex")
    a = ap.parse_args()
    g = build_from_args(a)
    rx = re.compile(a.grep) if a.grep else None
    paths = []
    for p in maximal_paths(g):
        if len(p) >= a.min_length:
            text = " => ".join(map(label, p))
            if not rx or rx.search(text):
                paths.append((len(p), text))
    paths.sort(key=lambda x: (-x[0], x[1]))
    for n, text in paths:
        print(f"{n}: {text}")
    print(f"{len(paths)} paths", file=sys.stderr)


if __name__ == "__main__":
    main()
