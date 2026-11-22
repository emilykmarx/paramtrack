"""Which config sections influence each concrete message a module logged: the evidence matrices' cells
(evidence_matrix.py: a section has evidence for a message when one of its evidence keys is eligible), but per concrete
message, (module, kind, method, path, status code), instead of per label. Step 6's join is redone per event (matches.joined_calls)
to get them: a label's cells are the union of its concrete messages'.
"""

import collections
import glob
import hashlib
import os
import pickle
import sys

import evidence_matrix  # (next to this file: eligible keys)
import matches  # (next to this file: the join and the keys' ambiguity)

KINDS = ("Request sent", "Response sent", "Request received", "Response received")


def concrete_key(event, label):
    """An event's concrete message: (kind, method, path, status code (None for a request))"""
    m = event.get("message") or {}
    return event["kind"], m.get("req.Method"), m.get("req.URL.Path"), m.get("code") or m.get("resp.StatusCode")


CACHE = os.path.expanduser("~/.cache/paramtrack/param_cells")
VERSION = 2  # (part of the cache key: change it when what compute_table computes changes)


def table(out):
    """compute_table(out), cached in CACHE: keyed by out's path and its inputs' modification times (each module's
    5-matches, each root's 4-configkeys, 6-label-values, 7-label-keys and 8-stats), since redoing the join takes
    minutes"""
    inputs = sorted(glob.glob(f"{out}/*/5-matches/*") + glob.glob(f"{out}/*/6-label-values/*") +
                    glob.glob(f"{out}/*/*/[478]-*/*"))
    key = hashlib.sha256(repr([VERSION, os.path.abspath(out)] + [(p, os.path.getmtime(p)) for p in inputs]).encode()).hexdigest()
    path = f"{CACHE}/{key[:16]}.pickle"
    if os.path.exists(path):
        print(f"param_cells: from the cache ({path})", file=sys.stderr)
        return pickle.load(open(path, "rb"))
    cells = compute_table(out)
    os.makedirs(CACHE, exist_ok=True)
    pickle.dump(dict(cells), open(path, "wb"))
    return cells


def compute_table(out):
    """(module, kind, method, path, code) => {(root, section)}: the sections with evidence for that concrete message, over
    every root of every module in a paramtrack --out directory (a root: <out>/<module>/<root> with 8-stats)"""
    roots = collections.defaultdict(list)
    for stats_dir in sorted(glob.glob(f"{out}/*/*/8-stats")):
        root_dir = os.path.dirname(stats_dir)
        roots[root_dir.split("/")[-2]].append(root_dir)
    cells = collections.defaultdict(set)
    for module, root_dirs in roots.items():
        # Per root: its needles' unambiguous full keys, their sections, and the sections' eligible keys
        per_root, kept, config_values = [], set(), set()
        for root_dir in root_dirs:
            ukeys, _, _, section_of = matches.unambiguous_keys(root_dir)
            fks = {n: {fk for fk in f if fk in ukeys} for n, f in matches.needle_full_keys(root_dir).items()}
            eligible = evidence_matrix.load_evidence(root_dir)["eligible"]
            per_root.append((os.path.basename(root_dir), fks, section_of, eligible))
            kept |= {n for n, f in fks.items() if f}
            config_values |= matches.unambiguous_values(root_dir)[1]
        # The join, per concrete message (the module's step 5 outputs: any root's)
        calls = matches.joined_calls(root_dirs[0], key=concrete_key, kinds=KINDS)
        cm, _ = matches.call_matches(root_dirs[0], set().union(*calls.values()) if calls else set(), kept,
                                     config_values)
        for k, cs in calls.items():
            needles = {n for c in cs for n, _ in cm.get(c, ())}
            for root, fks, section_of, eligible in per_root:
                for n in needles:
                    for fk in fks.get(n, ()):
                        if fk in eligible.get(section_of[fk], ()):
                            cells[(module,) + k].add((root, section_of[fk]))
    return cells
