"""Tests for message_paths.py's grouping: splitting/SPLITTING.md's "Decision 8 by example" (the "hello" example), on synthetic
events.

Run from this directory: uv run --with pandas --with networkx --with tabulate --with matplotlib --with pyyaml
--with numpy python -m unittest test_message_paths
"""

import contextlib
import io
import itertools
import textwrap
import unittest
from unittest import mock

import message_paths as mp
import param_cells
import sample_paths
import networkx as nx
import unmatched_messages as um

HELLO, MSG = "example.com/hello", "example.com/msg"
TS = itertools.count(1)  # (events' times, in the order the tests list them)


def sent(cid, host, path, api_id):
    return {"kind": "Request sent", "pid": 1, "context": {"context_id": cid}, "api_id": api_id, "ts": next(TS),
            "request_id": {"method": "GET", "path": path},
            "message": {"req.Method": "GET", "req.URL.Host": host, "req.URL.Path": path}}


def received(cid, path, pattern, api_id):
    """A received request and the router's "Request routed" event for it"""
    msg = {"req.Method": "GET", "req.URL.Path": path}
    return [{"kind": "Request received", "pid": 1, "context": {"context_id": cid}, "api_id": api_id, "ts": next(TS),
             "message": msg},
            {"kind": "Request routed", "pid": 1, "context": {"context_id": cid}, "api_id": api_id, "ts": next(TS),
             "message": {**msg, "pattern": pattern}}]


def response_sent(cid, path):
    return {"kind": "Response sent", "pid": 1, "context": {"context_id": cid}, "ts": next(TS),
            "message": {"code": "200", "req.Method": "GET", "req.URL.Path": path}}


def response_received(cid, path, api_id):
    return {"kind": "Response received", "pid": 1, "context": {"context_id": cid}, "api_id": api_id, "ts": next(TS),
            "message": {"req.Method": "GET", "req.URL.Path": path, "resp.StatusCode": "200"}}


def hello(client_paths, responses=False):
    """splitting/SPLITTING.md's example: the client sends GET client_paths (two, influenced by its parameters pX and pY); the
    server, routing them under /hello/*, logged /hello/1 (while handling it, it sends M1, /msg/1) and /hello/2 (M2,
    /msg/2); backend receives M1 and M2 under /msg/{n}. responses: every request is answered (200), each in its
    request's context. The paths, as sample_paths.py prints them (path_lines, without its heading lines; dedented, a
    blank line between paths), and the graph"""
    x, y = client_paths
    events = {
        "client": [sent("c1", "server:80", x, HELLO), sent("c2", "server:80", y, HELLO)],
        "server": received("s1", "/hello/1", "/hello/*", HELLO) + [sent("s1", "backend:80", "/msg/1", MSG)]
                  + received("s2", "/hello/2", "/hello/*", HELLO) + [sent("s2", "backend:80", "/msg/2", MSG)],
        "backend": received("b1", "/msg/1", "/msg/{n}", MSG) + received("b2", "/msg/2", "/msg/{n}", MSG),
    }
    if responses:
        events["backend"] += [response_sent("b1", "/msg/1"), response_sent("b2", "/msg/2")]
        events["server"] += [response_received("s1", "/msg/1", MSG), response_sent("s1", "/hello/1"),
                             response_received("s2", "/msg/2", MSG), response_sent("s2", "/hello/2")]
        events["client"] += [response_received("c1", x, HELLO), response_received("c2", y, HELLO)]
    cells = {("client", "Request sent", "GET", x, None): {("client", "pX")},
             ("client", "Request sent", "GET", y, None): {("client", "pY")}}
    return paths_of(events, cells)


