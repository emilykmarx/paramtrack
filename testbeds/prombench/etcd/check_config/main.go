// check_config: etcd has no command to check a config file, so this loads it as `etcd --config-file` does
// (embed.ConfigFromFile, from the etcd checkout this module's go.mod replaces etcd's modules with: v3.5.15) and
// validates it (Config.Validate, which etcd runs at start). It doesn't open listeners or files: a restart is the full
// check (../TESTBED.md). The config's NODE_IP must already be replaced (cc_vm.py check does).
//
// Usage: go run . <config file>   (in this directory; exit 1 if it doesn't load or validate)
package main

import (
	"fmt"
	"os"

	"go.etcd.io/etcd/server/v3/embed"
)

func main() {
	cfg, err := embed.ConfigFromFile(os.Args[1])
	if err != nil {
		fmt.Println("load:", err)
		os.Exit(1)
	}
	if err := cfg.Validate(); err != nil {
		fmt.Println("validate:", err)
		os.Exit(1)
	}
	fmt.Printf("ok: name %s, data-dir %s, listen-client-urls %v, listen-metrics-urls %v\n", cfg.Name, cfg.Dir,
		cfg.ListenClientUrls, cfg.ListenMetricsUrls)
}
