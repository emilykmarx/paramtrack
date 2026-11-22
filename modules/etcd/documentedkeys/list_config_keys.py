#!/usr/bin/env python3
"""List the config-file keys etcd's documentation gives, for etcd v3.5.15's --config-file.

The docs (README.md, "Sources") say a config file is a YAML map whose keys are the command-line flag names, and point
to two files in the etcd repo for the details, which this script reads at the analyzed tag:
  - server/etcdmain/help.go: the `etcd --help` text (flagsline), the flag list the website calls authoritative;
  - etcd.conf.yml.sample: the sample config, which nests the TLS flags under client-transport-security and
    peer-transport-security.
Each flag becomes a key, except that a TLS flag whose name (without `peer-`, and with `cert-allowed-X` shortened to
`allowed-X`) is a key of one of the sample's transport-security sections goes into that section (README.md says
why). The type comes from the flag's documented default (TYPE_OVERRIDES where the default is missing or a Go
expression), and from the sample's YAML value for lists.

The cached website page (raw/website-v3.5-configuration.md) is only compared with help.go: flags only one of them
lists are printed to stderr.

Output: "<key>\t<type>" per line, sorted. Usage: uv run --with pyyaml python list_config_keys.py [--etcd DIR] [--out FILE]
"""

import argparse
import os
import re
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ETCD = "/home/emily/projects/config_tracing/etcd-paramtrack"
WEBSITE = os.path.join(HERE, "raw", "website-v3.5-configuration.md")

# Flags that aren't config settings (help.go's usageline, not flagsline, has these; listed to be safe)
NOT_KEYS = {"config-file", "version", "help"}

# Types of the flags whose documented default is missing or a Go expression in help.go (what the expression is, or
# the description, decides; the website's rendered default agrees where it has one)
TYPE_OVERRIDES = {
    "max-snapshots": "<int>",  # strconv.Itoa(embed.DefaultMaxSnapshots); website: '5'
    "max-wals": "<int>",  # strconv.Itoa(embed.DefaultMaxWALs); website: '5'
    "max-concurrent-streams": "<int>",  # 'math.MaxUint32'
    "enable-grpc-gateway": "<bool>",  # no default; "Enable GRPC gateway."
    "raft-read-timeout": "<duration>",  # rafthttp.DefaultConnReadTimeout.String()
    "raft-write-timeout": "<duration>",  # rafthttp.DefaultConnWriteTimeout.String()
    "strict-reconfig-check": "<bool>",  # strconv.FormatBool(...)
    "enable-v2": "<bool>",  # strconv.FormatBool(...)
    "v2-deprecation": "<string>",  # string(cconfig.V2_DEPR_DEFAULT); website: 'not-yet'
    "bcrypt-cost": "<int>",  # fmt.Sprintf("%d", bcrypt.DefaultCost); website: 10
    # default '0', but "periodic: duration based retention ... (e.g. '5m'). 'revision' for revision number", and the
    # sample quotes it ("1")
    "auto-compaction-retention": "<string>",
    "backend-batch-interval": "<duration>",  # default ''; the website's config-file note lists it as a duration
    "experimental-downgrade-check-time": "<duration>",  # no default; "Duration of time between ..."
    "experimental-memory-mlock": "<bool>",  # no default; "Enable to enforce ..."
    "experimental-bootstrap-defrag-threshold-megabytes": "<int>",  # no default; "... threshold of disk space"
    "experimental-stop-grpc-service-on-defrag": "<bool>",  # no default; "Enable etcd gRPC service to stop ..."
}
TLS_SECTION = "Security"
LIST_RE = re.compile(r"\b(list of|comma-separated|comma separated)\b", re.I)
DURATION_RE = re.compile(r"^\d+(\.\d+)?(ns|us|ms|s|m|h)$")


