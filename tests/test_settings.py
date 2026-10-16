"""Saved settings and the Windows output switcher (with a fake Windows)."""

import json
import sys

import pytest

from airplay_windows.core.output_switch import OutputSwitcher
from airplay_windows.core.settings import Settings
from airplay_windows.protocol import Receiver


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_settings_round_trip_with_encrypted_password(tmp_path):
    path = str(tmp_path / "settings.json")
    settings = Settings(path)
    settings.remember(Receiver("EAC4C5275084@Bedroom", "172.30.10.145", 7000, {"sf": "0x80484"}))
    settings.set_volume("EAC4C5275084", 42.4)
    settings.set_password("EAC4C5275084", "not-a-real-one")
    settings.set_auto_connect("EAC4C5275084", True)
    settings.save()
    assert "not-a-real-one" not in open(path, encoding="utf-8").read()

    again = Settings(path)
    assert again.password("EAC4C5275084") == "not-a-real-one"
    assert again.volume("EAC4C5275084") == 42 and again.auto_connect("EAC4C5275084")
    [known] = again.known_receivers()
    assert (known.name, known.host, known.txt) == ("Bedroom", "172.30.10.145", {"sf": "0x80484"})
    again.set_password("EAC4C5275084", None)
    assert again.password("EAC4C5275084") is None
    assert json.load(open(path, encoding="utf-8"))["devices"]["EAC4C5275084"]["volume"] == 42


def test_settings_defaults_when_missing_or_corrupt(tmp_path):
    path = tmp_path / "settings.json"
    assert Settings(str(path)).data["latency_ms"] == 2000
    path.write_text("{not json")
    assert Settings(str(path)).data["devices"] == {}


class FakeWindows:
    """One default device per role."""

    def __init__(self, default="headset"):
        self.defaults = {0: default, 1: default, 2: default}
        self.calls = []

    def get(self, role):
        return self.defaults[role]

    def set(self, device, roles):
        if device == "unplugged":
            raise OSError("element not found")
        self.calls.append((device, tuple(roles)))
        for role in roles:
            self.defaults[role] = device


def switcher(tmp_path, win):
    settings = Settings(str(tmp_path / "settings.json"))
    return OutputSwitcher(settings, win.get, win.set, roles=(0, 1)), settings


def test_switch_and_restore(tmp_path):
    win = FakeWindows()
    sw, _ = switcher(tmp_path, win)
    assert sw.activate("cable") and win.defaults == {0: "cable", 1: "cable", 2: "headset"}
    assert not sw.activate("cable")              # a second speaker doesn't switch again
    assert sw.restore() and win.defaults == {0: "headset", 1: "headset", 2: "headset"}
    assert sw.active is None and not sw.restore()


def test_no_switch_when_already_default(tmp_path):
    win = FakeWindows(default="cable")
    sw, _ = switcher(tmp_path, win)
    assert not sw.activate("cable") and not win.calls and sw.active is None
    assert not sw.activate(None)


def test_restore_leaves_a_device_the_user_picked(tmp_path):
    win = FakeWindows()
    sw, _ = switcher(tmp_path, win)
    sw.activate("cable")
    win.defaults[0] = win.defaults[1] = "monitor"
    assert not sw.restore() and win.defaults[1] == "monitor"


def test_crash_recovery(tmp_path):
    win = FakeWindows()
    sw, _ = switcher(tmp_path, win)
    sw.activate("cable")
    sw2, _ = switcher(tmp_path, win)     # next start, same settings file
    assert sw2.active["target"] == "cable"
    assert sw2.recover() and win.defaults[1] == "headset"


def test_restore_survives_an_unplugged_device(tmp_path):
    win = FakeWindows(default="unplugged")
    sw, _ = switcher(tmp_path, win)
    assert sw.activate("cable")
    sw.restore()
    assert sw.active is None and win.defaults[1] == "cable"
