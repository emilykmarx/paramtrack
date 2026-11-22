# Documented config keys: etcd

Documented keys of etcd's config file (`go.etcd.io/etcd/server/v3`, v3.5.15), derived from the docs, for comparison
with the keys the static analysis finds. Format and conventions follow
[`../../prometheus/documentedkeys/README.md`](../../prometheus/documentedkeys/README.md); this file says only where
etcd differs.

## What "the config" is
- **Inputs:** command-line flags, `ETCD_*` environment variables (one per flag), or a YAML config file
  (`--config-file` / `ETCD_CONFIG_FILE`). With a config file, the other flags and env vars are ignored
  (`server/etcdmain/config.go:346-357`; flags that write package globals directly, `raft-read-timeout` and
  `raft-write-timeout`, still take effect).
- **The file is decoded twice, into two roots**, both with **sigs.k8s.io/yaml** v1.2.0 (`yaml.Unmarshal`: YAML ->
  JSON -> `encoding/json`, so `json` tags, field names matched case-insensitively, unknown keys silently ignored):
  - `embed.ConfigFromFile` -> `(*configYAML).configFromFile` (`server/embed/config.go:534-645`) into
    **`go.etcd.io/etcd/server/v3/embed.configYAML`**, which embeds `embed.Config` (most keys) and `configJSON`
    (string forms of the URL lists, `cors`, `host-whitelist`, and the `client-transport-security` /
    `peer-transport-security` sections, type `securityConfig`). `configFromFile` then parses the strings into
    `Config`'s `[]url.URL`/map fields and copies the sections into `Config.ClientTLSInfo`/`PeerTLSInfo`
    (`go.etcd.io/etcd/client/pkg/v3/transport.TLSInfo`). `socket-options` is `transport.SocketOpts` (client/pkg).
  - `(*config).configFromFile` (`server/etcdmain/config.go:442-470`) decodes the same bytes again into
    **`go.etcd.io/etcd/server/v3/etcdmain.configProxy`**: `proxy`, `proxy-*` and `discovery-fallback`.
- **Keys = flag names**, per the docs (website, "Configuration file"), except the TLS sections (below).
- **The testbed's flags** (kubeadm static pod) can all be expressed in the file: `cert-file`, `key-file`,
  `client-cert-auth`, `trusted-ca-file` go under `client-transport-security.`, the four `peer-*` TLS flags under
  `peer-transport-security.` (without `peer-`), the rest are top-level keys with the flag's name. Durations
  (`experimental-watch-progress-notify-interval=5s`) must be written as integer nanoseconds (`5000000000`), as the
  website notes. kubeadm can add `config-file` through `etcd.local.extraArgs` (+ `extraVolumes`); its own flags are
  then ignored, so the file must repeat them.

## Files
| File | What |
|---|---|
| `list_config_keys.py` | Writes `etcd_config_keys.txt` from help.go + the sample config; prints doc diffs to stderr |
| `etcd_config_keys.txt` | The documented keys (108), one list for the one config file (both roots) |
| `raw/website-v3.5-configuration.md` | Cached website page, only compared with help.go |
| `local_build_versions.json` | Checkout versions (not written by the script) |

