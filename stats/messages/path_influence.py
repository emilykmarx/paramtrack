"""The config sections that influence the messages on message_paths.py's paths (sample_paths.py --influence writes
them for its sample, with the sample's path IDs).

A section P (a row of a module's param_influence matrices, evidence_matrix.py, for one of its roots) influences a
node <sender> => <receiver> <label> of a path:
- direct send: P's root is the sender's, and P has evidence for one of the node's concrete messages as its sender
  logged them (the matrix cell of the node's sent kind, per concrete message: param_cells, message_paths' "params");
- direct receive: likewise for the receiver and the node's received kind;
  a direct section of a side whose concrete messages aren't the node's (a request's receiver, a response's sender:
  GROUPING.md decision 8) was found on that side's own paths, which follow it in parentheses
  (message_paths.param_via_text);
- indirect: P influences, directly (send or receive), a node from which message_paths' graph has a path to this one,
  as message_paths allows paths (an ancestor of it, on this path or not: message_paths.ancestors).
"""

import collections

import message_paths as mp  # (next to this file: the graph, its paths and the nodes' parameters)


def influence(g, path):
    """For each node of path: (its direct sections, {"send": {(module, root, section)}, "receive": ...}; indirect:
    {(module, root, section): the ancestors it reaches the node through})"""
    params = g.graph["params"]
    out = []
    for n in path:
        ind = collections.defaultdict(set)
        for a in mp.ancestors(g, n):
            for sec in params[a]["send"] | params[a]["receive"]:
                ind[sec].add(a)
        out.append((params[n], ind))
    return out


def lines_for(pid, path, g):
    """The influence file's lines for one path: per node, its direct send, direct receive and indirect sections
    (<module>/<root>: <section>), an indirect one with the nodes it comes through: those on this path by number,
    and how many others"""
    def sec(t):
        return f"{t[0]}/{t[1]}: {t[2]}"
    out = []
    pos = {m: j for j, m in enumerate(path, 1)}
    for i, (n, (dr, ind)) in enumerate(zip(path, influence(g, path)), 1):
        out.append(f"  {pid}.{i} {mp.label(n)}")
        for kind in ("send", "receive"):
            out.append(f"    direct {kind} ({len(dr[kind])}):")
            for t in sorted(dr[kind]):
                via = mp.param_via_text(g, n, kind, t)  # (decision 8: the other side's paths it was found on)
                out.append(f"      {sec(t)}" + (f" ({via})" if via else ""))
        out.append(f"    indirect ({len(ind)}):")
        for t in sorted(ind):
            on = sorted(pos[a] for a in ind[t] if a in pos)
            off = sum(1 for a in ind[t] if a not in pos)
            via = ", ".join(f"{pid}.{j}" for j in on) + (", " if on and off else "") + (f"{off} other nodes" if off else "")
            out.append(f"      {sec(t)} (via {via})")
    return out
