#!/usr/bin/env python3
"""The longest paths messages take through a paramtrack run's modules, from step 5's events of every module.

A node is a message between two endpoints: the type rows of unmatched_messages.py's matching (match_messages: a sent
type and the received type it pairs with; a type only one side logged has its other side named by --external, as in
unmatched_messages.py) between the same sender and receiver that share a label on either side, annotated with sender
=> receiver. A side's labels are contexttrack's (message_graph.py's _label_fields, on the side's events), so e.g. the
requests Caddy proxies to Grafana under one route pattern are one node, whatever paths Grafana received them as. A
proxied message is two nodes: as the proxy received it, and as the proxy sent it. External endpoints aren't nodes: they
appear only as a message's sender or receiver.

Edges, X -> Y:
- within a module M, X received by M and Y sent by M: the received-message matrices' cell rule (evidence_matrix.py's
  shared_ids: some event of X and some event of Y in M share a context ID, but not only as one exchange's request and
  response), applied to the nodes' events rather than to the matrices' labels;
- a request and its own response (which the matrices leave out): X a request M received and Y M's response to it
  (match_messages' response types name their request's type); or, when the responder is external, X a request M sent
  and Y the response M received to it.

Prints every maximal path (from a node with no predecessor, or any node of a cycle nothing enters, until it can't go on
without repeating a node) of at least --min-length nodes, longest first, a line each: <length>: <node> ==> <node> ==>
...; a message node is (label)
<sender> => <receiver> req|resp <method> [<code>]: {<sender's labels>} => {<receiver's labels>}
each label its route pattern or path (a Caddy pattern without its path=), {} for a side no module logged; when both
sides' labels are the same, just {<labels>}; at most SHOW_PATHS (5) per side, then ", ...<N> paths elided"; a single
label without the braces.

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
import message_graph  # noqa: E402  (contexttrack's, on the path unmatched_messages puts it on: message labels)

SIDE_KIND = {"snd_req": "Request sent", "rcv_req": "Request received",
             "snd_resp": "Response sent", "rcv_resp": "Response received"}

# stypes/rtypes: the match_messages types of its rows' sides; slabels/rlabels: those sides' labels (side_labels)
Node = collections.namedtuple("Node", "message snd rcv stypes rtypes slabels rlabels")


def side_labels(ev):
    """An event's label as contexttrack's message_graph labels it (_label_fields, on its route-stamped event), as a
    tuple of (field, value)"""
    return tuple(message_graph._label_fields(ev))


def merge(protos):
    """Nodes from (message, snd, rcv, stype or None, rtype or None, slabels, rlabels) rows: rows with the same
    message, sender and receiver that share a label on either side are one node (union-find)"""
    parent = list(range(len(protos)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    first = {}
    for i, (msg, snd, rcv, _, _, sl, rl) in enumerate(protos):
        for key in [(msg, snd, rcv, "s", lab) for lab in sl] + [(msg, snd, rcv, "r", lab) for lab in rl]:
            if key in first:
                parent[find(i)] = find(first[key])
            else:
                first[key] = i
    groups = collections.defaultdict(list)
    for i in range(len(protos)):
        groups[find(i)].append(protos[i])
    node_of = {}
    for ps in groups.values():
        msg, snd, rcv = ps[0][:3]
        n = Node(msg, snd, rcv, frozenset(p[3] for p in ps if p[3]), frozenset(p[4] for p in ps if p[4]),
                 frozenset().union(*(p[5] for p in ps)), frozenset().union(*(p[6] for p in ps)))
        for p in ps:
            node_of[id(p)] = n
    return [node_of[id(p)] for p in protos]


def build(run, api_ids=None, external=()):
    """The graph (networkx DiGraph) of a run's messages. Its graph attributes (g.graph) keep, for path_stats.py:
    "rows", unmatched_messages.py's type rows; "unmatched", (row, its peer's name or None if unexplained) for each
    unmatched row; "events", (module, side, type) => [number of events, first ts, last ts] (ts: Unix ns), each
    type as the module logged it (match_messages' sides: snd_req, rcv_req, snd_resp, rcv_resp); "nodes_of",
    (side, type) => the nodes it is a side of"""
    events = []  # (module, side, type, context ID, (method, path), ts)
    labels = collections.defaultdict(set)  # (side, type) => its events' labels (side_labels)

    peers = um.Peers(run, external)

    def on_message(module, side, typ, ev):
        peers.record_server(module, side, typ, ev)
        labels[(side, typ)].add(side_labels(ev))
        cid = (ev.get("context") or {}).get("context_id")
        m = ev.get("message") or {}
        events.append((module, side, typ, cid, (m.get("req.Method"), m.get("req.URL.Path")), ev.get("ts")))

    sides = {}
    rows = um.match(run, api_ids, on_message, sides)
    by_id = {f"{name}#{i}": typ for name, side in sides.items() for typ, i in side.ids.items()}
    # The nodes (rows merged by label: merge), and the nodes of each (side, type)
    unmatched, protos = [], []
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
        ss, rs = ("snd_req", "rcv_req") if req else ("snd_resp", "rcv_resp")
        protos.append((row["message"], snd, rcv, s, r, frozenset(labels[(ss, s)]) if s else frozenset(),
                       frozenset(labels[(rs, r)]) if r else frozenset()))
    nodes_of = collections.defaultdict(list)
    for p, n in zip(protos, merge(protos)):
        req = p[0] == "request"
        for side, typ in (("snd_req" if req else "snd_resp", p[3]), ("rcv_req" if req else "rcv_resp", p[4])):
            if typ and n not in nodes_of[(side, typ)]:
                nodes_of[(side, typ)].append(n)

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

    # A request and its own response: the responder's response types name their request's type as it received it;
    # when the responder is external, the client's name it as it sent it
    by_rtype, by_stype = collections.defaultdict(set), collections.defaultdict(set)
    for q in (n for n in g if n.message == "request"):
        for t in q.rtypes:
            by_rtype[t].add(q)
        for t in q.stypes:
            by_stype[t].add(q)
    for n in [n for n in g if n.message == "response"]:
        qs = set().union(*(by_rtype[t[2]] for t in n.stypes)) if n.stypes else \
            set().union(*(by_stype[t[2]] for t in n.rtypes))
        for q in qs:
            if (q.snd, q.rcv) == (n.rcv, n.snd):
                g.add_edge(q, n)
    return g


SHOW_PATHS = 5  # (label: the patterns or paths shown per side; the rest are counted)


def label(n):
    """A node as the docstring says: <sender> => <receiver> req|resp <method> [<code>]: {<sender's labels>} =>
    {<receiver's labels>}"""
    def field(lab, *names):
        f = dict(lab)
        return next((f[k] for k in names if f.get(k)), None)

    def paths(labs):  # (a side's patterns or paths, sorted)
        return sorted({field(lab, "pattern", "path", "req.URL.Path") for lab in labs} - {None})

    def show(ps):  # (at most SHOW_PATHS of them; one without braces)
        if len(ps) == 1:
            return ps[0].removeprefix("path=")
        more = f", ...{len(ps) - SHOW_PATHS} paths elided" if len(ps) > SHOW_PATHS else ""
        return "{" + ", ".join(p.removeprefix("path=") for p in ps[:SHOW_PATHS]) + more + "}"
    labs = n.slabels | n.rlabels
    method = "/".join(sorted({field(lab, "method", "req.Method") for lab in labs} - {None}))
    code = "/".join(sorted({field(lab, "code", "resp.StatusCode") for lab in labs} - {None}))
    kind = "req" if n.message == "request" else "resp"
    sl, rl = paths(n.slabels), paths(n.rlabels)
    return (f"{n.snd} => {n.rcv} {kind} {method}{' ' + code if code and n.message == 'response' else ''}: "
            + (show(sl) if sl == rl else f"{show(sl)} => {show(rl)}"))


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
            text = " ==> ".join(map(label, p))
            if not rx or rx.search(text):
                paths.append((len(p), text))
    paths.sort(key=lambda x: (-x[0], x[1]))
    for n, text in paths:
        print(f"{n}: {text}")
    print(f"{len(paths)} paths", file=sys.stderr)


if __name__ == "__main__":
    main()
