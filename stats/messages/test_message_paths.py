"""Tests for message_paths.py's grouping: GROUPING.md's "Decision 8 by example" (the "hello" example), on synthetic
events.

Run from this directory: uv run --with pandas --with networkx --with tabulate --with matplotlib --with pyyaml
--with numpy python -m unittest test_message_paths
"""

import contextlib
import io
import textwrap
import unittest
from unittest import mock

import message_paths as mp
import param_cells
import sample_paths
import networkx as nx
import unmatched_messages as um

HELLO, MSG = "example.com/hello", "example.com/msg"


def sent(cid, host, path, api_id):
    return {"kind": "Request sent", "pid": 1, "context": {"context_id": cid}, "api_id": api_id, "ts": 1,
            "request_id": {"method": "GET", "path": path},
            "message": {"req.Method": "GET", "req.URL.Host": host, "req.URL.Path": path}}


def received(cid, path, pattern, api_id):
    """A received request and the router's "Request routed" event for it"""
    msg = {"req.Method": "GET", "req.URL.Path": path}
    return [{"kind": "Request received", "pid": 1, "context": {"context_id": cid}, "api_id": api_id, "ts": 1,
             "message": msg},
            {"kind": "Request routed", "pid": 1, "context": {"context_id": cid}, "api_id": api_id, "ts": 1,
             "message": {**msg, "pattern": pattern}}]


def hello(client_paths):
    """GROUPING.md's example: the client sends GET client_paths (two, influenced by its parameters pX and pY); the
    server, routing them under /hello/*, logged /hello/1 (while handling it, it sends M1, /msg/1) and /hello/2 (M2,
    /msg/2); backend receives M1 and M2 under /msg/{n}. The paths, as sample_paths.py prints them
    (path_lines, without its heading lines; dedented, a blank line between paths), and the graph"""
    x, y = client_paths
    events = {
        "client": [sent("c1", "server:80", x, HELLO), sent("c2", "server:80", y, HELLO)],
        "server": received("s1", "/hello/1", "/hello/*", HELLO) + [sent("s1", "backend:80", "/msg/1", MSG)]
                  + received("s2", "/hello/2", "/hello/*", HELLO) + [sent("s2", "backend:80", "/msg/2", MSG)],
        "backend": received("b1", "/msg/1", "/msg/{n}", MSG) + received("b2", "/msg/2", "/msg/{n}", MSG),
    }
    cells = {("client", "Request sent", "GET", x, None): {("client", "pX")},
             ("client", "Request sent", "GET", y, None): {("client", "pY")}}
    with mock.patch.object(um, "module_events", lambda run: {m: m for m in events}), \
            mock.patch.object(um, "read_events", lambda path: events[path]), \
            mock.patch.object(param_cells, "table", lambda run: cells), \
            contextlib.redirect_stderr(io.StringIO()):
        g = mp.build("hello-run")
    paths = sorted("\n".join(sample_paths.path_lines(g, p)) for p in mp.maximal_paths(g))
    return textwrap.dedent("\n\n".join(paths)), g


SEPARATE = ("/hello/X", "/hello/Y")  # (the client's paths in its own run)
SHARED = ("/hello/1", "/hello/2")  # (one shared run: the client logged the server's paths)