def parse_help(path):
    """help.go's flagsline -> [(section, flag, default or None, description)]"""
    src = open(path).read()
    start = src.index("flagsline = `")
    lines = src[start:].splitlines()[1:]
    flags, section = [], None
    for line in lines:
        if line.startswith("CAUTIOUS") or line.startswith("`"):
            break
        if re.match(r"^\S.*:$", line):
            section = line[:-1]
            continue
        m = re.match(r"^  --([a-z0-9-]+)(?:\s+(.*))?$", line)
        if m:
            flags.append([section, m.group(1), m.group(2), ""])
        elif line.strip() and flags:
            flags[-1][3] += " " + line.strip()
    return [(s, f, d, desc.strip()) for s, f, d, desc in flags]


def parse_website(path):
    """Flag names in the website page's ```nocode blocks"""
    return set(re.findall(r"^--([a-z0-9-]+)", open(path).read(), re.M))


def flag_type(flag, default, desc, sample_value):
    if isinstance(sample_value, list):
        return "[]string"  # the sample writes it as a YAML list (log-outputs: [stderr])
    if flag in TYPE_OVERRIDES:
        return TYPE_OVERRIDES[flag]
    if default is None or "` +" in default:
        sys.exit(f"flag --{flag}: default {default!r} needs a TYPE_OVERRIDES entry")
    d = default.strip("'")
    if LIST_RE.search(desc):
        return "<comma-separated list>"
    if d in ("true", "false"):
        return "<bool>"
    if re.fullmatch(r"-?\d+", d):
        return "<int>"
    if DURATION_RE.match(d):
        return "<duration>"
    return "<string>"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--etcd", default=DEFAULT_ETCD, help="etcd checkout (v3.5.15)")
    ap.add_argument("--out", default=os.path.join(HERE, "etcd_config_keys.txt"))
    args = ap.parse_args()

    flags = parse_help(os.path.join(args.etcd, "server/etcdmain/help.go"))
    sample = yaml.safe_load(open(os.path.join(args.etcd, "etcd.conf.yml.sample")))
    sections = {k: v for k, v in sample.items() if isinstance(v, dict)}  # {client,peer}-transport-security
    sec_keys = set().union(*sections.values())  # both sections' keys: the Go type is shared (README.md)

    keys = {}
    for section, flag, default, desc in flags:
        if flag in NOT_KEYS:
            continue
        key, sample_value = flag, sample.get(flag)
        if section == TLS_SECTION:
            side, rest = ("peer", flag[len("peer-"):]) if flag.startswith("peer-") else ("client", flag)
            rest = re.sub(r"^(client-)?cert-allowed-", "allowed-", rest)
            if rest in sec_keys:
                key = f"{side}-transport-security.{rest}"
                sample_value = sections.get(key.split(".")[0], {}).get(rest)
        if key in keys:
            sys.exit(f"duplicate key {key}")
        keys[key] = flag_type(flag, default, desc, sample_value)

    # Every key of the sample must be a documented flag (or a transport-security key mapped from one)
    sample_keys = {k for k, v in sample.items() if not isinstance(v, dict)}
    sample_keys |= {f"{s}.{k}" for s, sub in sections.items() for k in sub}
    for k in sorted(sample_keys - set(keys)):
        print(f"sample key not in help.go: {k}", file=sys.stderr)

    if os.path.exists(WEBSITE):
        help_flags, web = {f for _, f, _, _ in flags}, parse_website(WEBSITE)
        print("flags only in help.go:", " ".join(sorted(help_flags - web)) or "-", file=sys.stderr)
        print("flags only on the website:", " ".join(sorted(web - help_flags)) or "-", file=sys.stderr)

    with open(args.out, "w") as f:
        for k in sorted(keys):
            f.write(f"{k}\t{keys[k]}\n")
    types = {}
    for t in keys.values():
        types[t] = types.get(t, 0) + 1
    print(f"{len(keys)} keys -> {args.out}; types: {types}", file=sys.stderr)


if __name__ == "__main__":
    main()
