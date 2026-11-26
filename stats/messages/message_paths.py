#!/usr/bin/env python3
"""The longest paths messages take through a paramtrack run's modules, from step 5's events of every module.

A node is a message between two endpoints, annotated with sender => receiver: a group of atoms, an atom being one
type row of unmatched_messages.py's matching (match_messages: a sent type and the received type it pairs with; a type
only one side logged has its other side named by --external, as in unmatched_messages.py; rows that differ only in
their types' host count as one, as the host isn't used for matching) and one concrete (method, path) its events have.
The grouping is the rows of the same kind between the same sender and receiver that share a label of the server's
side, i.e. its route pattern (merge, MERGE_LABELS: a request's receiver, a response's sender; by the client's labels
when the server isn't logged; a side's labels are contexttrack's, message_graph.py's _label_fields on the side's
events, so e.g. the requests Caddy proxies to Grafana are one group per Grafana route pattern); with GROUPING "edges", a group's concrete paths are split apart when
their edges differ (refine, splitting/SPLITTING.md). An atom's concrete path is its client's (or its server's when no client
logged it); the edges and parameters are annotated with the concrete paths they were observed on (`via`, PATHS.md).
A proxied message is two nodes: as
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
the stretch share a context ID (decision 7: transition); a call out to another module and back, through its
response, is still that request.

Prints every maximal path (from a node with no predecessor, or any node of a cycle nothing enters, until it can't go on
without repeating a node) of at least --min-length nodes, longest first, a line each: <length>: <node> ==> <node> ==>
...; an edge with annotations ==[<module> received the previous message as <path> and sent the next message as <path>; ...]==>
(via_text: which concrete paths the edge was observed on, PATHS.md); a message
node is (label)
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


# Grouping decisions (splitting/SPLITTING.md, by row number): change these to evaluate alternatives.
# GROUPING picks the grouping itself: "labels" (the default since 2026-11-25) merges every row between the same
# endpoints that shares a label on a side (merge), and the edges' and parameters' annotations say which concrete paths
# each was observed on (PATHS.md); "edges" starts from that and splits it (refine), as splitting/SPLITTING.md's decisions 1-6 say.
GROUPING = "labels"
MERGE_LABELS = "server"  # merge's labels: "server" (the server's side: a request's receiver, a response's sender; a
#                          row with no server-side labels, its server being external, by its client's) or "both"
COMPARE = "containment"  # 1: how two concrete paths' edge sets compare: "containment" (one contains the other) or
#                          "equality"
AMBIGUOUS = "alone"  # 2: a path whose edges several incomparable largest edge sets contain: "alone" (its own node:
#                      merge only within a group with a single largest set) or "all" (merge it, and so them, together)
REFINE = "fixpoint"  # 3: "fixpoint" (split until nothing changes: partition refinement) or "once" (one pass)
EMPTY_MERGES = True  # 4: a path with no edges merges (its empty set is contained in every set); False: its own node
RECEIVER_PARAMS = True  # 5: the parameters that directly influence a receive count among its edges
# 6: a response's node splits wherever its request's does (respond_blocks); no alternative implemented
CONTEXT_PATHS = True  # 7: a path stays in one request per module stretch (transition, maximal_paths, ancestors)
ENTRY_RESPONSE = True  # 7, with CONTEXT_PATHS: a module stretch entered by a request leaves through a response only
#                        if it's that request's (transition, back); False: any response sharing a context ID
RECEIVER_SPLITS = False  # 8: a node also splits by its receiver's side (its edges out, and with 5 its receiver
#                          parameters), tying the receiver's events to the sender's concrete paths: assumes both
#                          logged the same concrete paths (one shared run). False: the receiver's side annotates the
#                          node's edges and parameters with its own concrete paths (edge attribute "via")
# (Not a decision: the sender's parameters count among a send's edges, SENDER_PARAMS; the design has them)
SENDER_PARAMS = True

# match_messages' sides: the client's (whose type has the concrete path) and the server's (a route pattern), per message
CLIENT = {"request": "snd_req", "response": "rcv_resp"}
SERVER = {"request": "rcv_req", "response": "snd_resp"}


def merge(protos):
    """The grouping (refine's starting point, with GROUPING "edges"): the index of each (message, snd, rcv, stype or
    None, rtype or None, slabels, rlabels) row's group; rows with the same message, sender and receiver that share a
    label are one group (union-find): a label of the server's side (MERGE_LABELS "server": a request's receiver, a
    response's sender, so its route pattern; a row whose server isn't logged, by its client's labels), or of either
    side ("both")"""
    parent = list(range(len(protos)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    first = {}
    for i, (msg, snd, rcv, _, _, sl, rl) in enumerate(protos):
        if MERGE_LABELS == "server":  # (the server's labels, its route patterns; the client's if it has none)
            server, client = (rl, sl) if msg == "request" else (sl, rl)
            keys = [(msg, snd, rcv, "server", lab) for lab in server] or [(msg, snd, rcv, "client", lab) for lab in client]
        else:
            keys = [(msg, snd, rcv, "s", lab) for lab in sl] + [(msg, snd, rcv, "r", lab) for lab in rl]
        for key in keys:
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

    # Rows that differ only in their types' host are one row for the atoms: the host isn't used for matching
    # (match_messages' docstring) and is run-specific (an address), so it mustn't keep messages apart. canon[i]: the
    # first such row, which stands for them all (same_rows[canon[i]])
    def no_host(t):  # (a request type (api_id, method, host, path) without its host; a response's, its request's)
        if t is None or len(t) == 3:
            return t and (t[0], t[1], no_host(t[2]))
        return (t[0], t[1], t[3])
    canon, first, same_rows = [], {}, collections.defaultdict(list)
    for i, p in enumerate(protos):
        canon.append(first.setdefault((p[0], p[1], p[2], no_host(p[3]), no_host(p[4])), i))
        same_rows[canon[i]].append(i)

    # Atoms: (row, (method, path)), the concrete paths of a row's client-side events (one, as the client's type has
    # its path), or of its server-side events when the client isn't logged; a row being its canon (the rows that
    # differ from it only in host: their paths too)
    rows_of = collections.defaultdict(list)  # (side, type) => rows (canon)
    for i, p in enumerate(protos):
        for side in (CLIENT[p[0]], SERVER[p[0]]):
            if row_type(i, side) and canon[i] not in rows_of[(side, row_type(i, side))]:
                rows_of[(side, row_type(i, side))].append(canon[i])
    of_type = collections.defaultdict(set)  # (side, type) => its events' (method, path)s
    for _, side, typ, _, mp, _ in events:
        of_type[(side, typ)].add(mp)
    paths_of, atom_side = [], []  # row => its atoms' (method, path)s, and the side they're from
    for i, p in enumerate(protos):
        side = CLIENT[p[0]] if row_type(i, CLIENT[p[0]]) else SERVER[p[0]]
        atom_side.append(side)
        paths_of.append(set().union(*(of_type[(side, row_type(j, side))] for j in same_rows[i])) or {(None, None)}
                        if canon[i] == i else set())

    def atoms_of(side, typ, mp):
        """An event's atoms, each with the event's concrete path as an annotation when the atoms' paths are the other
        side's (another run's: None otherwise). The other side's paths say nothing about which of them the event's
        is, so the event goes to all its rows' atoms (decision 8); with RECEIVER_SPLITS, to those with its path, or all
        its rows' if none has it (as if both sides logged the same paths)"""
        out = []
        for i in rows_of[(side, typ)]:
            if side != atom_side[i] and not RECEIVER_SPLITS:
                out += [((i, q), mp) for q in paths_of[i]]
            elif mp in paths_of[i]:
                out.append(((i, mp), None))
        return out or [((i, q), None) for i in rows_of[(side, typ)] for q in paths_of[i]]

    # Each atom's events: context IDs (by side, and as the matrices' cell rule wants them) and the within-module edges
    d = {"ids": collections.defaultdict(set), "exchanges": collections.defaultdict(set)}
    by_cid = collections.defaultdict(lambda: collections.defaultdict(set))  # module => cid => keys
    cids = collections.defaultdict(set)  # (atom, "snd" or "rcv") => (module, cid)
    # (module, side, concrete path) => the route patterns (or paths) of its events' types: a request's, a response's
    # request's (annotations: show_paths)
    via_patterns = collections.defaultdict(set)
    atom_side_paths = collections.defaultdict(set)  # (atom, "snd" or "rcv") => the concrete paths its events have
    for module, side, typ, cid, mp, _ in events:
        for a, via in atoms_of(side, typ, mp):
            via_patterns[(module, side, mp[1])].add(typ[3] if side.endswith("req") else typ[2][3])
            atom_side_paths[(a, side[:3])].add(mp[1])
            if cid:
                key = (SIDE_KIND[side], a, module, via)
                d["ids"][key].add(cid)
                d["exchanges"][key].add((cid, mp))
                by_cid[module][cid].add(key)
                cids[(a, side[:3])].add((module, cid))
    edges = set()  # (atom, atom, (module, the first's concrete path or None, the second's or None)), within a module
    # (atom, atom, module) => the concrete paths, (first's, second's), of their events that share a context ID: what
    # the module logged them under (the edges' annotations: via_text)
    edge_paths = collections.defaultdict(set)
    for module, by in by_cid.items():
        pairs = {(a, b) for keys in by.values() for a in keys if "received" in a[0] for b in keys if "sent" in b[0]}
        for r, c in pairs:
            if evidence_matrix.shared_ids(d, r, c)[0]:
                edges.add((r[1], c[1], (module, r[3], c[3])))
                sent_by_cid = collections.defaultdict(set)
                for cid, q in d["exchanges"][c]:
                    sent_by_cid[cid].add(q)
                edge_paths[(r[1], c[1], module)] |= {(p, q) for cid, p in d["exchanges"][r] for q in sent_by_cid[cid]}
    # A request and its own response, with the same concrete path: the responder's response types name their
    # request's type as it received it; when the responder is external, the client's name it as it sent it
    request_of = collections.defaultdict(set)  # response atom => its request atoms
    for i, p in enumerate(protos):
        if p[0] != "response":
            continue
        side, typ = ("rcv_req", p[3][2]) if p[3] else ("snd_req", p[4][2])
        for j in rows_of[(side, typ)]:
            if (protos[j][1], protos[j][2]) == (p[2], p[1]):
                for mp in paths_of[canon[i]] & paths_of[j]:
                    request_of[(canon[i], mp)].add((j, mp))
    atoms = [(i, mp) for i in range(len(protos)) for mp in paths_of[i]]

    # Each atom's parameters (param_cells), on each side its module logged
    atom_params = {}
    param_via = collections.defaultdict(set)  # (atom, side, param) => the (module, concrete path)s it was found on
    if params or GROUPING == "edges":
        cells = param_cells.table(run)
        sent_kind = {"request": "Request sent", "response": "Response sent"}
        received_kind = {"request": "Request received", "response": "Response received"}
        for a in atoms:
            msg, snd, rcv, s, r = protos[a[0]][:5]
            code = (s or r)[1] if msg == "response" else None  # (a response type's code)
            out = {"send": set(), "receive": set()}
            for side, modules, kind, ev_side in (("send", snd, sent_kind[msg], CLIENT[msg] if msg == "request"
                                                    else SERVER[msg]), ("receive", rcv, received_kind[msg],
                                                    SERVER[msg] if msg == "request" else CLIENT[msg])):
                # (a side whose concrete paths aren't the atom's: its parameters by its own paths, annotated: 8)
                cross = ev_side != atom_side[a[0]] and not RECEIVER_SPLITS
                ts = {row_type(j, ev_side) for j in same_rows[a[0]]} - {None}
                for module in modules.split(";"):
                    for q in (set().union(*(of_type[(ev_side, t)] for t in ts)) if cross and ts else [a[1]]):
                        for c in cells.get((module, kind) + q + (code,), ()):
                            out[side].add((module,) + c)
                            param_via[(a, side, (module,) + c)].add((module, q))
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
        rs = sorted({j for i, _ in ats for j in same_rows[i]})
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

    # (a Node is the graph's key for its block: two blocks with equal Nodes would silently become one node)
    assert len(set(node_of.values())) == len(members), "two groups of atoms made the same Node"
    g = nx.DiGraph(rows=rows, unmatched=unmatched, refine_log=refine_log, node_of=node_of, protos=protos)
    g.add_nodes_from(node_of.values())
    # (an edge's annotations, "via": (module, the concrete path its module logged the first message under, the
    # second's), per pair of the module's events that share a context ID: edge_paths)
    for a, b, (module, _, _) in edges:
        u, v = node_of[a], node_of[b]
        if not g.has_edge(u, v):
            g.add_edge(u, v, via=set())
        g.edges[u, v]["via"] |= {(module, p, q) for p, q in edge_paths[(a, b, module)]}
    g.graph["requests_of"] = collections.defaultdict(set)  # response node => its requests' nodes (request_of)
    for a, qs in request_of.items():
        for q in qs:
            g.graph["requests_of"][node_of[a]].add(node_of[q])
            if not g.has_edge(node_of[q], node_of[a]):
                g.add_edge(node_of[q], node_of[a], via=set())
    # Each type's events per module: how many, and when the first and last were logged
    stats = {}
    for module, side, typ, _, _, ts in events:
        c = stats.setdefault((module, side, typ), [0, ts, ts])
        c[0] += 1
        if ts is not None:
            c[1] = ts if c[1] is None else min(c[1], ts)
            c[2] = ts if c[2] is None else max(c[2], ts)
    g.graph["events"], g.graph["nodes_of"], g.graph["via_patterns"] = stats, dict(nodes_of), dict(via_patterns)
    # (node, "snd" or "rcv") => the concrete paths its sender's (receiver's) events have: an annotation names the paths
    # of a side only when it has several (show_side)
    g.graph["side_paths"] = collections.defaultdict(set)
    for (a, side), ps in atom_side_paths.items():
        g.graph["side_paths"][(node_of[a], side)] |= ps
    g.graph["cids"] = collections.defaultdict(set)
    for (a, side), cs in cids.items():
        g.graph["cids"][(node_of[a], side)] |= cs
    if atom_params:
        g.graph["params"] = collections.defaultdict(lambda: {"send": set(), "receive": set()})
        for a, ps in atom_params.items():
            for side in ps:
                g.graph["params"][node_of[a]][side] |= ps[side]
        # (node, "send" or "receive", param) => the (module, concrete path)s it was found on (param_via_text)
        g.graph["param_via"] = collections.defaultdict(set)
        for (a, side, prm), vs in param_via.items():
            g.graph["param_via"][(node_of[a], side, prm)] |= vs
    return g


