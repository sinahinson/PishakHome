"""
Pishak Home — network status.

Reports what the dashboard's "Network" card needs: every interface's IP,
link state, link speed, and *live* throughput. Throughput can't be read
from a single sample (the counters are cumulative byte totals since boot),
so a background thread samples psutil's counters every couple of seconds
and keeps a rolling rate — the HTTP request just reads the latest value
instead of blocking for a sample window.

Deliberately does not do an active speedtest (downloading test files to
measure "internet speed") — that's needless network load for a background
monitoring app. Real, current throughput of actual traffic is more useful
and has zero cost.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Optional

import psutil


class NetworkMonitor:
    def __init__(self, sample_interval: float = 2.0):
        self.sample_interval = sample_interval
        self._lock = threading.Lock()
        self._prev_counters: Optional[dict[str, Any]] = None
        self._prev_time: Optional[float] = None
        self._rates: dict[str, dict[str, float]] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._sample_once()
            self._stop_event.wait(self.sample_interval)

    def _sample_once(self) -> None:
        counters = psutil.net_io_counters(pernic=True)
        now = time.time()
        with self._lock:
            if self._prev_counters and self._prev_time:
                dt = now - self._prev_time
                rates: dict[str, dict[str, float]] = {}
                if dt > 0:
                    for iface, current in counters.items():
                        previous = self._prev_counters.get(iface)
                        if previous is None:
                            continue
                        rx_bps = max(0.0, (current.bytes_recv - previous.bytes_recv) / dt) * 8
                        tx_bps = max(0.0, (current.bytes_sent - previous.bytes_sent) / dt) * 8
                        rates[iface] = {"rx_bps": rx_bps, "tx_bps": tx_bps}
                self._rates = rates
            self._prev_counters = counters
            self._prev_time = now

    def get_status(self) -> dict[str, Any]:
        # Take at least one sample immediately if we've never sampled yet,
        # so the very first API call after startup isn't empty.
        with self._lock:
            have_sample = self._prev_counters is not None
        if not have_sample:
            self._sample_once()

        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        with self._lock:
            rates = dict(self._rates)

        interfaces = []
        for name, stat in stats.items():
            ipv4 = None
            mac = None
            for addr in addrs.get(name, []):
                family_name = getattr(addr.family, "name", str(addr.family))
                if family_name == "AF_INET":
                    ipv4 = addr.address
                elif family_name in ("AF_PACKET", "AF_LINK"):
                    mac = addr.address
            rate = rates.get(name, {"rx_bps": 0.0, "tx_bps": 0.0})
            interfaces.append(
                {
                    "name": name,
                    "is_up": stat.isup,
                    "speed_mbps": stat.speed,
                    "mtu": stat.mtu,
                    "ipv4": ipv4,
                    "mac": mac,
                    "rx_kbps": round(rate["rx_bps"] / 1000, 1),
                    "tx_kbps": round(rate["tx_bps"] / 1000, 1),
                }
            )
        # Real interfaces with an IP first, loopback/inactive last.
        interfaces.sort(key=lambda i: (i["ipv4"] is None, not i["is_up"], i["name"]))
        return {"interfaces": interfaces}
