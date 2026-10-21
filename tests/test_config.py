import can

from cananalyser.bus import BusConfig
from cananalyser.gui import main_window


def test_kvaser_defaults_to_normal_mode(monkeypatch):
    opened = {}
    monkeypatch.setattr(can, "Bus", lambda **kw: opened.update(kw))
    BusConfig().open()
    assert opened["driver_mode"] is True  # python-can: True = normal, False = silent
    BusConfig(listen_only=True).open()
    assert opened["driver_mode"] is False


def test_kvaser_channel_labels_put_virtual_last(monkeypatch):
    configs = [
        {"channel": 0, "device_name": "Kvaser Virtual CAN Driver", "serial": 0, "dongle_channel": 1},
        {"channel": 2, "device_name": "Kvaser USBcan Light 2xHS", "serial": 12345, "dongle_channel": 1},
    ]
    monkeypatch.setattr(can, "detect_available_configs", lambda interfaces: configs)
    labels = main_window.kvaser_channels()
    assert labels[0] == ("2: Kvaser USBcan Light 2xHS S/N 12345 ch 1", 2)
    assert "Virtual" in labels[1][0]