def refine(atoms, protos, block, edges, request_of, atom_params, log=None):
    """Split the grouping block (atom => group) as the decisions say, and return it: a request atom's edges are its
    sender's (the receives with edges into it, and its sender parameters: SENDER_PARAMS) and its receiver's (the sends
    its edges go to, and its receiver parameters: 5), each receive or send as its current group; within each group,
    split (1, 2, 4); repeat until nothing splits (3). A response atom's group is its request's (6). log: a list to
    append each split to, (pass, {atom: (its new group's key, its edge set)}), to see why groups split"""
    ins, outs = collections.defaultdict(set), collections.defaultdict(set)
    for a, b, (_, va, _) in edges:
        outs[a].add(b)
        ins[b].add((a, va))
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
            if RECEIVER_SPLITS:  # (both sides: the old grouping)
                out = {("in", block[x]) for x, _ in ins[a]} | {("out", block[y]) for y in outs[a]}
            else:  # (the sender's side only: its edges in, each with its sender's concrete path for the message in)
                out = {("in", block[x], va) for x, va in ins[a]}
            if SENDER_PARAMS:
                out |= {("send", p) for p in atom_params[a]["send"]}
            if RECEIVER_PARAMS and RECEIVER_SPLITS:
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


def field(lab, *names):
    """A label's first non-empty field of names"""
    f = dict(lab)
    return next((f[k] for k in names if f.get(k)), None)