def paths_of(events, cells):
    """message_paths' graph of events ({module: its events}) and parameter cells, and its paths, as sample_paths.py
    prints them (path_lines, without its heading lines; dedented, a blank line between paths)"""
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
    """splitting/SPLITTING.md's decision 8 by example: GROUPING "edges" (refine), which it is about"""

    def setUp(self):
        for name in ("GROUPING", "RECEIVER_SPLITS", "ENTRY_RESPONSE"):
            self.addCleanup(setattr, mp, name, getattr(mp, name))
        mp.GROUPING = "edges"

    def test_separate_runs(self):
        # (decision 8, the default: as splitting/SPLITTING.md shows) /hello/* splits by the client's parameters, M1 and M2 by
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
        # The server's events match none of the client's paths: they fall back to every atom, and M1 and M2, with
        # the same edges in, merge; the edges' annotations still pair what the server received with what it sent
        mp.RECEIVER_SPLITS = True
        paths, g = hello(SEPARATE)
        self.assertEqual((g.number_of_nodes(), g.number_of_edges()), (3, 2))
        self.assertEqual(paths, """\
client => server req GET: /hello/X => /hello/* [paths: /hello/X]
  [server received the previous message as /hello/1 and sent the next message as /msg/1; server received the previous message as /hello/2 and sent the next message as /msg/2]
server => backend req GET: {/msg/1, /msg/2} => /msg/{n}

client => server req GET: /hello/Y => /hello/* [paths: /hello/Y]
  [server received the previous message as /hello/1 and sent the next message as /msg/1; server received the previous message as /hello/2 and sent the next message as /msg/2]
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

    def test_entry_response(self):
        # With responses: the server's response events attach to both of the client's response atoms (decision 8),
        # and so do their context IDs. A path entering the server by /hello/X leaves it only through the response
        # to /hello/X (ENTRY_RESPONSE), not /hello/Y's
        mp.RECEIVER_SPLITS = False
        paths, _ = hello(SEPARATE, responses=True)
        for p in paths.split("\n\n"):
            first, last = p.splitlines()[0], p.splitlines()[-1]
            self.assertTrue(first.startswith("client => server req"), p)
            self.assertTrue(last.startswith("server => client resp"), p)
            self.assertEqual(first.endswith("[paths: /hello/X]"), last.endswith("[paths: /hello/X]"), p)
        # (X and Y, each through M1, through M2, and straight to its response)
        self.assertEqual(paths.count("server => client resp"), 6)
        mp.ENTRY_RESPONSE = False
        paths, _ = hello(SEPARATE, responses=True)
        self.assertEqual(paths.count("server => client resp"), 10)  # (also X to Y's response, and Y to X's)

    def test_atom_context(self):
        # With GROUPING "labels", /hello/X and /hello/Y are one node, and so are their responses. Per atom
        # (ATOM_CONTEXT), a path followed from /hello/X's concrete message ends only at the response to /hello/X
        mp.GROUPING, mp.ATOM_CONTEXT = "labels", True
        self.addCleanup(setattr, mp, "ATOM_CONTEXT", True)
        _, g = hello(SEPARATE, responses=True)
        x = next(a for a in g.graph["atoms_of"][next(n for n in g if n.snd == "client")] if a[1][1] == "/hello/X")
        ends = set()
        for p in mp.maximal_paths(g):
            if p[0].snd != "client" or p[-1].rcv != "client":
                continue
            state = frozenset(st for st in mp.fresh(g, p[0]) if st[0] == x)
            for u, v in zip(p, p[1:]):
                state = mp.transition(g, u, v, state)
                self.assertTrue(state, p)
            ends |= {st[0][1][1] for st in state}
        self.assertEqual(ends, {"/hello/X"})


class PathAnnotations(unittest.TestCase):
    """A path's annotations name only what some realization of the whole path uses (path_pairs)"""

    def test_narrowed_by_context(self):
        # The server handles /hello/1 and /hello/2 alike (a call to /msg/a), but only /hello/1's handling goes on to
        # call /other/b. The path that ends at /other/b names only /hello/1, though the edge also joins /hello/2's
        events = {
            "client": [sent("c1", "server:80", "/hello/X", HELLO)],
            "server": received("s1", "/hello/1", "/hello/*", HELLO) + [sent("s1", "backend:80", "/msg/a", MSG)]
                      + [response_received("s1", "/msg/a", MSG), sent("s1", "backend:80", "/other/b", MSG)]
                      + received("s2", "/hello/2", "/hello/*", HELLO) + [sent("s2", "backend:80", "/msg/a", MSG)]
                      + [response_received("s2", "/msg/a", MSG)],
            "backend": received("b1", "/msg/a", "/msg/{n}", MSG) + [response_sent("b1", "/msg/a")]
                       + received("b2", "/msg/a", "/msg/{n}", MSG) + [response_sent("b2", "/msg/a")]
                       + received("b3", "/other/b", "/other/{n}", MSG),
        }
        saved = mp.GROUPING
        self.addCleanup(setattr, mp, "GROUPING", saved)
        mp.GROUPING = "labels"
        paths, g = paths_of(events, {})
        longest = max(paths.split("\n\n"), key=len)
        self.assertIn("/other/b", longest.splitlines()[-1])
        self.assertIn("[server received the previous message as /hello/1]", longest)
        self.assertNotIn("/hello/2", longest)
        # (the edge itself joins both)
        u = next(n for n in g if n.snd == "client")
        v = next(n for n in g.successors(u) if "/msg/a" in mp.label(n))  # (not /other/b's node)
        self.assertIn("/hello/2", mp.via_text(g, u, v))


class EdgeOrder(unittest.TestCase):
    """A within-module edge, received X -> sent Y, needs X's event before Y's in a context they share (shared_ids)"""

    def edges(self, server):
        events = {"client": [sent("c1", "server:80", "/hello/X", HELLO)], "server": server,
                  "backend": received("b1", "/msg/1", "/msg/{n}", MSG)}
        _, g = paths_of(events, {})
        return {(u.snd, v.snd) for u, v in g.edges}

    def test_receive_then_send(self):
        self.assertIn(("client", "server"), self.edges(
            received("s1", "/hello/1", "/hello/*", HELLO) + [sent("s1", "backend:80", "/msg/1", MSG)]))

    def test_send_then_receive(self):
        # (the send came first: the receive can't have led to it)
        self.assertNotIn(("client", "server"), self.edges(
            [sent("s1", "backend:80", "/msg/1", MSG)] + received("s1", "/hello/1", "/hello/*", HELLO)))


class Annotations(unittest.TestCase):
    """via_text: per edge, the concrete paths its module logged the edge's two messages under, paired, for the sides
    with several paths; one side's alone as a list, or as a count (per pattern if the node has several)"""

    def annotate(self, patterns, pairs):
        """The text on an edge from a client's request (the server's received labels: patterns) to the server's send to
        a backend, the server having logged pairs: [(the path it received, the path it sent)]"""
        labs = frozenset((("api_id", HELLO), ("pattern", pat), ("method", "GET")) for pat in patterns)
        u = mp.Node("request", "client", "server", frozenset(), frozenset(), frozenset(), labs, frozenset())
        v = mp.Node("request", "server", "backend", frozenset(), frozenset(), frozenset(), frozenset(), frozenset())
        received, sent = {r for r, _ in pairs}, {t for _, t in pairs}
        pats = {r: next(p for p in patterns if r.startswith(p.rstrip("*"))) for r in received}
        g = nx.DiGraph(via_patterns={("server", "rcv_req", r): {pats[r]} for r in received},
                       side_paths={(u, "rcv"): received, (v, "snd"): sent})
        g.add_edge(u, v, via={("server", ("GET", r), ("GET", t)) for r, t in pairs})
        return mp.via_text(g, u, v)

    def test_single_paths_not_annotated(self):
        # (each side logged one path: nothing the labels don't say)
        self.assertEqual(self.annotate(["/hello/*"], [("/hello/1", "/msg/1")]), "")

    def test_pairs(self):
        self.assertEqual(self.annotate(["/hello/*"], [("/hello/1", "/msg/1"), ("/hello/2", "/msg/2")]),
                         "server received the previous message as /hello/1 and sent the next message as /msg/1; "
                         "server received the previous message as /hello/2 and sent the next message as /msg/2")

    def test_one_side(self):
        # (the server sent one path: only the received side is named)
        self.assertEqual(self.annotate(["/hello/*"], [("/hello/1", "/msg/1"), ("/hello/2", "/msg/1")]),
                         "server received the previous message as /hello/1, /hello/2")

    def test_counted(self):
        # (the pattern is in the node's label already)
        self.assertEqual(self.annotate(["/hello/*"], [(f"/hello/{i}", "/msg/1") for i in range(6)]),
                         "server received the previous message as 6 paths")

    def test_counted_per_pattern(self):
        self.assertEqual(self.annotate(["/hello/*", "/bye/*"], [(f"/hello/{i}", "/msg/1") for i in range(4)]
                                       + [("/bye/1", "/msg/1"), ("/bye/2", "/msg/1")]),
                         "server received the previous message as 2 paths under /bye/*, 4 paths under /hello/*")

    def test_counted_pairs(self):
        self.assertEqual(self.annotate(["/hello/*"], [(f"/hello/{i}", f"/msg/{i}") for i in range(6)]),
                         "server received the previous message as 6 paths and sent the next message as 6 paths "
                         "(6 pairs)")


if __name__ == "__main__":
    unittest.main()
