#!/usr/bin/env python3
"""Prometheus exporter for another network namespace's statistics, with node_exporter's metric names; no host networking needed.

/proc/<pid>/net shows the network namespace of that process. With the host's /proc mounted at /host/proc, /host/proc/1/net is the
host's (PID 1 = host init) while the exporter stays in its own network namespace: no access to the host's loopback-only services or
its interfaces, which network_mode: host would give. node_exporter supported this with --path.procfs=/host/proc/1 until v1.13
(see prometheus/node_exporter#3917), so this reimplements those five collectors with the same metric names, labels, types and help
texts, as a drop-in replacement for dashboards and alerts built on them:

  netdev      /net/dev       node_network_{receive,transmit}_<field>_total{device}
  netstat     /net/netstat, /net/snmp, /net/snmp6   node_netstat_<Proto>_<Field> (node_exporter's default field filter)
  sockstat    /net/sockstat, /net/sockstat6         node_sockstat_<PROTO>_<field>, *_mem_bytes, node_sockstat_sockets_used
  arp         /net/arp       node_arp_entries{device}
  udp_queues  /net/udp, /net/udp6                   node_udp_queues{ip,queue}

plus node_scrape_collector_{success,duration_seconds}{collector}; a failing collector reports success 0 and the others go on. Files
are read at every scrape, nothing is cached. Python standard library only; the files are world-readable, so it runs as nobody.

Usage: node_exporter_netns.py   (listens on $LISTEN_PORT, default 9100; reads $NET_DIR, default /host/proc/1/net)
"""
import os
import re
import signal
import sys
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

PORT: Final = int(os.environ.get("LISTEN_PORT", "9100"))
NET_DIR: Final = os.environ.get("NET_DIR", "/host/proc/1/net")
PAGE_SIZE: Final = os.sysconf("SC_PAGE_SIZE")

# node_exporter's --collector.netstat.fields default
NETSTAT_FIELDS: Final = re.compile(
    r"^(.*_(InErrors|InErrs)|Ip_Forwarding|Ip(6|Ext)_(InOctets|OutOctets)|Icmp6?_(InMsgs|OutMsgs)|TcpExt_(Listen.*|Syncookies.*|TCPSynRetrans"
    r"|TCPTimeouts|TCPOFOQueue|TCPRcvQDrop)|Tcp_(ActiveOpens|InSegs|OutSegs|OutRsts|PassiveOpens|RetransSegs|CurrEstab)|Udp6?_(InDatagrams"
    r"|OutDatagrams|NoPorts|RcvbufErrors|SndbufErrors))$")
RECEIVE: Final = ("bytes", "packets", "errs", "drop", "fifo", "frame", "compressed", "multicast")
TRANSMIT: Final = ("bytes", "packets", "errs", "drop", "fifo", "colls", "carrier", "compressed")


