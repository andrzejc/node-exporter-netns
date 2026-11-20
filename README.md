# node_exporter_netns

Host network statistics for Prometheus from a container that does **not** run with `network_mode: host`, with
node_exporter's metric names, labels and help texts. A drop-in replacement for node_exporter's `netdev`, `netstat`,
`sockstat`, `arp` and `udp_queues` collectors, so existing dashboards and alerts keep working.

## Why

`/proc/<pid>/net` shows the network namespace of that process. Mount the host's `/proc` read-only into the container
and `/host/proc/1/net` is the host's network stack (PID 1 is the host's init), while the exporter itself stays in its
own network namespace. With `network_mode: host` the exporter would also reach every service the host binds to
loopback and could listen on every host interface; reading a few files under `/proc/1/net` needs neither.

node_exporter did this with `--path.procfs=/host/proc/1` up to v1.12.1. v1.13 broke it, and upstream treats a PID
directory as an invalid `--path.procfs` ([prometheus/node_exporter#3917](https://github.com/prometheus/node_exporter/issues/3917)).

## What it exports

| collector | source (under `/host/proc/1/net`) | metrics |
|---|---|---|
| netdev | `dev` | `node_network_{receive,transmit}_<field>_total{device}` |
| netstat | `netstat`, `snmp`, `snmp6` | `node_netstat_<Proto>_<Field>`, filtered like node_exporter's default `--collector.netstat.fields` |
| sockstat | `sockstat`, `sockstat6` | `node_sockstat_<PROTO>_<field>`, `*_mem_bytes`, `node_sockstat_sockets_used` |
| arp | `arp` | `node_arp_entries{device}` |
| udp_queues | `udp`, `udp6` | `node_udp_queues{ip,queue}` |

plus `node_scrape_collector_success` and `node_scrape_collector_duration_seconds` per collector. A collector that fails
reports `success 0` and logs the reason to stderr; the others keep working. On hosts with IPv6 disabled the IPv6 files
are absent and skipped, as node_exporter does.

Checked against node_exporter v1.12.1 with the same collectors on the same host: identical metric names, labels, types
and help texts, values equal within the counters' movement between the two reads.

## Running

One file, Python 3.11+ standard library only. Everything it reads is world-readable, so it runs as `nobody`, read-only,
without capabilities. Example for Docker Compose ([compose.example.yaml](compose.example.yaml)):

```yaml
services:
  node-exporter-netns:
    image: python:3.13-alpine   # pin an exact version in production
    command: [python, /app/node_exporter_netns.py]
    user: "65534:65534"
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    environment:
      PYTHONDONTWRITEBYTECODE: 1
    volumes:
      - ./node_exporter_netns.py:/app/node_exporter_netns.py:ro
      - /proc:/host/proc:ro
```

and scrape it at `node-exporter-netns:9100` from a Prometheus on the same Docker network. Run node_exporter for
everything else with these five collectors disabled (`--no-collector.netdev --no-collector.netstat
--no-collector.sockstat --no-collector.arp --no-collector.udp_queues`), so they are not reported twice from the
container's own namespace.

Environment: `LISTEN_PORT` (default `9100`), `NET_DIR` (default `/host/proc/1/net`). To watch another namespace, point
`NET_DIR` at `/host/proc/<pid>/net` of a process inside it.

## Not covered

`conntrack` (reads `/proc/sys/net`, which has no per-process view) and `netclass` (sysfs, not namespaced; node_exporter
can keep it). No TLS or authentication: keep the port on an internal network.

## Development

```
python -m unittest discover -s tests -v
ruff check . && mypy node_exporter_netns.py tests/test_exporter.py
```

The parity tests run the exporter on fixed trees under `tests/fixtures/<case>/net` and compare every sample, HELP and
TYPE line with `tests/golden/<case>.prom`, which `tools/make-golden.sh` produces with node_exporter v1.12.1 (Docker) from
the same files. Cases: `full` (a real host, addresses anonymised), `no-ipv6`, `edge` (a device name with a quote and a
backslash, no ARP entries, UDP sockets with queued data). To cover a new case, add a fixture directory and rerun the
script.