def side_paths(labs):
    """A side's labels' patterns or paths, sorted"""
    return sorted({field(lab, "pattern", "path", "req.URL.Path") for lab in labs} - {None})


def label(n):
    """A node as the docstring says: <sender> => <receiver> req|resp <method> [<code>]: {<sender's labels>} =>
    {<receiver's labels>}"""
    def show(ps):  # (at most SHOW_PATHS of them; one without braces)
        if len(ps) == 1:
            return ps[0].removeprefix("path=")
        more = f", ...{len(ps) - SHOW_PATHS} paths elided" if len(ps) > SHOW_PATHS else ""
        return "{" + ", ".join(p.removeprefix("path=") for p in ps[:SHOW_PATHS]) + more + "}"
    labs = n.slabels | n.rlabels
    method = "/".join(sorted({field(lab, "method", "req.Method") for lab in labs} - {None}))
    code = "/".join(sorted({field(lab, "code", "resp.StatusCode") for lab in labs} - {None}))
    kind = "req" if n.message == "request" else "resp"
    sl, rl = side_paths(n.slabels), side_paths(n.rlabels)
    # (a node split from its label's group: its own concrete paths, since other nodes have the same label)
    own = f" [paths: {show(sorted({p for _, p in n.paths if p}))}]" if n.paths else ""
    return (f"{n.snd} => {n.rcv} {kind} {method}{' ' + code if code and n.message == 'response' else ''}: "
            + (show(sl) if sl == rl else f"{show(sl)} => {show(rl)}") + own)


