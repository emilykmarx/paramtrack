# etcd

The concreteconfig skill's module file for etcd (`go.etcd.io/etcd/server/v3`). What its config is, how it's loaded and
how the documented keys were listed are in [documentedkeys/README.md](documentedkeys/README.md); module.py has the
paramtrack settings.

- **Checkout:** `/home/emily/projects/config_tracing/etcd-paramtrack` (v3.5.15, the version the testbed's kind node
  runs; same path locally and on the prombench VM). The server is the Go module in `server/`; etcd's other modules
  (`api/`, `client/pkg/`, ...) are next to it, and its other dependencies are in the Go module cache (`go list -m -f
  '{{.Dir}}' <module>` in `server/`).
- **Module, config source and format:** `--module etcd`, one config source, `config`: the YAML file etcd reads with
  `--config-file`, decoded by `sigs.k8s.io/yaml` (YAML converted to JSON, then the `json` tags; unknown keys are
  ignored, not rejected). Write it as JSON (TESTBED.md, "JSON"). The file replaces every flag and environment variable, so a config sets everything the
  testbed needs (TESTBED.md, "What the rest of the testbed relies on").
- **Ignore file:** [ignore-config.txt](ignore-config.txt), with a comment saying why: the v2 proxy mode's keys, the
  peer keys and the discovery keys (the testbed's etcd is the cluster's only member, bootstrapped by kubeadm).
- **No transformed-keys file yet:** some string values are stored parsed, not as written (e.g. the URL lists, read
  as one comma-separated string into `[]url.URL`; `cipher-suites`, into TLS IDs). If you find such keys, say which
  in `result.md` (the Alertmanager run wrote its module's `transformed-config.txt` that way).

## Developer knowledge
In the checkout:
- `etcd.conf.yml.sample`: the config file's keys, the TLS sections' nesting, and a value for each.
- `server/etcdmain/help.go`: every flag (a file key is its flag's name, but for the TLS ones), its default and meaning;
  [documentedkeys/raw/website-v3.5-configuration.md](documentedkeys/raw/website-v3.5-configuration.md) is the
  website's version.
- `server/embed/config.go`: `Config` (the keys' types and defaults, `NewConfig`), `configYAML`/`configJSON` and
  `configFromFile` (how the file is read: the URL lists and TLS sections are copied into `Config` after decoding), and
  `Validate`; `server/embed/config_test.go` for configs that load or fail.
- What the docs leave out (documentedkeys/README.md, "Known gaps"): file keys under other names than their flags
  (`socket-options.*`, `*-transport-security.client-cert-file`/`client-key-file`), flags with no file key, and
  `experimental-snapshot-catch-up-entries` (a file key with no flag).

## Checking a config
TESTBED.md, "Applying a config": `cc_vm.py check` (load and validate, as etcd does at start), then a restart. Durations
are integer nanoseconds in the file (`5000000000` for 5s), not strings; `cipher-suites`,
`client-transport-security.allowed-cn` and `allowed-hostname` are JSON lists (the docs call them comma-separated),
while the URL lists, `cors` and `host-whitelist` are single comma-separated strings. A wrong type stops etcd at start
("cannot unmarshal string into ... of type []string"), so check before every restart.

## Keys to skip, or not to count
- **Numbers, bools and durations:** not searched (`--strings-only`), so they don't count. Leave them as shipped, or
  unset, unless a string key needs them.
- **Anything that changes what the API server relies on** (TESTBED.md): the data dir and member name, the
  `127.0.0.1:2379` listener and its TLS, the metrics listener.
- **`client-transport-security.auto-tls`:** a self-signed certificate the API server doesn't trust.
- **`host-whitelist` and `cors`:** checked only on plain-HTTP requests (`accessController` in
  `server/embed/serve.go`); the API server's requests are TLS, so they're inert there, but keep the metrics listener's
  probes in mind if the metrics listener serves through it.

## Exercising keys
The workflow learns about a key from the messages its value is found on, and etcd's messages are mostly the API
server's gRPC calls and their responses (TESTBED.md, "Message labels"). Keys that make etcd send messages of its own:
- **Distributed tracing** (`experimental-enable-distributed-tracing`, `experimental-distributed-tracing-address`,
  `-service-name`, `-instance-id`, `-sampling-rate`): etcd exports spans over OTLP gRPC. kube-apiserver's testbed
  fixtures include an OpenTelemetry collector at `127.0.0.1:4317` on the node (etcd's pod is on the host network), when
  the run instruments the API server too, as the label checks do (`testbeds/prombench/kube-apiserver/fixtures.py`).
- **More client listeners** (`listen-client-urls`, `listen-client-http-urls`) and the gRPC gateway
  (`enable-grpc-gateway`): only messages if a client uses them; the API server uses `127.0.0.1:2379` only.

## Values known to be safe
- Extra listeners on other local ports, e.g. `https://127.0.0.1:<port>` in `listen-client-urls` (alongside
  `127.0.0.1:2379`), and extra `listen-metrics-urls`.
- `log-outputs` to a file under `/argmatch-out/etcd/` (writable, mounted) besides `stderr`; `log-level` one of its
  levels.
- File paths for TLS that are symlinks to the kubeadm files (TESTBED.md, "Adding files").
