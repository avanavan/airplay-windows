"""Tests for the Windows Firewall decision (no firewall is touched)."""

from airplay_windows.windows import firewall

PROGRAM = r"C:\Python311\pythonw.exe"


def profiles(**overrides):
    base = {name: {"name": name, "enabled": "True", "defaultInbound": "NotConfigured", "allowInboundRules": "True"}
            for name in ("Domain", "Private", "Public")}
    for name, values in overrides.items():
        base[name].update(values)
    return list(base.values())


def rule(action="Allow", profile="Any", protocol="UDP", enabled="True", local=True, port="Any", rid="r1"):
    return {"id": rid, "name": "x", "enabled": enabled, "direction": "Inbound", "action": action,
            "profile": profile, "protocol": protocol, "localPort": port, "local": local}


def status(rules, categories=("Public",), **profile_overrides):
    return firewall.evaluate({"categories": list(categories), "profiles": profiles(**profile_overrides),
                              "rules": rules}, PROGRAM)


def test_no_rule_means_blocked_on_the_active_network():
    s = status([])
    assert s.needed and s.fixable and "Public network" in s.message


def test_allow_rule_for_the_active_profile_is_enough():
    assert not status([rule()]).needed
    assert not status([rule(profile="Private, Public")]).needed
    assert status([rule(profile="Private")]).needed            # covers the wrong network
    assert status([rule(protocol="TCP")]).needed               # wrong protocol
    assert status([rule(enabled="False")]).needed
    assert status([rule(port="5000")]).needed                  # streamer ports are random


def test_firewall_off_or_default_allow_needs_nothing():
    assert not status([], Public={"enabled": "False"}).needed
    assert not status([], Public={"defaultInbound": "Allow"}).needed
    assert not status([], categories=()).needed                # not connected anywhere


def test_local_block_rule_wins_and_is_disabled_by_the_fix():
    s = status([rule(), rule(action="Block", profile="Public", rid="{blk}")])
    assert s.needed and s.fixable and [r["id"] for r in s.block_rules] == ["{blk}"]
    script = firewall.fix_script(s)
    assert "Disable-NetFirewallRule -Name '{blk}'" in script
    assert r"-Program 'C:\Python311\pythonw.exe'" in script and "-Protocol UDP" in script


def test_cases_a_rule_cannot_fix():
    assert not status([rule(action="Block", local=False)]).fixable          # group policy
    shields = status([rule()], Public={"allowInboundRules": "False"})
    assert shields.needed and not shields.fixable and "block all incoming" in shields.message


def test_fix_script_quotes_paths():
    s = firewall.FirewallStatus(r"C:\Users\O'Neil\python.exe", needed=True)
    assert r"-Program 'C:\Users\O''Neil\python.exe'" in firewall.fix_script(s)
