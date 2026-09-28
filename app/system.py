"""
Pishak Home — system health helpers.

Small wrapper around psutil for the numbers the dashboard and the
StorageLow event need. Kept side-effect-free and easy to unit test.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import psutil

START_TIME = time.time()


@dataclass
class SystemStatus:
    cpu_percent: float
    ram_used_mb: float
    ram_total_mb: float
    ram_percent: float
    disk_free_mb: float
    disk_total_mb: float
    disk_percent_used: float
    uptime_seconds: float


def get_system_status(path_for_disk: str = "/") -> SystemStatus:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(path_for_disk)
    return SystemStatus(
        cpu_percent=psutil.cpu_percent(interval=0.1),
        ram_used_mb=round(vm.used / (1024 * 1024), 1),
        ram_total_mb=round(vm.total / (1024 * 1024), 1),
        ram_percent=vm.percent,
        disk_free_mb=round(disk.free / (1024 * 1024), 1),
        disk_total_mb=round(disk.total / (1024 * 1024), 1),
        disk_percent_used=disk.percent,
        uptime_seconds=round(time.time() - START_TIME, 1),
    )


def is_storage_low(path: str, threshold_mb: float) -> bool:
    free_mb = psutil.disk_usage(path).free / (1024 * 1024)
    return free_mb < threshold_mb


def dir_size_mb(path: str) -> float:
    p = Path(path)
    if not p.exists():
        return 0.0
    total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return round(total / (1024 * 1024), 1)