def as_text(n, which, side):
    """How an annotation names a node's message: "<module> received|sent the previous|this|next message as [the
    response to] <paths>" (which: previous, this or next; side: snd or rcv)"""
    verb = "sent" if side == "snd" else "received"
    return f"{verb} the {which} message as " + ("the response to " if n.message == "response" else "")


def show_side(g, n, side):
    """Whether annotations name the concrete paths of a node's side (snd or rcv): only if it has several"""
    return len(g.graph["side_paths"].get((n, side), ())) > 1


def via_text(g, u, v):
    """The edge u -> v's annotations, or "": per module (the edge's: u's receiver and v's sender), the concrete paths
    it logged u's message and v's under ("<module> received the previous message as <path> and sent the next message
    as <path>", a response's path being its request's: "as the response to <path>"), paired as the edge's atoms are,
    for the sides that have several paths (show_side); with only one such side, or more than SHOW_PATHS pairs, the
    paths on each side as show_paths shows them (and how many pairs)"""
    by = collections.defaultdict(set)
    for module, va, vb in g.edges[u, v].get("via", ()):
        by[module].add((va[1] if va and show_side(g, u, "rcv") else None,
                        vb[1] if vb and show_side(g, v, "snd") else None))
    parts = []
    for m in sorted(by):
        pairs = sorted(pr for pr in by[m] if pr != (None, None))
        if not pairs:
            continue
        rcvd, sent = {a for a, _ in pairs} - {None}, {b for _, b in pairs} - {None}
        if rcvd and sent and len(pairs) <= SHOW_PATHS:
            parts += [f"{m} {as_text(u, 'previous', 'rcv')}{a} and {as_text(v, 'next', 'snd')}{b}" for a, b in pairs]
            continue
        side_u = "rcv_req" if u.message == "request" else "rcv_resp"
        side_v = "snd_req" if v.message == "request" else "snd_resp"
        texts = ([as_text(u, "previous", "rcv") + show_paths(g, m, side_u, rcvd, u.rlabels)] if rcvd else []) + (
            [as_text(v, "next", "snd") + show_paths(g, m, side_v, sent, v.slabels)] if sent else [])
        parts.append(f"{m} " + " and ".join(texts) + (f" ({len(pairs)} pairs)" if rcvd and sent else ""))
    return "; ".join(parts)