## Sources
- `/home/emily/projects/config_tracing/etcd-paramtrack`, tag v3.5.15, commit `9a5533382d84999e4e79642e1ec0f8bfa9b70ba8`:
  - `server/etcdmain/help.go` (`flagsline`): the `etcd --help` text, which the website names as the authoritative,
    up-to-date flag list. Used for the keys, defaults and descriptions.
  - `etcd.conf.yml.sample`: the sample the website links; used for the TLS sections and for YAML list values.
  - (The repo's `Documentation/` only points to the website.)
- https://etcd.io/docs/v3.5/op-guide/configuration/, source
  `https://raw.githubusercontent.com/etcd-io/website/main/content/en/docs/v3.5/op-guide/configuration.md`, last
  commit `ec9870cb` (2026-06-10), fetched 2026-11-21 into `raw/`. It's for the latest 3.5 (mentions v3.5.32+) and
  lists a subset of help.go's flags (14 missing, none extra; the script prints them), so it adds no keys. It gives
  the "keys are flag names" rule and the duration-as-nanoseconds note.
- No machine-readable reference exists; help.go's text is parsed with a regex, the sample with PyYAML.

## Rerun
```sh
cd /home/emily/projects/config_tracing/paramtrack/modules/etcd/documentedkeys
timeout 120 uv run --with pyyaml python list_config_keys.py   # --etcd DIR, --out FILE
```
Exits non-zero on a duplicate key or a flag whose type can't be read from its default and has no override.

## Conventions and judgment calls
- **One flag = one top-level key**, except `config-file`/`version`/`help` (not settings). No placeholders, no maps,
  no `k=v` segments (nothing selects a type by value), no recursion.
- **TLS sections:** the sample nests TLS flags under `client-transport-security` / `peer-transport-security`, but
  lists only some of them. Rule: a flag in help.go's "Security" group goes into a section if its name, with a
  `peer-` prefix removed (-> peer section, else client section) and `(client-)cert-allowed-X` shortened to
  `allowed-X`, is a key of *either* section in the sample (they share one Go type, `securityConfig`). That puts 13
  flags in sections, including `client-transport-security.allowed-hostname` (from `client-cert-allowed-hostname`;
  the sample lists `allowed-hostname` only for peers). Flags that don't match stay top-level as the docs' general
  rule says, though the code doesn't accept them there (below).
- **Types** are inferred from the docs, since they give none: `'true'/'false'` -> `<bool>`, an integer -> `<int>`, a
  number with a unit -> `<duration>`, a description saying "list of"/"comma-separated" -> `<comma-separated list>`,
  a YAML list in the sample -> `[]string`, else `<string>`. `TYPE_OVERRIDES` in the script gives 16 types where the
  default is missing, a Go expression, or misleading (`auto-compaction-retention`: default `'0'`, but it's a
  duration-or-revision string, quoted in the sample), each with its reason.

## Types
| Type | Count | Meaning | Kind |
|---|---|---|---|
| `<string>` | 40 | string (paths, names, enums like `proxy`, `auth-token`'s `type,k=v,...`, a JSON string) | string |
| `<bool>` | 24 | boolean | bool |
| `<int>` | 21 | integer (milliseconds for `heartbeat-interval`, `election-timeout`, `proxy-*`; seconds for `auth-token-ttl`; years for `self-signed-cert-validity`) | number |
| `<duration>` | 11 | Go `time.Duration`: `'10m'` as a flag, integer nanoseconds in the file | number |
| `<comma-separated list>` | 11 | comma-separated URLs/names/cipher suites, a single string as a flag | string |
| `[]string` | 1 | `log-outputs`, a YAML list in the sample | string |

## Known gaps: docs vs code (from the struct tags, not used to build the list)
- **Documented but not a file key at that place** (the comparison will show them as documented-only):
  - `socket-reuse-port`, `socket-reuse-address`: the file key is `socket-options.reuse-port` / `.reuse-address`.
  - `client-cert-file`, `client-key-file`, `peer-client-cert-file`, `peer-client-key-file`: the file keys are
    `{client,peer}-transport-security.client-{cert,key}-file` (not in the sample).
  - No file key at all (flag only): `client-crl-file`, `peer-crl-file`,
    `experimental-peer-skip-client-san-verification`, `raft-read-timeout`, `raft-write-timeout`.
- **In code, not documented:** `experimental-snapshot-catch-up-entries` (its flag, `experimental-snapshot-catchup-entries`,
  spelled differently, isn't in help.go either); `socket-options.*`; the transport sections' `client-cert-file` /
  `client-key-file`.
- **Untagged exported fields of `embed.Config`** are decodable too, by Go field name: `ListenPeerUrls`,
  `ListenClientUrls`, `ListenClientHttpUrls`, `AdvertisePeerUrls`, `AdvertiseClientUrls`, `ListenMetricsUrls`
  (`[]url.URL`), `ClientTLSInfo`, `PeerTLSInfo` (`transport.TLSInfo`, e.g. `ClientTLSInfo.CRLFile`, `ServerName`),
  `ClientAutoTLS`, `PeerAutoTLS`, `CORS`, `HostWhitelist`. Several are overwritten by `configFromFile`, but not all
  (e.g. TLSInfo's `CRLFile`, `InsecureSkipVerify`), so a static analysis may report them as keys.
- **List types:** the docs call `cipher-suites`, `allowed-cn`, `allowed-hostname` comma-separated, but in the file
  they're `[]string` (YAML lists); the URL lists, `cors` and `host-whitelist` really are comma-separated strings.
- `peer-transport-security.allowed-cn` fills `TLSInfo.AllowedCNs`; `TLSInfo.AllowedCN` (singular) is deprecated.