class HelloExample(unittest.TestCase):
    def setUp(self):
        saved = mp.RECEIVER_SPLITS
        self.addCleanup(setattr, mp, "RECEIVER_SPLITS", saved)

    def test_separate_runs(self):
        # (decision 8, the default: as GROUPING.md shows) /hello/* splits by the client's parameters, M1 and M2 by
        # the server's paths; every X/Y => M1/M2 combination, each annotated with the server's path
        mp.RECEIVER_SPLITS = False
        paths, g = hello(SEPARATE)
        self.assertEqual((g.number_of_nodes(), g.number_of_edges()), (4, 4))
        self.assertEqual(paths, """\
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
server => backend req GET: /msg/2 => /msg/{n} [paths: /msg/2]""")

    def test_separate_runs_receiver_splits(self):
        # The server's events match none of the client's paths: they fall back to every atom, unannotated, and M1
        # and M2, with the same edges in, merge
        mp.RECEIVER_SPLITS = True
        paths, g = hello(SEPARATE)
        self.assertEqual((g.number_of_nodes(), g.number_of_edges()), (3, 2))
        self.assertEqual(paths, """\
client => server req GET: /hello/X => /hello/* [paths: /hello/X]
server => backend req GET: {/msg/1, /msg/2} => /msg/{n}

client => server req GET: /hello/Y => /hello/* [paths: /hello/Y]
server => backend req GET: {/msg/1, /msg/2} => /msg/{n}""")

    def test_shared_run(self):
        # Decision 8 doesn't use that the client logged the server's paths: still every combination
        mp.RECEIVER_SPLITS = False
        paths, _ = hello(SHARED)
        self.assertEqual(paths.count("server => backend"), 4)

    def test_shared_run_receiver_splits(self):
        # With one shared run, RECEIVER_SPLITS ties each server event to the client's atom with its path: /hello/1
        # leads only to M1, /hello/2 only to M2
        mp.RECEIVER_SPLITS = True
        paths, g = hello(SHARED)
        self.assertEqual((g.number_of_nodes(), g.number_of_edges()), (4, 2))
        self.assertEqual(paths, """\
client => server req GET: /hello/1 => /hello/* [paths: /hello/1]
server => backend req GET: /msg/1 => /msg/{n} [paths: /msg/1]

client => server req GET: /hello/2 => /hello/* [paths: /hello/2]
server => backend req GET: /msg/2 => /msg/{n} [paths: /msg/2]""")


class Annotations(unittest.TestCase):
    """via_text: the receiver's paths if at most SHOW_PATHS, else their count, per pattern if the node's receiver
    side has several"""

    def annotate(self, patterns, received):
        """The text on an edge out of a node whose receiver labels are patterns, its receiver (server) having received
        received: {pattern: [paths]}"""
        labs = frozenset((("api_id", HELLO), ("pattern", pat), ("method", "GET")) for pat in patterns)
        u = mp.Node("request", "client", "server", frozenset(), frozenset(), frozenset(), labs, frozenset())
        v = mp.Node("request", "server", "backend", frozenset(), frozenset(), frozenset(), frozenset(), frozenset())
        g = nx.DiGraph(via_patterns={("server", "rcv_req", p): {pat} for pat, ps in received.items() for p in ps},
                       own_paths={u: {"/hello/X"}, v: {"/msg/1"}})
        g.add_edge(u, v, via={("server", ("GET", p), None) for ps in received.values() for p in ps})
        return mp.via_text(g, u, v)

    def test_own_paths_not_annotated(self):
        # (the receiver logged just the node's own path: nothing to add)
        self.assertEqual(self.annotate(["/hello/*"], {"/hello/*": ["/hello/X"]}), "")

    def test_listed(self):
        self.assertEqual(self.annotate(["/hello/*"], {"/hello/*": ["/hello/1", "/hello/2"]}),
                         "server received the previous message as /hello/1, /hello/2")

    def test_counted(self):
        # (the pattern is in the node's label already)
        self.assertEqual(self.annotate(["/hello/*"], {"/hello/*": [f"/hello/{i}" for i in range(6)]}),
                         "server received the previous message as 6 paths")

    def test_counted_per_pattern(self):
        self.assertEqual(self.annotate(["/hello/*", "/bye/*"], {"/hello/*": [f"/hello/{i}" for i in range(4)],
                                                                "/bye/*": ["/bye/1", "/bye/2"]}),
                         "server received the previous message as 2 paths under /bye/*, 4 paths under /hello/*")


if __name__ == "__main__":
    unittest.main()
