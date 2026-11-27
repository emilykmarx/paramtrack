Below focuses on path and pattern, eliding fields that are matched exactly:
  - api_id
  - method
  - response code
  - send module name
  - receive module name
  - request vs response

Important notes for composability, i.e. things we must keep in mind given that each module's data comes from its own tests:
- The client and server may not log the same path for a given message type, since the data comes from separate tests which often choose different values for dynamic parts of the path (e.g. kubernetes namespaces, user IDs)
- The client and server do not propagate any per-message ID

Thus we focus primarily on patterns, not paths, which also helps with supporting generic querying.

# Grouping and matching
- The module tests log individual messages
  - (contexttrack's patches)
- We match them using router-like logic
  - (contexttrack's match_messages.py)
  - Exceptions:
    - Server never logged "request routed" (e.g. it rejected the request as unauthorized) => see "unrouted" logic
- We group them into message nodes by the server's label (a route pattern)
  - i.e. the receiver for requests, and the sender for responses
  - Exceptions:
    - Server is an external endpoint => group by client's label
- We keep additional info in annotations:
  - The client's concrete paths as a node annotation
    - i.e. "sent /hello/X and /hello/Y, both handled by /hello/*"
  - The path pairs leading from one node to the next as edge annotations
    - i.e. "on receiving pathX, sent pathY"

# Path-finding
- We find paths through message nodes ignoring their edge annotations
  - (message_paths.py)
- We add parameter info to the message paths after finding them
  - (path_influence.py)
- We must overapproximate to some extent, due to the composability notes above.
  - That is, when finding paths we generally follow every node's edge in to every edge out, even though not all edge pairs may really be related.
  - Two ways we limit the impact:
    - (1) The edge annotations may help a user manually guess which edges are related
    - (2) We make a few exceptions - see PATHS.md

# Path format (for sample_paths.py - message_paths.py is different)

<sender> => <receiver> req|resp <method> [<code>]: <node annotations>
[<edge annotations>]

Node annotations:
{<sender paths/pattern>} => {<receiver paths/pattern>}
(Only show one when they're the same)

Edge annotations:
Path pairs leading from this node to the next
(Elide for request/corresponding response; elide a side when only one path is possible, i.e. sender or receiver only has one path in the node annotation)


## Assumptions for convenience only

The matching and the grouping must still work if each module's testbed runs (and source-code analysis) happen
separately, e.g. Caddy run in prombench and kube-apiserver from its own tests; they may take input from several
modules' runs, but not assume those were one run (the same concrete paths, clocks or addresses). Two things here do
assume one shared run, only to make the results easier to read; neither decides what pairs or groups:
- **The run's cutoff** (`events.py`): every module's events are cut at the earliest of the modules' last event times,
  so that all modules cover the same stretch of the run. With separate runs, the times aren't comparable.
- **Naming external peers from the testbed's address snapshot** (`unmatched_messages.py`'s `Peers`): an unmatched
  message's peer address is named by what the run's `testbed/<step>/addresses.json` says it is (a pod, node or
  service), and an address a module served on makes the message unexplained rather than external. With separate runs,
  an address means something only in its own run.
