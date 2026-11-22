# prombench-caddyICingress (prombench, module: etcd)

The module is the cluster's own etcd: kubeadm's static pod `etcd-prombench-control-plane` (namespace `kube-system`) on
the control-plane node of prombench's kind cluster, on the GCE VM `paramtrack-e2e` (zone `us-central1-b`, project
`conftamer`): `gcloud compute ssh --zone us-central1-b paramtrack-e2e --project conftamer`. kubectl context
`kind-prombench`; the node is the docker container `prombench-control-plane` (`docker exec` into it for its files and
`crictl`). On the VM, the checkouts are under `/home/emily/projects/config_tracing`.

paramtrack's testbed, with `--module etcd` (`Etcd` in `testbeds.py`; its docstring says what it installs), builds etcd
under argmatch and contexttrack and runs the static pod with that image and the command `etcd
--config-file=/argmatch-out/etcd/config/config.json` (the concrete config). etcd then ignores every flag, so a config
must set everything the cluster needs (below). The other modules run stock unless the run instruments them too.

- **Shipped concrete config,** next to this file: [shipped-config.json](shipped-config.json), kubeadm's flags written
  as the config file (from `/etc/kubernetes/manifests/etcd.yaml` in the node, Kubernetes v1.31.1): file keys are the
  flags' names, but the TLS flags are nested in `client-transport-security` and `peer-transport-security`
  (documentedkeys/README.md), and durations are integer nanoseconds. Stock, etcd runs kubeadm's manifest with those
  flags.
- **JSON:** etcd reads the file as YAML, but paramtrack parses a concrete config by module.py's `FORMAT`, `json`
  (etcd's types have only `json` tags), so write the config as JSON (valid YAML), with no comments.
- **`NODE_IP`:** a config writes the node's IP as `NODE_IP` (as the shipped config does), not as an address: kind
  assigns the nodes' IPs in the order they start, so it differs between clusters (`172.18.0.2` in one, `172.18.0.3`
  in the next). The testbed and `cc_vm.py` replace it with the node's IP (kubeadm's `--advertise-client-urls`).
- **Information about the testbed:** kubeadm's manifest, `/etc/kubernetes/manifests/etcd.yaml` in the node (saved as
  `/etc/kubernetes/paramtrack-original/etcd.yaml` before the first change); the API server's,
  `kube-apiserver.yaml` next to it (its `--etcd-*` flags). etcd is the cluster's only member, and the API server is
  its only client (gRPC, over TLS with client certificates); the kubelet probes its metrics listener.

## What the rest of the testbed relies on
A config that changes these breaks the cluster (constraint 2: the API server's and every other module's labels):
- **`name: prombench-control-plane` and `data-dir: /var/lib/etcd`:** the cluster's data (and its member, by that name)
  is there. `wal-dir` must stay unset (the WAL is in the data dir).
- **A listener at `https://127.0.0.1:2379`** in `listen-client-urls`, with the client TLS the API server expects:
  `client-transport-security.cert-file`/`key-file` a certificate for `127.0.0.1` signed by the etcd CA,
  `trusted-ca-file` that CA (the API server's client certificate is signed by it), `client-cert-auth: true`. The
  kubeadm files are under `/etc/kubernetes/pki/etcd/` (mounted in the pod); symlinks to them at other paths work the
  same (see "Adding files").
- **The metrics listener at `http://127.0.0.1:2381`** (`listen-metrics-urls`): the kubelet's probes
  (`/livez`, `/readyz`) and this file's health check use it.
- **Peers and discovery are out of scope** (the module's ignore file): leave the peer keys as shipped.

## Applying a config
`cc_vm.py` (next to this file) does this section's and "Adding files"' procedures, and the health checks: copy this
whole directory to the VM and run `python3 cc_vm.py files <dir> | check <config> | restart <config> | health |
shipped` there (its docstring has the details). What it does:
- **Checking a candidate:** etcd has no check command. `cc_vm.py check` loads and validates the config as etcd does at
  start ([check_config/](check_config/): `embed.ConfigFromFile` and `Config.Validate` from the checkout), with
  `NODE_IP` replaced. It doesn't open listeners or files, so a restart is the full check.
- **No reload:** etcd reads its config only at start, so applying a config is a restart: `cc_vm.py restart` writes it
  (NODE_IP replaced) to `/argmatch-out/etcd/config/config.json`, sets the static pod's command to `etcd
  --config-file=` that (keeping the image it runs), removes and rewrites the manifest, and waits until etcd's
  `/health` answers and the API serves again. SKILL.md's "on reload" doesn't apply.
- **Back to kubeadm's flags:** `cc_vm.py shipped`.

## Adding files
`cc_vm.py files <dir>` copies each file to `/argmatch-out/etcd/files/<name>` (the host's
`$CT/dump_ancestors/argmatch-out/etcd/files/`), mounted in the pod; refer to them by that path. Put a copy of each in
the output's `files/`: paramtrack installs them the same way (`--config-files`), symlinks as symlinks. For TLS, make
the file a **symlink** to the kubeadm file it stands for (`ln -s /etc/kubernetes/pki/etcd/server.crt <name>`): it
resolves in the pod, where that directory is mounted, so the API server's connection keeps working. Not a copy: each
new cluster has new certificates, so a copied one breaks the next freshly deployed cluster; and a certificate the API
server doesn't trust breaks it at once.

## Health
After each restart, through `cc_vm.py health`: etcd's `/health` ok, the API's `/readyz` ok, and no new pod restarts or
failing pods, except that kube-apiserver, kube-controller-manager and kube-scheduler restart once on each etcd
restart (they lose the API or their leases while it's down; 2026-11-22: once each, on the shipped config).

## Message labels (constraint 2)
SKILL.md's constraint 2 says how to check them. On prombench:
- **The baseline run** is the testbed's shared one (paramtrack's README, "Use with /concreteconfig", step 1), with
  etcd instrumented, and `scripts/check_labels.py` instruments the same modules.
- **Timings (2026-11-22):** a restart through `cc_vm.py` takes 1-2 minutes (etcd healthy after 68 s, the API serving
  after 106 s, on the shipped config). A paramtrack run through step 3 with etcd instrumented alone took about 9
  minutes on a reused cluster (etcd serving 36 s after its start), most of it the 240 s workload; a check
  instruments every module, which takes longer.
- **Labels that depend on timing:** not measured yet. etcd's labels are the API server's gRPC calls (`KV/Range`,
  `KV/Txn`, `Watch/Watch`, `Lease/LeaseGrant`, `Maintenance/Status`) and the kubelet's probes; the API server's own
  labels vary between runs (`../kube-apiserver/TESTBED.md`).

paramtrack's testbed leaves etcd running the config it ran, on the instrumented image. When done, put back whatever
the user wants the testbed left with: `cc_vm.py shipped`, or a paramtrack run that doesn't instrument etcd, puts back
kubeadm's manifest.
