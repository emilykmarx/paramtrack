#!/usr/bin/env python3
"""Watch/stream types and whether their events have outgoing edges (message_paths' within-module rule).

A stream: a Kubernetes watch (watch=true or a /watch/ path), a log follow (follow=true), etcd's streaming gRPC
methods (Watch, LeaseKeepAlive, Snapshot), or a websocket path (/ws). contexttrack logs a stream as one message, so a
stream's own events (watch events, frames) aren't nodes; this asks whether even the stream as a whole leads to anything.

Event level: a stream event is a received side of a stream exchange (the client's "Response received": the stream's
data arrives; the server's "Request received": the stream's request arrives). It has an outgoing edge if some sent event
of the same module shares its context ID, at or after it (their ts, the module's own clock), other than its own
exchange (same cid and (method, path): shared_ids' rule).
Node level: the message_paths nodes those types are in, and whether those nodes have out-edges (a node can hold a
watch and a list of the same path: message_paths' atoms are (method, path), no query).

Prints a markdown table (one row per module, side and stream type), then the counts of types with no such edge.

Usage: uv run --with pandas --with networkx --with tabulate --with matplotlib --with pyyaml --with numpy
python stream_edges.py --run <paramtrack --out dir> [--api-ids f] [--external f]
"""
import argparse
import collections
import re

import unmatched_messages as um  # (next to this file)
import message_paths as mp  # (next to this file)

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
mp.add_args(ap)
args = ap.parse_args()
GRPC_STREAMS = re.compile(r"/etcdserverpb\.(Watch/Watch|Lease/LeaseKeepAlive|Maintenance/Snapshot)$")


def stream_kind(m):
    path, q = m.get("req.URL.Path") or "", m.get("req.URL.RawQuery") or ""
    if re.search(r"(^|&)watch=(true|1)(&|$)", q) or "/watch/" in path:
        return "k8s watch"
    if re.search(r"(^|&)follow=(true|1)(&|$)", q):
        return "follow"
    if GRPC_STREAMS.search(path):
        return "grpc stream"
    if path.endswith("/api/live/ws") or path.endswith("/ws"):
        return "websocket"
    return None


events = []  # (module, side, typ, cid, (method, path), stream kind, ts)
orig_match = um.match


def match(run, api_ids=None, on_message=None, sides=None):
    def rec(module, side, typ, ev):
        m = ev.get("message") or {}
        events.append((module, side, typ, (ev.get("context") or {}).get("context_id"),
                       (m.get("req.Method"), m.get("req.URL.Path")), stream_kind(m), ev.get("ts")))
        on_message(module, side, typ, ev)
    return orig_match(run, api_ids, rec, sides)


um.match = match
g = mp.build_from_args(args)

# Stream exchanges are identified on the request events; responses don't carry the query. A (module, cid, mp) whose
# request (sent or received) was a stream marks that exchange's response events as stream too.
stream_ex = {}
for module, side, typ, cid, p, k, _ in events:
    if k and side in ("snd_req", "rcv_req") and cid:
        stream_ex[(module, side[:3], cid, p)] = k
sent = collections.defaultdict(set)  # (module, cid) => sent (side, (method, path), ts)s
for module, side, typ, cid, p, k, ts in events:
    if side.startswith("snd") and cid:
        sent[(module, cid)].add((side, p, ts))

# types: (module, role, mp, kind) => [events, events with an out edge, (side, typ)s]
types = collections.defaultdict(lambda: [0, 0, set()])
for module, side, typ, cid, p, k, ts in events:
    if side == "rcv_resp":
        k = stream_ex.get((module, "snd", cid, p))
        role = "client (stream data received)"
    elif side == "rcv_req":
        role = "server (stream request received)"
    else:
        continue
    if not k:
        continue
    t = types[(module, role, p, k)]
    t[0] += 1
    t[2].add((side, typ))
    own = ("snd_req" if side == "rcv_resp" else "snd_resp", p)  # its own exchange's other side (shared_ids)
    if cid and any((s_, q) != own and (ts is None or t_ is None or ts <= t_) for s_, q, t_ in sent[(module, cid)]):
        t[1] += 1

nodes_of = g.graph["nodes_of"]
rows = []
for (module, role, p, k), (n, e, sts) in sorted(types.items()):
    nodes = {nd for st in sts for nd in nodes_of.get(st, [])}
    # out-edges other than a request node's to its own response node (a response node has no such edges)
    nodes_out = sum(1 for nd in nodes if any(not (nd.message == "request" and v.message == "response"
                                                  and (v.snd, v.rcv) == (nd.rcv, nd.snd)) for v in g.successors(nd)))
    rows.append((module, role, k, f"{p[0]} {p[1]}", n, e, len(nodes), nodes_out))

print("| module | role | kind | message | events | with out-edge | nodes | nodes with out-edges |")
print("|---|---|---|---|---|---|---|---|")
for r in rows:
    print("| " + " | ".join(map(str, r)) + " |")
tot = collections.Counter()
for module, role, k, msg, n, e, nn, no in rows:
    tot[(role, "types")] += 1
    tot[(role, "types without")] += (e == 0)
print()
for k, v in sorted(tot.items()):
    print(k, v)
