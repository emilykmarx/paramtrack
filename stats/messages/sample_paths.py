#!/usr/bin/env python3
"""A sample of message_paths.py's paths to spot-check: for each path length, up to --per-length paths that differ
from each other and from those sampled at other lengths.

"Different" (sample, greedily, longest lengths first):
- a path's route is its sequence of (sender, receiver) hops, e.g. caddy => grafana, grafana => prometheus, ...: paths with
  the same route differ only in which messages went over the same hops. A sampled path's route is one no path
  sampled so far (at any length) has, while the length has such paths;
- among those, the path with the most messages (nodes) no sampled path has, so a sample doesn't repeat the same
  requests (e.g. kube-apiserver's etcd calls) with only one message changed; ties broken by the path's text.
Each sampled path is printed with its length, route (its endpoints in order: route_text) and how many of its messages are new, a message per line.

Usage: uv run --with pandas --with networkx --with tabulate --with matplotlib --with pyyaml --with numpy
python sample_paths.py --run <paramtrack --out dir> [--api-ids f] [--external f] [--per-length N] [--min-length N]
--out <file>
"""

import argparse
import collections
import os

import message_paths as mp  # (next to this file: the graph and its paths)
import path_influence  # (next to this file: the sections that influence the paths' messages)


def route(p):
    return tuple((n.snd, n.rcv) for n in p)


def route_text(hops):
    """A route as its endpoints in order, X => Y => Z; a hop whose sender isn't the previous hop's receiver (which
    message_paths' edges don't make, but a type logged by several modules, a;b, could) starts a new run, after a
    " | " """
    out = f"{hops[0][0]} => {hops[0][1]}"
    for (_, prev), (s, r) in zip(hops, hops[1:]):
        out += f" => {r}" if s == prev else f" | {s} => {r}"
    return out


def sample(paths, per_length):
    """{length: [(path, new messages)]}, sampled from paths as the docstring says"""
    by_length = collections.defaultdict(list)
    for p in paths:
        by_length[len(p)].append(p)
    routes, seen, out = set(), set(), {}
    for length in sorted(by_length, reverse=True):
        cands = sorted(by_length[length], key=lambda p: [mp.label(n) for n in p])
        out[length] = []
        for _ in range(per_length):
            best = max(cands, key=lambda p: (route(p) not in routes, len(set(p) - seen)), default=None)
            if best is None or best in [b for b, _ in out[length]]:
                break
            out[length].append((best, len(set(best) - seen)))
            routes.add(route(best))
            seen |= set(best)
            cands = [p for p in cands if p is not best]
    return out


def path_lines(g, p):
    """A path's messages, a line each (mp.label), an edge's annotations (mp.via_text, only the pairs of concrete
    messages the path can be on: mp.path_pairs) on an indented [...] line before the message it leads to"""
    lines, pairs = [], mp.path_pairs(g, p)
    for j, n in enumerate(p):
        via = mp.via_text(g, p[j - 1], n, pairs[j - 1]) if j else ""
        lines += ([f"      [{via}]"] if via else []) + [f"    {mp.label(n)}"]
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mp.add_args(ap)
    ap.add_argument("--per-length", type=int, default=3, help="paths per length (default %(default)s)")
    ap.add_argument("--min-length", type=int, default=4, help="shortest length sampled (default %(default)s, as "
                                                               "message_paths.py's)")
    ap.add_argument("--out", required=True, help="the sample (text)")
    ap.add_argument("--influence", help="also write the config sections that influence each sampled path's messages "
                                        "here (path_influence.py), with the same path IDs")
    a = ap.parse_args()
    g = mp.build_from_args(a, params=bool(a.influence))
    picked = sample((p for p in mp.maximal_paths(g) if len(p) >= a.min_length), a.per_length)
    lines = [f"# Sampled paths (stats/messages/sample_paths.py --run {a.run}): up to {a.per_length} per length, "
             "each with a route no earlier sample has where possible, and the most messages no earlier sample has", ""]
    for length, ps in picked.items():
        lines.append(f"## Length {length}")
        for i, (p, new) in enumerate(ps, 1):
            lines += ["", f"{length}.{i} route: " + route_text(route(p)),
                      f"    ({new} of {length} messages not in an earlier sample)"]
            lines += path_lines(g, p)
        lines.append("")
    open(a.out, "w").write("\n".join(lines))
    print(f"wrote {a.out}: {sum(len(ps) for ps in picked.values())} paths")
    if a.influence:
        lines = [f"# Config sections influencing the sampled paths' messages (stats/messages/sample_paths.py --run "
                 f"{a.run} --influence; path IDs as in {os.path.basename(a.out)}; see path_influence.py for the rules)",
                 ""]
        for length, ps in picked.items():
            for i, (p, _) in enumerate(ps, 1):
                lines += [f"{length}.{i}"] + path_influence.lines_for(f"{length}.{i}", p, g) + [""]
        open(a.influence, "w").write("\n".join(lines))
        print(f"wrote {a.influence}")


if __name__ == "__main__":
    main()
