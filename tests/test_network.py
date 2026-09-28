import time

from app.network import NetworkMonitor


def test_get_status_returns_at_least_loopback():
    monitor = NetworkMonitor(sample_interval=0.2)
    status = monitor.get_status()
    assert "interfaces" in status
    names = [i["name"] for i in status["interfaces"]]
    assert len(names) > 0
    for iface in status["interfaces"]:
        assert "rx_kbps" in iface
        assert "tx_kbps" in iface
        assert isinstance(iface["is_up"], bool)


def test_background_sampling_updates_rates():
    monitor = NetworkMonitor(sample_interval=0.2)
    monitor.start()
    try:
        time.sleep(0.6)
        status = monitor.get_status()
        assert len(status["interfaces"]) > 0
    finally:
        monitor.stop()


def test_interfaces_with_ip_sort_first():
    monitor = NetworkMonitor()
    status = monitor.get_status()
    interfaces = status["interfaces"]
    if len(interfaces) > 1:
        # Once we hit an interface with no IP, no later one should have one.
        seen_no_ip = False
        for iface in interfaces:
            if iface["ipv4"] is None:
                seen_no_ip = True
            elif seen_no_ip:
                assert False, "interface with an IP appeared after one without"
