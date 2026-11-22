"""A run's message events cut at the run's cutoff: the earliest of its modules' last event times. paramtrack's step 6
joins the cut events (cut_file), and the stats/messages scripts read them (read).

Why: the testbed copies each module's outputs one module at a time while all of them keep running (testbeds.py's
capture()), and the copies take minutes (the argmatch call logs are large), so each module's log ends at a different
time. Without the cut, a message sent after the receiver's log was copied looks unmatched (e.g. in
2026-11-18-check-all5's step 5, Grafana's last 17 query_range requests to Prometheus, sent up to 65s after Prometheus's
log ends).

The cutoff is over the modules of the run's --out directory: <out>/<module>/<step>/events.jsonl (layout.py's module
steps), if out has a testbed/ directory (a multi-module run's); a run from before 2026-11-15 (one module) isn't cut.
"""

import functools
import glob
import json
import os
import re
import sys

TS = re.compile(rb'"ts":\s*(\d+)')  # (an event's time, without parsing the whole line: the files are up to GBs)


def out_dir(path):
    """The run's --out directory, for a module's events file <out>/<module>/<step>/events.jsonl"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(path))))


def ts(line):
    """An events file line's time (ts), or None"""
    m = TS.search(line)
    return int(m.group(1)) if m else None


def last_time(path):
    """The latest event time (ts) in an events file, or None if it has none"""
    with open(path, "rb") as f:
        return max((t for t in map(ts, f) if t is not None), default=None)


@functools.lru_cache
def cutoff(out, step):
    """The earliest of the last event times of the modules' <step>/events.jsonl in out (None if there are none)"""
    lasts = [t for p in sorted(glob.glob(f"{out}/*/{step}/events.jsonl")) if (t := last_time(p)) is not None]
    return min(lasts) if lasts else None


def file_cutoff(path):
    """The cutoff for a module's events file <out>/<module>/<step>/events.jsonl: its run's, or None (not cut: not a
    multi-module run)"""
    out = out_dir(path)
    return cutoff(out, os.path.basename(os.path.dirname(path))) if os.path.isdir(f"{out}/testbed") else None


def kept(line, t):
    """Whether an events file line is kept at cutoff t (an event with no time is)"""
    lt = ts(line)
    return t is None or lt is None or lt <= t


_said = set()  # (the files read says it cut, so it says so once per file)


def read(path):
    """The events of a module's events file <out>/<module>/<step>/events.jsonl, but those after the run's cutoff;
    says on stderr how many it left out (once per file)"""
    t = file_cutoff(path)
    with open(path, "rb") as f:
        lines = [line for line in f if line.strip()]
    events = [json.loads(line) for line in lines if kept(line, t)]
    if len(events) < len(lines) and path not in _said:
        _said.add(path)
        print(f"events.py: {path}: left out {len(lines) - len(events)} of {len(lines)} events after the run's cutoff "
              f"(ts {t}, the earliest module's last event)", file=sys.stderr)
    return events


def cut_file(path, dest):
    """Writes the events of a module's events file to dest, but those after the run's cutoff; returns (cutoff or None,
    events kept, events left out)"""
    t = file_cutoff(path)
    n = left = 0
    with open(path, "rb") as f, open(dest, "wb") as out:
        for line in f:
            if not line.strip():
                continue
            if kept(line, t):
                out.write(line)
                n += 1
            else:
                left += 1
    return t, n, left