class Out:
    """Exposition text: HELP and TYPE once per metric name, then its samples."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.seen: set[str] = set()

    def add(self, name: str, kind: str, help_: str, value: float, labels: dict[str, str] | None = None) -> None:
        if name not in self.seen:
            self.seen.add(name)
            self.lines += [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
        lab = ",".join(f'{k}="{escape(v)}"' for k, v in (labels or {}).items())
        self.lines.append(f"{name}{{{lab}}} {value!r}" if lab else f"{name} {value!r}")


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def read(name: str) -> list[str]:
    with open(os.path.join(NET_DIR, name)) as f:
        return f.read().splitlines()


def read_ipv6(name: str) -> list[str] | None:
    """The IPv6 files (snmp6, sockstat6, udp6) are missing when IPv6 is disabled; node_exporter skips them then."""
    try:
        return read(name)
    except FileNotFoundError:
        return None


def netdev(out: Out) -> None:
    rows = []
    for line in read("dev")[2:]:
        device, _, data = line.partition(":")
        rows.append((device.strip(), [float(x) for x in data.split()]))
    # node_exporter emits metric by metric, devices sorted inside each
    for i, field in enumerate(RECEIVE):
        for device, v in sorted(rows):
            out.add(f"node_network_receive_{field}_total", "counter", f"Network device statistic receive_{field}.", v[i], {"device": device})
    for i, field in enumerate(TRANSMIT):
        for device, v in sorted(rows):
            out.add(f"node_network_transmit_{field}_total", "counter", f"Network device statistic transmit_{field}.", v[8 + i],
                    {"device": device})


def netstat(out: Out) -> None:
    stats: dict[str, float] = {}
    for name in ("netstat", "snmp"):
        lines = read(name)
        for header, values in zip(lines[::2], lines[1::2], strict=True):  # "Proto: Field ..." then "Proto: value ..."
            proto, *fields = header.split()
            for field, value in zip(fields, values.split()[1:], strict=True):
                stats[f"{proto[:-1]}_{field}"] = float(value)
    for line in read_ipv6("snmp6") or []:
        key, value = line.split()
        m = re.match(r"^([A-Za-z]+6)(.+)$", key)  # Ip6InOctets, Udp6NoPorts, UdpLite6InErrors
        if m:
            stats[f"{m.group(1)}_{m.group(2)}"] = float(value)
    for key in sorted(stats):
        if NETSTAT_FIELDS.match(key):
            out.add(f"node_netstat_{key}", "untyped", f"Statistic {key.replace('_', '')}.", stats[key])


def sockstat(out: Out) -> None:
    values: dict[str, tuple[str, float]] = {}
    for lines in (read("sockstat"), read_ipv6("sockstat6") or []):
        for line in lines:
            proto, _, rest = line.partition(":")
            words = rest.split()
            if proto == "sockets":  # "sockets: used N"
                values["sockets_used"] = ("Number of IPv4 sockets in use.", float(words[1]))
                continue
            for field, value in zip(words[::2], words[1::2], strict=True):
                values[f"{proto}_{field}"] = (f"Number of {proto} sockets in state {field}.", float(value))
                if field == "mem":  # in pages
                    values[f"{proto}_mem_bytes"] = (f"Number of {proto} sockets in state mem_bytes.", float(value) * PAGE_SIZE)
    for key in sorted(values):
        out.add(f"node_sockstat_{key}", "gauge", *values[key])


def arp(out: Out) -> None:
    count: dict[str, int] = {}
    for line in read("arp")[1:]:
        device = line.split()[-1]
        count[device] = count.get(device, 0) + 1
    for device in sorted(count):
        out.add("node_arp_entries", "gauge", "ARP entries by device", float(count[device]), {"device": device})


def udp_queues(out: Out) -> None:
    for ip, lines in (("v4", read("udp")), ("v6", read_ipv6("udp6"))):
        if lines is None:
            continue
        tx = rx = 0
        for line in lines[1:]:
            t, r = line.split()[4].split(":")  # tx_queue:rx_queue, hex
            tx, rx = tx + int(t, 16), rx + int(r, 16)
        for queue, value in (("rx", rx), ("tx", tx)):
            out.add("node_udp_queues", "gauge", "Number of allocated memory in the kernel for UDP datagrams in bytes.", float(value),
                    {"ip": ip, "queue": queue})


COLLECTORS: Final[dict[str, Callable[[Out], None]]] = {"arp": arp, "netdev": netdev, "netstat": netstat, "sockstat": sockstat,
                                                        "udp_queues": udp_queues}


def metrics() -> str:
    out, result = Out(), {}
    for name, collect in COLLECTORS.items():
        start, ok = time.monotonic(), 1.0
        try:
            collect(out)
        except (OSError, ValueError, IndexError) as e:  # one broken file must not take the other collectors down
            print(f"{name}: {e!r}", file=sys.stderr, flush=True)
            ok = 0.0
        result[name] = (time.monotonic() - start, ok)
    for name in sorted(result):
        out.add("node_scrape_collector_duration_seconds", "gauge", "node_exporter: Duration of a collector scrape.", result[name][0],
                {"collector": name})
    for name in sorted(result):
        out.add("node_scrape_collector_success", "gauge", "node_exporter: Whether a collector succeeded.", result[name][1],
                {"collector": name})
    return "\n".join(out.lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/metrics":
            self.reply(200, metrics())
        elif self.path in ("/", "/healthz"):
            self.reply(200, "node_exporter_netns: use /metrics\n")
        else:
            self.reply(404, "not found\n")

    def reply(self, code: int, body: str) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass  # one line per scrape is noise


def main() -> None:
    # As PID 1 in a container, Python ignores SIGTERM (no default action for PID 1): `docker stop` would wait its 10 s
    # and SIGKILL. Exit cleanly instead.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
