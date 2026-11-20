#!/bin/bash
# Regenerate tests/golden/<case>.prom: node_exporter v1.12.1 (the last release that reads a /proc/<pid>-style tree through
# --path.procfs) run on each tests/fixtures/<case>, keeping the node_* metrics the parity test compares. Needs Docker.
# Usage: tools/make-golden.sh   (from the repository root)
set -euo pipefail

IMAGE="prom/node-exporter:v1.12.1"
cd "$(dirname "$0")/.."
for dir in tests/fixtures/*/; do
    case=$(basename "$dir")
    id=$(docker run -d --rm --network none -v "$PWD/$dir:/fx:ro" "$IMAGE" \
        --path.procfs=/fx --web.listen-address=127.0.0.1:9100 --collector.disable-defaults \
        --collector.netdev --no-collector.netdev.netlink --collector.netstat --collector.sockstat \
        --collector.arp --no-collector.arp.netlink --collector.udp_queues)
    sleep 1
    docker exec "$id" wget -qO- http://127.0.0.1:9100/metrics \
        | grep -v -E '^(# (HELP|TYPE) )?(go_|process_|promhttp_|node_exporter_build_info|node_scrape_collector_duration_seconds)' \
        > "tests/golden/$case.prom"
    docker rm -f "$id" > /dev/null
    echo "tests/golden/$case.prom: $(grep -c -v '^#' "tests/golden/$case.prom") samples"
done