def show_paths(g, module, side, ps, labs):
    """Annotated concrete paths ps, logged by module on side (match_messages' side name), of a node whose labels on
    that side are labs: the paths if at most SHOW_PATHS, else their count, per route pattern if labs has several"""
    ps = sorted(p for p in ps if p)
    if len(ps) <= SHOW_PATHS:
        return ", ".join(ps)
    if len({field(lab, "pattern") for lab in labs} - {None}) <= 1:
        return f"{len(ps)} paths"
    under = collections.Counter(pat for p in ps for pat in g.graph["via_patterns"].get((module, side, p), ()))
    return ", ".join(f"{n} paths under {pat.removeprefix('path=')}" for pat, n in sorted(under.items()))


def param_via_text(g, n, side, param):
    """The annotation of a node's parameter, or "": the concrete paths, as their module logged them, of the node's
    messages it was found on: "<module> received|sent this message as [the response to] <paths>", as show_paths shows
    them, for a side with several paths (show_side)"""
    s = "rcv" if side == "receive" else "snd"
    if not show_side(g, n, s):
        return ""
    by = collections.defaultdict(set)
    for module, (_, path) in g.graph.get("param_via", {}).get((n, side, param), ()):
        by[module].add(path)
    ms = {"request": {"snd": "snd_req", "rcv": "rcv_req"}, "response": {"snd": "snd_resp", "rcv": "rcv_resp"}}
    return "; ".join(f"{m} {as_text(n, 'this', s)}"
                     f"{show_paths(g, m, ms[n.message][s], by[m], n.slabels if s == 'snd' else n.rlabels)}"
                     for m in sorted(by))


