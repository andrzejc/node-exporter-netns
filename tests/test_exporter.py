"""Tests for node_exporter_netns.py: parity with node_exporter v1.12.1 on fixed /proc/<pid>/net trees, and behaviour node_exporter
cannot show (a broken collector, SIGTERM). Standard library only: python -m unittest discover -s tests

tests/fixtures/<case>/net holds the files, tests/golden/<case>.prom what node_exporter v1.12.1 exported from them
(tools/make-golden.sh): full (a real host, addresses anonymised), no-ipv6 (snmp6, sockstat6 and udp6 absent, as with IPv6
disabled), edge (a device name with a quote and a backslash, no ARP entries, UDP sockets with queued data).
"""
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "node_exporter_netns.py"
FIXTURES = ROOT / "tests" / "fixtures"
GOLDEN = ROOT / "tests" / "golden"
SKIP = ("node_scrape_collector_duration_seconds",)  # timing, differs on every run


def run_metrics(net_dir: Path) -> str:
    """The exposition text the exporter serves for net_dir (imported fresh, as NET_DIR is read at import)."""
    code = "import node_exporter_netns as m; print(m.metrics(), end='')"
    env = {**os.environ, "NET_DIR": str(net_dir), "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-c", code], env=env, check=True, capture_output=True, text=True).stdout


def parse(text: str) -> tuple[dict[str, float], dict[tuple[str, str], str]]:
    """Samples (series -> value) and metadata ((HELP|TYPE, name) -> text) of an exposition, without the SKIP metrics."""
    samples: dict[str, float] = {}
    meta: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        if line.startswith(("# HELP ", "# TYPE ")):
            _, kind, name, rest = line.split(" ", 3)
            if name not in SKIP:
                meta[(kind, name)] = rest
        elif line and not line.startswith("#"):
            series, value = line.rsplit(" ", 1)
            if not series.startswith(SKIP):
                samples[series] = float(value)
    return samples, meta


class Parity(unittest.TestCase):
    def check(self, case: str) -> None:
        ours = parse(run_metrics(FIXTURES / case / "net"))
        theirs = parse((GOLDEN / f"{case}.prom").read_text())
        self.assertEqual(ours[0], theirs[0], "samples differ from node_exporter v1.12.1")
        self.assertEqual(ours[1], theirs[1], "HELP/TYPE differ from node_exporter v1.12.1")

    def test_full(self) -> None:
        self.check("full")

    def test_no_ipv6(self) -> None:
        self.check("no-ipv6")

    def test_edge(self) -> None:
        self.check("edge")


class Behaviour(unittest.TestCase):
    def test_broken_collector_does_not_stop_the_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            net = Path(tmp) / "net"
            shutil.copytree(FIXTURES / "full" / "net", net)
            (net / "netstat").unlink()
            samples, _ = parse(run_metrics(net))
        self.assertEqual(samples['node_scrape_collector_success{collector="netstat"}'], 0)
        for name in ("arp", "netdev", "sockstat", "udp_queues"):
            self.assertEqual(samples[f'node_scrape_collector_success{{collector="{name}"}}'], 1, name)
        self.assertFalse(any(s.startswith("node_netstat_") for s in samples))

    def test_sigterm_exits_promptly(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {**os.environ, "NET_DIR": str(FIXTURES / "full" / "net"), "LISTEN_PORT": str(port)}
        proc = subprocess.Popen([sys.executable, str(SCRIPT)], env=env)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:  # wait until it listens
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.05)
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=3), 0)
        finally:
            proc.kill()


if __name__ == "__main__":
    unittest.main()
