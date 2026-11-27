# Grouping message_paths' nodes only when it doesn't change their edges: design decisions

Before this change, a node was a group of `merge` (message_paths.py): the type rows of the same message kind between
the same sender and receiver that share a label (a route pattern or path, with api_id and method) on either side,
transitively. Now that grouping is only the starting point: `refine` splits each group's atoms (a row and one concrete
path) until they differ in no edges that matter (decision 3). A request atom's edges are its sender's side only
(decision 8): its edges in (the receives that share a context ID with the send, each with the concrete path the
sender's module logged the receive under) and its sender's parameters (step 6's join redone per event, keyed by
concrete path: param_cells.py). A response splits as its request does (decision 6).


Since 2026-11-25 message_paths' default is `GROUPING=labels`: no splitting, with annotations instead; these decisions
are `GROUPING=edges`'s. What applies to both groupings (the annotations, decision 7) is in [PATHS.md](../PATHS.md), and the
assumptions for convenience only are in [GROUPING_MATCHING_PATHS.md](../GROUPING_MATCHING_PATHS.md).

Examples are from results/runs/2026-11-18-check-all5.

Each decision is a setting at the top of message_paths.py (Setting column), which
`--decision NAME=VALUE` overrides (message_paths.py, path_stats.py, sample_paths.py), e.g. `--decision COMPARE=equality`.
`GROUPING=labels` is the grouping before these decisions (with it, `CONTEXT_PATHS=False` reproduces the earlier output
exactly).

| # | Decision | Status | Setting | Example |
|---|---|---|---|---|
| 1 | Compare edge sets by equality or containment | Decided: containment. Paths merge when one's edges contain the other's: an edge means the message *could have* triggered (or been influenced by) the other, even if timing or caching kept it from happening; the merged node gets the union of the edges | `COMPARE` = `"containment"` (or `"equality"`) | kube-apiserver receives `/api/v1/namespaces/caddy-system/events` (7 events; sends etcd `KV/Txn`) and `.../default/events` (25; `KV/Txn` and `Lease/LeaseGrant`, the lease being reused between event writes): merged. With equality they aren't, and the graph has 833 nodes and 406 paths instead of 746 and 306 |
| 2 | Containment isn't transitive: with {A} ⊂ {A,B}, {A} ⊂ {A,C}, {A,B} and {A,C} incomparable, which group does {A} join? | Decided: merge only within a group with a single largest edge set (every member's edges contained in it); an {A} that could join several stays on its own, and incomparable paths stay apart | `AMBIGUOUS` = `"alone"` (or `"all"`: merge it, and so them, together) | Caddy's GET requests to Grafana under `["/grafana","/grafana/*"]`: three datasource-resource paths (`.../resources/api/v1/series`, `.../label/namespace/values`, `.../label/node/values`) each have one more edge than the others (their Prometheus query), so they're three incomparable largest sets; the other 76 paths (pages, scripts, icons, `/grafana/api/search`, ...) are contained in all three, so they stay a node of their own. With `"all"`: 685 nodes, 245 paths |
| 3 | Edges depend on the neighbours' grouping, and splitting a node can split its neighbours | Decided: partition refinement, i.e. start from today's grouping, split wherever 1 and 2 separate paths, repeat until nothing changes (rather than comparing once, at the concrete-path level) | `REFINE` = `"fixpoint"` (or `"once"`) | The viewer's GET requests to Caddy (`["/grafana","/grafana/*"]`) split in the second pass, because Caddy's sends to Grafana split in the first (2's example): `.../resources/api/v1/series` and the two label-values paths then lead to different nodes. 9 splits in all, 1 in pass 2. With `"once"`: 742 nodes |
| 4 | A path with no observed edges: its empty set is contained in every set | Decided: merge (the containment rule as is) | `EMPTY_MERGES` = `True` | kube-apiserver receives `GET /api/v1/namespaces/prombench-16075/pods` (no edges) and `.../caddy-system/pods` (1) and `.../default/pods` (2, the largest set): merged. With `False`: 748 nodes |
| 5 | On the receiving side, do the parameters that directly influence the receive (its receiver-side matrix cells) count, as well as its sends? | Decided: yes, they count (only with 8's `RECEIVER_SPLITS = True`; otherwise they annotate) | `RECEIVER_PARAMS` = `True` | On `results/runs/2026-11-18-check-all5`, before decision 8 (so as with `RECEIVER_SPLITS = True`), no split depends on them: with `False`, the same 746 nodes, 724 edges and 306 paths |
| 6 | Responses | Decided: a response node splits wherever its request's node does (responses pair through their requests) | (none: `refine`'s `respond_blocks`) | 54 response nodes are parts of split groups, e.g. kube-apiserver => Prometheus `resp GET 200` on `/api/v1/nodes/{name}/proxy/{path:*}`: one node for the `.../proxy/metrics` paths and one for `.../proxy/metrics/cadvisor`, as their requests split |
| 7 | Paths that splice requests | Moved to [PATHS.md](../PATHS.md): it applies to both groupings | `CONTEXT_PATHS`, `ENTRY_RESPONSE` | |
| 8 | The receiver's side: a node's two sides are logged by different modules, possibly in separate runs (GROUPING_MATCHING_PATHS.md, "Assumptions for convenience only"), so the receiver's concrete paths needn't be the sender's (a client's `/hello/123`, a server's `/hello/456` under `/hello/{id}`) | Decided: a node splits only by its sender's side (its edges in, each with the concrete path its module logged the message in under, and its sender's parameters), all from the sender's run (example: "Decision 8 by example", below). The receiver's side annotates instead (PATHS.md, "Annotations"); a path through a node can then combine any edge in with any edge out. `RECEIVER_SPLITS = True` restores splitting by the receiver's side too (its edges out and, with 5, its parameters), which ASSUMES THE SENDER AND THE RECEIVER LOGGED THE SAME CONCRETE PATHS (one shared run): a server's event goes to the client's atom with its path | `RECEIVER_SPLITS` = `False` (or `True`) | 2's example (Caddy's sends to Grafana under `["/grafana","/grafana/*"]`) is now one node, whose edges to Grafana's Prometheus queries are annotated with Grafana's concrete paths (`grafana received the previous message as /grafana/api/datasources/uid/.../resources/api/v1/series`). On `results/runs/2026-11-22-all6`: 749 nodes, 724 edges, 618 paths (longest 8), against 772, 750 and 310 with `True` (identical to before this decision). Before rows differing only in host became one row for the atoms (message_paths.py's docstring), the default gave 755, 736 and 624 |

The sender's parameters count among a send's edges as part of the design, not a decision: `SENDER_PARAMS` = `True`.

## Decision 8 by example

A client C sends `GET /hello/X`, influenced by its parameter pX, and `GET /hello/Y`, influenced by pY. A server S
routes both under the pattern `/hello/*`. S's events come from a separate run, where it logged `/hello/1` and
`/hello/2` under `/hello/*`. While handling `/hello/1`, S sends M1, and while handling `/hello/2`, it sends M2.

- **Rows and atoms.** The matching pairs each of C's sent types with S's received type `/hello/*`, so there are two
  rows. The atoms take C's paths, since the client logged them: `/hello/X` and `/hello/Y`. The rows share the receiver's
  label `/hello/*`, so they start as one group (`merge`).
- **The receiver's events attach to every atom of the type, annotated.** S's received `/hello/1` event says nothing
  about which of C's paths it corresponds to, so it goes to both atoms, carrying `/hello/1` (`via`). The same holds for
  `/hello/2`. The edges are then:
  - `/hello/X` → M1 and `/hello/Y` → M1, both `[S received the previous message as /hello/1]`;
  - `/hello/X` → M2 and `/hello/Y` → M2, both `[S received the previous message as /hello/2]`.
- **The `/hello/*` group splits only by C's side.** Its signatures are C's: each atom's edges in (here none) and C's
  parameters, `{pX}` against `{pY}`. These are incomparable, so the group becomes two nodes, `/hello/X` and `/hello/Y`.
  If C's two sends had identical edges in and parameters, it would stay one node, however differently S handled
  `/hello/1` and `/hello/2`: S's side is a different run, and its paths can't be tied to C's.
- **M1 and M2 split by their own sender's side, which is S's.** Their edges in are S's receives, annotated with S's
  paths: M1's come `via /hello/1` and M2's `via /hello/2`. So, if they started in one group, their signatures differ
  and they stay apart. Both ends of these annotations come from S's run, so the split uses only that run.
- **Paths combine any edge in with any edge out.** The paths are X → M1, X → M2, Y → M1 and Y → M2, with an
  annotation saying which of S's paths each step went through. Nothing claims that X went to M1.

The paths message_paths.py finds on synthetic events for this example, laid out as sample_paths.py lays out a path
(a message per line, an edge's annotation on the line before the message it leads to; ../test_message_paths.py, checks this and the variants below). The example adds a third module, `backend`, which receives M1
(`/msg/1`) and M2 (`/msg/2`) under `/msg/{n}`, so that M1 and M2 start in one group. Requests only, no responses:
```
client => server req GET: /hello/X => /hello/* [paths: /hello/X]
  [server received the previous message as /hello/1]
server => backend req GET: /msg/1 => /msg/{n} [paths: /msg/1]

client => server req GET: /hello/X => /hello/* [paths: /hello/X]
  [server received the previous message as /hello/2]
server => backend req GET: /msg/2 => /msg/{n} [paths: /msg/2]

client => server req GET: /hello/Y => /hello/* [paths: /hello/Y]
  [server received the previous message as /hello/1]
server => backend req GET: /msg/1 => /msg/{n} [paths: /msg/1]

client => server req GET: /hello/Y => /hello/* [paths: /hello/Y]
  [server received the previous message as /hello/2]
server => backend req GET: /msg/2 => /msg/{n} [paths: /msg/2]
```

With `RECEIVER_SPLITS = True`, an S event goes only to the atom with its own path. In this example C's paths aren't
S's, so the event matches no atom and falls back to all of them. M1 and M2 then have the same edges in (from X and
from Y, with no paths to tell them apart), so they merge; the edges' annotations still pair what S received with what
it sent (PATHS.md, "Annotations"):
```
client => server req GET: /hello/X => /hello/* [paths: /hello/X]
  [server received the previous message as /hello/1 and sent the next message as /msg/1; server received the previous message as /hello/2 and sent the next message as /msg/2]
server => backend req GET: {/msg/1, /msg/2} => /msg/{n}

client => server req GET: /hello/Y => /hello/* [paths: /hello/Y]
  [server received the previous message as /hello/1 and sent the next message as /msg/1; server received the previous message as /hello/2 and sent the next message as /msg/2]
server => backend req GET: {/msg/1, /msg/2} => /msg/{n}
```
In one shared run, C would log `/hello/1`
and `/hello/2` itself, so `/hello/1` would lead only to M1 and `/hello/2` only to M2. The receiver's side would then
also split the `/hello/*` group, through its edges out and S's parameters. That gives two paths instead of four, but
the result is only valid when both sides logged the same concrete paths.
