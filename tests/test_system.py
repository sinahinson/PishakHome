from app.system import dir_size_mb, get_system_status, is_storage_low


def test_get_system_status_returns_sane_values():
    status = get_system_status("/")
    assert 0 <= status.cpu_percent <= 100
    assert status.ram_total_mb > 0
    assert status.disk_total_mb > 0
    assert status.uptime_seconds >= 0


def test_is_storage_low_with_absurd_threshold():
    # Any real filesystem has less than this free, so it should read as "low".
    assert is_storage_low("/", threshold_mb=10**9) is True


def test_is_storage_low_with_zero_threshold():
    assert is_storage_low("/", threshold_mb=0) is False


def test_dir_size_mb_for_missing_dir():
    assert dir_size_mb("/definitely/not/a/real/path") == 0.0
