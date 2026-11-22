#!/usr/bin/env python3
"""The longest paths messages take through a paramtrack run's modules, from step 5's events of every module.

A node is a message between two endpoints, annotated with sender => receiver: a group of atoms, an atom being one
type row of unmatched_messages.py's matching (match_messages: a sent type and the received type it pairs with; a type
only one side logged has its other side named by --external, as in unmatched_messages.py) and one concrete (method,
path) its events have. The grouping starts from the rows between the same sender and receiver that share a label on
either side (merge; a side's labels are contexttrack's, message_graph.py's _label_fields on the side's events, so e.g.
the requests Caddy proxies to Grafana under one route pattern start as one group), and splits a group's concrete
paths apart when their edges differ (refine), as the grouping decisions below say. A proxied message is two nodes: as
the proxy received it, and as the proxy sent it. External endpoints aren't nodes: they appear only as a message's
sender or receiver.

Edges, X -> Y (between atoms, so between the nodes they're in):
- within a module M, X received by M and Y sent by M: the received-message matrices' cell rule (evidence_matrix.py's
  shared_ids: some event of X and some event of Y in M share a context ID, but not only as one exchange's request and
  response), applied to the atoms' events rather than to the matrices' labels;
- a request and its own response (which the matrices leave out), with the same concrete path: X a request M received
  and Y M's response to it (match_messages' response types name their request's type); or, when the responder is
  external, X a request M sent and Y the response M received to it.

A path follows edges and, while it stays in one module, stays in one request there: all that module's messages on
the stretch share a context ID (decision 7: transition).

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
import param_cells  # (next to this file: the parameters that influence each concrete message)
import message_graph  # noqa: E402  (contexttrack's, on the path unmatched_messages puts it on: message labels)

SIDE_KIND = {"snd_req": "Request sent", "rcv_req": "Request received",
             "snd_resp": "Response sent", "rcv_resp": "Response received"}

# stypes/rtypes: the match_messages types of its rows' sides; slabels/rlabels: those sides' labels (side_labels);
# paths: its concrete (method, path)s when its label's group was split (refine), else empty
Node = collections.namedtuple("Node", "message snd rcv stypes rtypes slabels rlabels paths")


def side_labels(ev):
    """An event's label as contexttrack's message_graph labels it (_label_fields, on its route-stamped event), as a
    tuple of (field, value); with its own api_id, not one --api-ids made up (um.ORIGINAL_API_ID)"""
    if um.ORIGINAL_API_ID in ev:
        ev = {**ev, "api_id": ev[um.ORIGINAL_API_ID]}
        if ev["api_id"] is None:
            del ev["api_id"]
    return tuple(message_graph._label_fields(ev))


# Grouping decisions (GROUPING.md, next to this file, by row number): change these to evaluate alternatives.
# GROUPING picks the grouping itself: "labels" merges every row between the same endpoints that shares a label on a
# side (merge), as before the decisions; "edges" starts from that and splits it (refine), as the decisions say.
GROUPING = "edges"
COMPARE = "containment"  # 1: how two concrete paths' edge sets compare: "containment" (one contains the other) or
#                          "equality"
AMBIGUOUS = "alone"  # 2: a path whose edges several incomparable largest edge sets contain: "alone" (its own node:
#                      merge only within a group with a single largest set) or "all" (merge it, and so them, together)
REFINE = "fixpoint"  # 3: "fixpoint" (split until nothing changes: partition refinement) or "once" (one pass)
EMPTY_MERGES = True  # 4: a path with no edges merges (its empty set is contained in every set); False: its own node
RECEIVER_PARAMS = True  # 5: the parameters that directly influence a receive count among its edges
# 6: a response's node splits wherever its request's does (respond_blocks); no alternative implemented
CONTEXT_PATHS = True  # 7: a path stays in one request per module stretch (transition, maximal_paths, ancestors)
# (Not a decision: the sender's parameters count among a send's edges, SENDER_PARAMS; the design has them)
SENDER_PARAMS = True

# match_messages' sides: the client's (whose type has the concrete path) and the server's (a route pattern), per message
CLIENT = {"request": "snd_req", "response": "rcv_resp"}
SERVER = {"request": "rcv_req", "response": "snd_resp"}


def merge(protos):
    """Today's grouping, refine's starting point: the index of each (message, snd, rcv, stype or None, rtype or
    None, slabels, rlabels) row's group; rows with the same message, sender and receiver that share a label on either
    side are one group (union-find)"""
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
    return [find(i) for i in range(len(protos))]


def split(atoms, sig):
    """One group's atoms split by their edge sets sig(atom), as decisions 1, 2 and 4 say: a key per atom (atoms with
    the same key stay together)"""
    sigs = {a: frozenset(sig(a)) for a in atoms}
    if COMPARE == "equality":
        return sigs
    distinct = set(sigs.values())
    largest = [x for x in distinct if not any(x < y for y in distinct)]
    key = {}
    for x in distinct:
        if not x and not EMPTY_MERGES:
            key[x] = x
            continue
        holders = [m for m in largest if x <= m]
        key[x] = holders[0] if len(holders) == 1 else x
    if AMBIGUOUS == "all":  # (union-find: an x several largest sets contain merges them all)
        parent = {x: x for x in distinct}

        def find(x):
            while parent[x] != x:
                x = parent[x]
            return x
        for x in distinct:
            for m in (m for m in largest if x <= m and (x or EMPTY_MERGES)):
                parent[find(m)] = find(x)
        key = {x: find(x) for x in distinct}
    return {a: key[sigs[a]] for a in atoms}


def build(run, api_ids=None, external=(), params=False):
    """The graph (networkx DiGraph) of a run's messages. A node is a group of atoms: a type row of the matching and
    one concrete (method, path) its events have. params: also compute the parameters that directly influence each
    node (param_cells: needed for GROUPING "edges" anyway). Its graph attributes (g.graph) keep, for path_stats.py:
    "rows", unmatched_messages.py's type rows; "unmatched", (row, its peer's name or None if unexplained) for each
    unmatched row; "events", (module, side, type) => [number of events, first ts, last ts] (ts: Unix ns), each
    type as the module logged it (match_messages' sides: snd_req, rcv_req, snd_resp, rcv_resp); "nodes_of",
    (side, type) => the nodes it is a side of; and, for paths: "cids", (node, "snd" or "rcv") => the (module, context
    ID)s of its events on that side; "params", node => {"send": {(module, root, section)}, "receive": ...} (with
    params or GROUPING "edges"); and, to look into the grouping: "refine_log" (refine's splits), "node_of" (atom =>
    node) and "protos" (the rows, as merge takes them)"""
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

    def row_type(i, side):  # (row i's type on a side, or None)
        return protos[i][3] if side.startswith("snd") else protos[i][4]

    # Atoms: (row, (method, path)), the concrete paths of a row's client-side events (one, as the client's type has
    # its path), or of its server-side events when the client isn't logged
    rows_of = collections.defaultdict(list)  # (side, type) => rows
    for i, p in enumerate(protos):
        for side in (CLIENT[p[0]], SERVER[p[0]]):
            if row_type(i, side):
                rows_of[(side, row_type(i, side))].append(i)
    of_type = collections.defaultdict(set)  # (side, type) => its events' (method, path)s
    for _, side, typ, _, mp, _ in events:
        of_type[(side, typ)].add(mp)
    paths_of = []  # row => its atoms' (method, path)s
    for i, p in enumerate(protos):
        side = CLIENT[p[0]] if row_type(i, CLIENT[p[0]]) else SERVER[p[0]]
        paths_of.append(of_type[(side, row_type(i, side))] or {(None, None)})

    def atoms_of(side, typ, mp):  # (an event's atoms: its rows' with its path, or all its rows' if none has it)
        rs = rows_of[(side, typ)]
        out = [(i, mp) for i in rs if mp in paths_of[i]]
        return out or [(i, q) for i in rs for q in paths_of[i]]

    # Each atom's events: context IDs (by side, and as the matrices' cell rule wants them) and the within-module edges
    d = {"ids": collections.defaultdict(set), "exchanges": collections.defaultdict(set)}
    by_cid = collections.defaultdict(lambda: collections.defaultdict(set))  # module => cid => keys
    cids = collections.defaultdict(set)  # (atom, "snd" or "rcv") => (module, cid)
    for module, side, typ, cid, mp, _ in events:
        for a in atoms_of(side, typ, mp):
            if cid:
                key = (SIDE_KIND[side], a, module)
                d["ids"][key].add(cid)
                d["exchanges"][key].add((cid, mp))
                by_cid[module][cid].add(key)
                cids[(a, side[:3])].add((module, cid))
    edges = set()  # (atom, atom), within a module
    for module, by in by_cid.items():
        pairs = {(a, b) for keys in by.values() for a in keys if "received" in a[0] for b in keys if "sent" in b[0]}
        for r, c in pairs:
            if evidence_matrix.shared_ids(d, r, c)[0]:
                edges.add((r[1], c[1]))
    # A request and its own response, with the same concrete path: the responder's response types name their
    # request's type as it received it; when the responder is external, the client's name it as it sent it
    request_of = collections.defaultdict(set)  # response atom => its request atoms
    for i, p in enumerate(protos):
        if p[0] != "response":
            continue
        side, typ = ("rcv_req", p[3][2]) if p[3] else ("snd_req", p[4][2])
        for j in rows_of[(side, typ)]:
            if (protos[j][1], protos[j][2]) == (p[2], p[1]):
                for mp in paths_of[i] & paths_of[j]:
                    request_of[(i, mp)].add((j, mp))
    atoms = [(i, mp) for i in range(len(protos)) for mp in paths_of[i]]

    # Each atom's parameters (param_cells), on each side its module logged
    atom_params = {}
    if params or GROUPING == "edges":
        cells = param_cells.table(run)
        sent_kind = {"request": "Request sent", "response": "Response sent"}
        received_kind = {"request": "Request received", "response": "Response received"}
        for a in atoms:
            msg, snd, rcv, s, r = protos[a[0]][:5]
            code = (s or r)[1] if msg == "response" else None  # (a response type's code)
            out = {"send": set(), "receive": set()}
            for side, modules, kind in (("send", snd, sent_kind[msg]), ("receive", rcv, received_kind[msg])):
                for module in modules.split(";"):
                    out[side] |= {(module,) + t for t in cells.get((module, kind) + a[1] + (code,), ())}
            atom_params[a] = out

    # Group the atoms: today's grouping (merge), then, with GROUPING "edges", split it (refine)
    initial = merge(protos)
    block = {a: initial[a[0]] for a in atoms}
    refine_log = []
    if GROUPING == "edges":
        block = refine(atoms, protos, block, edges, request_of, atom_params, refine_log)
    parts = collections.defaultdict(set)  # initial group => its blocks
    for a in atoms:
        parts[initial[a[0]]].add(block[a])

    # The nodes
    members = collections.defaultdict(list)
    for a in atoms:
        members[block[a]].append(a)
    node_of, nodes_of = {}, collections.defaultdict(list)
    for b, ats in members.items():
        rs = sorted({i for i, _ in ats})
        msg, snd, rcv = protos[rs[0]][:3]
        split_ = len(parts[initial[rs[0]]]) > 1
        n = Node(msg, snd, rcv, frozenset(protos[i][3] for i in rs if protos[i][3]),
                 frozenset(protos[i][4] for i in rs if protos[i][4]),
                 frozenset().union(*(protos[i][5] for i in rs)), frozenset().union(*(protos[i][6] for i in rs)),
                 frozenset(mp for _, mp in ats) if split_ else frozenset())
        for a in ats:
            node_of[a] = n
        for i in rs:
            for side in (CLIENT[msg], SERVER[msg]):
                if row_type(i, side) and n not in nodes_of[(side, row_type(i, side))]:
                    nodes_of[(side, row_type(i, side))].append(n)

    g = nx.DiGraph(rows=rows, unmatched=unmatched, refine_log=refine_log, node_of=node_of, protos=protos)
    g.add_nodes_from(node_of.values())
    g.add_edges_from((node_of[a], node_of[b]) for a, b in edges)
    g.add_edges_from((node_of[q], node_of[a]) for a, qs in request_of.items() for q in qs)
    # Each type's events per module: how many, and when the first and last were logged
    stats = {}
    for module, side, typ, _, _, ts in events:
        c = stats.setdefault((module, side, typ), [0, ts, ts])
        c[0] += 1
        if ts is not None:
            c[1] = ts if c[1] is None else min(c[1], ts)
            c[2] = ts if c[2] is None else max(c[2], ts)
    g.graph["events"], g.graph["nodes_of"] = stats, dict(nodes_of)
    g.graph["cids"] = collections.defaultdict(set)
    for (a, side), cs in cids.items():
        g.graph["cids"][(node_of[a], side)] |= cs
    if atom_params:
        g.graph["params"] = collections.defaultdict(lambda: {"send": set(), "receive": set()})
        for a, ps in atom_params.items():
            for side in ps:
                g.graph["params"][node_of[a]][side] |= ps[side]
    return g


def refine(atoms, protos, block, edges, request_of, atom_params, log=None):
    """Split the grouping block (atom => group) as the decisions say, and return it: a request atom's edges are its
    sender's (the receives with edges into it, and its sender parameters: SENDER_PARAMS) and its receiver's (the sends
    its edges go to, and its receiver parameters: 5), each receive or send as its current group; within each group,
    split (1, 2, 4); repeat until nothing splits (3). A response atom's group is its request's (6). log: a list to
    append each split to, (pass, {atom: (its new group's key, its edge set)}), to see why groups split"""
    ins, outs = collections.defaultdict(set), collections.defaultdict(set)
    for a, b in edges:
        outs[a].add(b)
        ins[b].add(a)
    requests = [a for a in atoms if protos[a[0]][0] == "request"]
    responses = [a for a in atoms if protos[a[0]][0] == "response"]

    def respond_blocks(block):  # (6: a response's group: its starting group's, split as its requests' are)
        return {a: (block[a], frozenset(block[q] for q in request_of[a])) for a in responses}

    block = dict(block)
    block.update(respond_blocks(block))
    passes = 0
    while True:
        passes += 1
        def sig(a):
            out = {("in", block[x]) for x in ins[a]} | {("out", block[y]) for y in outs[a]}
            if SENDER_PARAMS:
                out |= {("send", p) for p in atom_params[a]["send"]}
            if RECEIVER_PARAMS:
                out |= {("receive", p) for p in atom_params[a]["receive"]}
            return out
        groups = collections.defaultdict(list)
        for a in requests:
            groups[block[a]].append(a)
        new = dict(block)
        for b, ats in groups.items():
            keys = split(ats, sig)
            for a, k in keys.items():
                new[a] = (b, k)
            if log is not None and len(set(keys.values())) > 1:
                log.append((passes, {a: (keys[a], frozenset(sig(a))) for a in ats}))
        new.update(respond_blocks(new))
        # (canonical group IDs, so an unchanged grouping compares equal)
        ids = {}
        new = {a: ids.setdefault(b, len(ids)) for a, b in sorted(new.items(), key=lambda x: repr(x[0]))}
        changed = len(set(new.values())) != len(set(block.values()))
        block = new
        if not changed or REFINE == "once":
            return block


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
    # (a node split from its label's group: its own concrete paths, since other nodes have the same label)
    own = f" [paths: {show(sorted({p for _, p in n.paths if p}))}]" if n.paths else ""
    return (f"{n.snd} => {n.rcv} {kind} {method}{' ' + code if code and n.message == 'response' else ''}: "
            + (show(sl) if sl == rl else f"{show(sl)} => {show(rl)}") + own)


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


def stretch_sides(g, u, v):
    """Decision 7: the module sides the edge u -> v enters and leaves through, with their (module, context ID)s:
    ((module, u's side), (module, v's side), u's, v's), or None if a side has no events with context IDs (no check).
    The edge's module is u's receiver and v's sender (a within-module edge, or a responder's request and response),
    or else u's sender and v's receiver (an external responder's request and response)"""
    cids = g.graph["cids"]
    if u.rcv == v.snd and cids.get((u, "rcv")) and cids.get((v, "snd")):
        enter, leave = (u.rcv, "rcv"), (v.snd, "snd")
    elif u.snd == v.rcv:
        enter, leave = (u.snd, "snd"), (v.rcv, "rcv")
    else:
        return None
    cu, cv = cids.get((u, enter[1]), set()), cids.get((v, leave[1]), set())
    return (enter, leave, cu, cv) if cu and cv else None


def transition(g, u, v, state):
    """Decision 7's state after the edge u -> v, given the state u was reached with (None at a path's start): (the
    side v is left through, the (module, context ID)s the messages of this module stretch share), None if unchecked,
    or False if they share none. A stretch is the messages one module handles in a row: it goes on while each edge
    enters a message through the same side (module and side) the previous edge left it through"""
    sides = stretch_sides(g, u, v) if CONTEXT_PATHS else None
    if not sides:
        return None
    enter, leave, cu, cv = sides
    shared = (state[1] if state and state[0] == enter else cu) & cv
    return (leave, shared) if shared else False


def maximal_paths(g):
    """Every simple path that starts at one of starts(g) and can't be extended (its last node has no successor off
    the path that decision 7 allows: transition), by depth-first search"""
    succ = {n: list(g.successors(n)) for n in g}

    def nexts(u, state):  # (u's successors off the path the rule allows, with their states)
        for v in succ[u]:
            if v not in on:
                st = transition(g, u, v, state)
                if st is not False:
                    yield v, st
    for s in starts(g):
        on = {s}
        stack = [(s, None, nexts(s, None))]
        while stack:
            nxt = next(stack[-1][2], None)
            if nxt is None:
                on.discard(stack.pop()[0])
                continue
            v, st = nxt
            on.add(v)
            stack.append((v, st, nexts(v, st)))
            if next(nexts(v, st), None) is None:
                yield [n for n, _, _ in stack]


def ancestors(g, n):
    """The nodes from which a path, as maximal_paths allows them (decision 7), reaches n"""
    if not CONTEXT_PATHS:
        return nx.ancestors(g, n)
    # Backwards from n; a state: (the stretch key v is left through, the shared context IDs), as transition's
    seen = collections.defaultdict(list)  # node => states it was reached with (a smaller set adds nothing new)
    out, todo = set(), [(n, None)]
    while todo:
        v, state = todo.pop()
        for u in g.predecessors(v):
            st = back(g, u, v, state)
            if st is False:
                continue
            if any(st == s or (st and s and st[0] == s[0] and st[1] <= s[1]) for s in seen[u]):
                continue
            seen[u].append(st)
            out.add(u)
            todo.append((u, st))
    out.discard(n)
    return out


def back(g, u, v, state):
    """transition, backwards: the state u is reached with from the state v is (None at the end node), or False"""
    sides = stretch_sides(g, u, v)
    if not sides:
        return None
    enter, leave, cu, cv = sides
    shared = cu & (state[1] if state and state[0] == leave else cv)
    return (enter, shared) if shared else False


def add_args(ap):
    """The arguments build needs (also path_stats.py's and sample_paths.py's)"""
    ap.add_argument("--run", required=True, help="a paramtrack run's --out directory (all its modules)")
    ap.add_argument("--api-ids", help="as unmatched_messages.py's")
    ap.add_argument("--external", help="as unmatched_messages.py's")
    ap.add_argument("--decision", action="append", default=[], metavar="NAME=VALUE",
                    help="override a grouping decision's setting (GROUPING, COMPARE, ...; True/False for the flags), "
                         "e.g. --decision COMPARE=equality; repeatable")


DECISIONS = ("GROUPING", "COMPARE", "AMBIGUOUS", "REFINE", "EMPTY_MERGES", "RECEIVER_PARAMS", "CONTEXT_PATHS",
             "SENDER_PARAMS")


def set_decisions(settings):
    """Apply --decision NAME=VALUE overrides to this module's decision settings"""
    for s in settings:
        name, _, value = s.partition("=")
        if name not in DECISIONS:
            sys.exit(f"--decision {s}: not one of {', '.join(DECISIONS)}")
        globals()[name] = {"True": True, "False": False}.get(value, value)


def build_from_args(a, params=False):
    """build, from add_args' arguments"""
    set_decisions(a.decision)
    g = build(a.run, um.ApiIds(um.read_api_ids(a.api_ids)) if a.api_ids else None,
              um.read_globs(a.external) if a.external else [], params)
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
