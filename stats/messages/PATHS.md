# message_paths: what applies to both groupings

message_paths.py groups messages into nodes in one of two ways (its `GROUPING` setting). The default since 2026-11-25
is `"labels"`: `merge`'s groups (rows of the same message kind between the same sender and receiver that share a label
on either side), with no splitting; the edges and parameters then say which concrete paths they were observed on
(Annotations, below). `"edges"` splits those groups by their edges, as [splitting/SPLITTING.md](splitting/SPLITTING.md)'s decisions say.
What follows applies to both.

## Annotations

An atom is a type row and one concrete path; a node groups atoms, so its messages can have several concrete paths on
each side. Edges are between atoms, in one module, so each edge's module logged both of its messages: the edge's
annotation (graph attribute `via`) keeps, per pair of the module's events that share a context ID, the concrete
path it logged each under. Printed (`via_text`) for the sides with several paths, e.g.
`==[grafana received the previous message as .../label/namespace/values and sent the next message as
/prometheus-meta/api/v1/label/namespace/values; ...]==>`; a response's path is its request's ("as the response to
..."). With one such side, or more than 5 pairs, the paths of each side are listed or counted (`show_paths`).

A parameter is annotated likewise with the concrete paths of the node's messages it was found on (`param_via`,
`param_via_text`): `(caddy sent this message as /api/v1/namespaces/caddy-system/secrets/...)`.

A node's two sides can be logged in separate runs (decision 8, and "Assumptions for convenience only" below), so a
node's sender-side paths aren't tied to its receiver-side paths: a path through a node can combine any edge in with
any edge out. The annotations keep, per edge, which concrete message led to which, so a query that tracks the concrete
path it's on while following edges can recover what splitting the nodes would show.

This still overapproximates - when finding paths, we follow any edge in to a node to any edge out, even though some edges in may not truly influence some edges out. However, I think this is the best we can do without assuming concrete paths match across the sender and receiver (see Decision 8).
This holds where a path crosses a node, from the sender's module to the receiver's: context IDs are per process, so
nothing ties the two. Within one module's stretch of a path, decision 7 does constrain it: the module's messages must
share a context ID, and a response resumes its caller's stretch, so e.g. a path that goes from Caddy into Grafana comes
back through Caddy messages that share a context ID with those it left by (one Caddy request could have handled them
all). Even splitting by the receiver's side too (`RECEIVER_SPLITS = True`) would only make
nodes finer; tying an edge in to an edge out across the crossing would take an ID that crosses processes (e.g. a
propagated trace header), a logging change.

## Paths that splice requests (decision 7)

Paths that splice requests: a node many requests share (e.g. one concrete path every request uses, not from grouping) lets a path enter through one request and leave through another's

**Decided:** Decided: while a path stays in one module, all that module's messages on that stretch must share one context ID in it (be one request's); the check restarts when the path moves to another module (context IDs are per process), except that a call out to another module and back (the module's request, then the response to it) resumes the caller's stretch, as one request still (2026-11-22, when etcd became a module). It applies to every path, with no definition of a hub needed: only nodes several requests share are affected. And a stretch entered by a request leaves through a response only if it's that request's (2026-11-25, after 8: a receiver's events under a route pattern attach to every client's message of the pattern, with their context IDs, so the ID check alone let a path enter by one request and leave by another's response)

**Per atom** (2026-11-26): a node can group several concrete messages (atoms), and its context IDs are those of all of them, so the check per node lets a path enter a node by one message and leave by another's events. With `ATOM_CONTEXT`, the path's state is instead the set of concrete messages it can be on: an edge goes on from an atom only through the pairs of atoms it joins that start at it (graph attribute `atom_edges`), each checked as above with the two atoms' own context IDs, and the request a stretch was entered by is an atom, whose own response the stretch must leave by. A node-level path is kept if some atom-level path realizes it. On `results/runs/2026-11-22-all6` (`GROUPING=labels`): the same 458 maximal paths (232 of at least 4 messages) as per node, as every node path has such a realization; what changes is that the state says which (e.g. Caddy's GET of one secret, entering kube-apiserver, leaves by that secret's response, not another's in the same node). test_message_paths.py: `test_atom_context`

**Setting:** `CONTEXT_PATHS` = `True`; `ATOM_CONTEXT` = `True` (or `False`: per node); `ENTRY_RESPONSE` = `True` (or `False`)

**Example:** kube-apiserver => etcd `POST /etcdserverpb.KV/Range`: one type on each side, 55 edges in and (through its response) 57 out; before, through it, each node of sample path 10.1 after it was indirectly influenced by 460 sections (a path from a `DELETE .../pods/{name}` to a `GET .../pods` response). Now the longest paths (8) end in their own request's response (`PATCH .../events/{name}` ... `PATCH 200`), and that response (sample path 8.1's last node) is indirectly influenced by 5 sections; the etcd nodes themselves still are by up to 460, from every request that reaches them. Paths: 306 (18984 with `False`; 237 vs 9795 with `GROUPING=labels`). With etcd a module (`results/runs/2026-11-22-all6`), restarting after the call out to etcd let paths splice again: 23076 paths, the longest 10, and sample path 10.1's `/authorize` node indirectly influenced by 462 sections; resuming the caller's stretch: 310, the longest 8, and the non-etcd nodes of the sampled paths by at most 7 (the 2 more than before are etcd's own); the etcd nodes still by up to 462. `ENTRY_RESPONSE` on all6 (with 8): 521 maximal paths instead of 869, 269 of at least 4 messages instead of 618; every one removed entered kube-apiserver by one request and left by the response to another under the same pattern, e.g. Caddy's GET of one secret ending in the response to another's (282), or Prometheus' service-discovery request ending in kube-apiserver's response to Caddy's (test_message_paths.py: `test_entry_response`)

## Misc path-finding logic
Within a module, use timestamps so recv => send edges are "happens-before" (precisely, a shared context ID counts only if some event of the received message comes at or before some event of the sent message in it) - see `evidence_matrix.shared_ids`.