def path_text(g, p):
    """A path as main prints it: its nodes' labels joined by ==>, an annotated edge as ==[<via_text>]==>"""
    out = label(p[0])
    for u, v in zip(p, p[1:]):
        via = via_text(g, u, v)
        out += (f" ==[{via}]==> " if via else " ==> ") + label(v)
    return out


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
    side v is left through, the (module, context ID)s the messages of this module stretch share, the calls out
    pending), None if unchecked, or False if they share none. A stretch is the messages one module handles in a row:
    it goes on while each edge enters a message through the same side (module and side) the previous edge left it
    through. A stretch that ends with the module sending a request to another module is a call out: it's set aside
    (pushed: (module, its shared IDs)), and when the path comes back to that module through a response, the stretch
    resumes from it (popped), as one request's still, rather than restarting. The state also keeps the request a
    stretch was entered by (its entry, kept across calls out): with ENTRY_RESPONSE, the stretch leaves through a
    response only if it answers that request (requests_of), since a request's handling sends its own response. Without
    it, decision 8 lets a path leave through another client's response: the receiver's events under a route pattern
    attach to every client's message of the pattern, and so do their context IDs"""
    sides = stretch_sides(g, u, v) if CONTEXT_PATHS else None
    if not sides:
        return None
    enter, leave, cu, cv = sides
    calls = state[2] if state else ()
    entry = state[3] if state else None
    if state and state[0] == enter:
        base = state[1]
    else:
        if state and state[0][1] == "snd" and u.message == "request":
            calls += ((state[0][0], state[1], entry),)
        if enter[1] == "rcv" and u.message == "response" and calls and calls[-1][0] == enter[0]:
            base, entry, calls = calls[-1][1] & cu, calls[-1][2], calls[:-1]
        else:
            base, entry = cu, (u if enter[1] == "rcv" and u.message == "request" else None)
    if (ENTRY_RESPONSE and entry is not None and leave[1] == "snd" and v.message == "response"
            and entry not in g.graph["requests_of"].get(v, ())):
        return False
    shared = base & cv
    return (leave, shared, calls, entry) if shared else False


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
            if any(st == s or (st and s and st[0] == s[0] and st[2] == s[2] and st[3] == s[3] and st[1] <= s[1]) for s in seen[u]):
                continue
            seen[u].append(st)
            out.add(u)
            todo.append((u, st))
    out.discard(n)
    return out


def back(g, u, v, state):
    """transition, backwards: the state u is reached with from the state v is (None at the end node), or False. A
    call out, backwards: a stretch that begins with the module receiving a response from another module is set
    aside, and resumes when the path, going back, reaches the module's request. ENTRY_RESPONSE, backwards: the
    response a stretch leaves through (its exit) must answer the request it was entered by"""
    sides = stretch_sides(g, u, v)
    if not sides:
        return None
    enter, leave, cu, cv = sides
    calls = state[2] if state else ()
    exit_ = state[3] if state else None
    if state and state[0] == leave:
        base = state[1]
    else:
        if state and state[0][1] == "rcv" and v.message == "response":
            calls += ((state[0][0], state[1], exit_),)
        if leave[1] == "snd" and v.message == "request" and calls and calls[-1][0] == leave[0]:
            base, exit_, calls = calls[-1][1] & cv, calls[-1][2], calls[:-1]
        else:
            base, exit_ = cv, (v if leave[1] == "snd" and v.message == "response" else None)
    if (ENTRY_RESPONSE and exit_ is not None and enter[1] == "rcv" and u.message == "request"
            and u not in g.graph["requests_of"].get(exit_, ())):
        return False
    shared = cu & base
    return (enter, shared, calls, exit_) if shared else False


def add_args(ap):
    """The arguments build needs (also path_stats.py's and sample_paths.py's)"""
    ap.add_argument("--run", required=True, help="a paramtrack run's --out directory (all its modules)")
    ap.add_argument("--api-ids", help="as unmatched_messages.py's")
    ap.add_argument("--external", help="as unmatched_messages.py's")
    ap.add_argument("--decision", action="append", default=[], metavar="NAME=VALUE",
                    help="override a grouping decision's setting (GROUPING, COMPARE, ...; True/False for the flags), "
                         "e.g. --decision COMPARE=equality; repeatable")


DECISIONS = ("GROUPING", "MERGE_LABELS", "COMPARE", "AMBIGUOUS", "REFINE", "EMPTY_MERGES", "RECEIVER_PARAMS", "CONTEXT_PATHS", "ENTRY_RESPONSE",
             "RECEIVER_SPLITS", "SENDER_PARAMS")


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
            text = path_text(g, p)
            if not rx or rx.search(text):
                paths.append((len(p), text))
    paths.sort(key=lambda x: (-x[0], x[1]))
    for n, text in paths:
        print(f"{n}: {text}")
    print(f"{len(paths)} paths", file=sys.stderr)


if __name__ == "__main__":
    main()
