"""Check whether Windows Firewall lets speakers reach this app, and add a rule if not.

AirPlay speakers send UDP to the sender (clock-sync requests and resend
requests). Windows Firewall blocks unsolicited inbound UDP by default, so the
program that owns the sockets needs an inbound allow rule. The installer adds
one for airplay-windows.exe; this module checks for it and, when the app runs
from source or the rule is missing, adds one through a UAC prompt.

The rules and profiles are read with PowerShell's NetSecurity cmdlets; the
decision is made in Python (`evaluate`) so it can be tested without a firewall.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
from dataclasses import dataclass, field

from airplay_windows import DISPLAY_NAME

RULE_DESCRIPTION = "Lets AirPlay speakers send clock-sync and retransmission requests to AirPlay for Windows."


def rule_name(program: str) -> str:
    return f"{DISPLAY_NAME} ({os.path.basename(program)})"


def current_program() -> str:
    """This process's executable. From a venv that is the base interpreter, which owns the sockets."""
    buf = ctypes.create_unicode_buffer(32768)
    ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf))
    return buf.value


@dataclass
class FirewallStatus:
    program: str
    needed: bool = False            # True: speakers' UDP will be blocked
    fixable: bool = True            # False: a rule can't help (e.g. "block all incoming connections")
    message: str = ""
    block_rules: list[dict] = field(default_factory=list)  # local block rules to disable
    error: str = ""                 # the check itself failed


def _ps_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


_CHECK_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$target = [Environment]::ExpandEnvironmentVariables(%PROGRAM%).ToLowerInvariant()
$categories = @(Get-NetConnectionProfile | ForEach-Object {
    $c = $_.NetworkCategory.ToString(); if ($c -eq 'DomainAuthenticated') { 'Domain' } else { $c } })
$profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore | ForEach-Object {
    [pscustomobject]@{ name = $_.Name.ToString(); enabled = $_.Enabled.ToString();
                       defaultInbound = $_.DefaultInboundAction.ToString();
                       allowInboundRules = $_.AllowInboundRules.ToString() } })
$rules = @(foreach ($f in Get-NetFirewallApplicationFilter -PolicyStore ActiveStore) {
    if (-not $f.Program) { continue }
    if ([Environment]::ExpandEnvironmentVariables($f.Program).ToLowerInvariant() -ne $target) { continue }
    $r = $f | Get-NetFirewallRule
    $p = $r | Get-NetFirewallPortFilter
    [pscustomobject]@{ id = $r.Name; name = $r.DisplayName; enabled = $r.Enabled.ToString();
                       direction = $r.Direction.ToString(); action = $r.Action.ToString();
                       profile = $r.Profile.ToString(); protocol = $p.Protocol.ToString();
                       localPort = (@($p.LocalPort) -join ',');
                       local = ($r.PolicyStoreSourceType.ToString() -eq 'Local') } })
[pscustomobject]@{ categories = $categories; profiles = $profiles; rules = $rules } | ConvertTo-Json -Depth 4 -Compress
"""


def _powershell(script: str, timeout: float = 60) -> str:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-EncodedCommand", encoded], capture_output=True, text=True, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip().splitlines()[0] if (done.stderr or done.stdout)
                           else f"powershell exited with {done.returncode}")
    return done.stdout


def _covers(rule_profile: str, profile: str) -> bool:
    return rule_profile == "Any" or profile in {p.strip() for p in rule_profile.split(",")}


def _applies(rule: dict) -> bool:
    """An enabled inbound rule for UDP on any local port (what the streamer's random ports need)."""
    return (rule.get("enabled") == "True" and rule.get("direction") == "Inbound"
            and rule.get("protocol") in ("UDP", "Any") and rule.get("localPort") in ("", "Any", None))


def evaluate(data: dict, program: str) -> FirewallStatus:
    """Decide from the profiles and this program's rules whether speakers' UDP gets through."""
    status = FirewallStatus(program)
    categories = set(data.get("categories") or [])
    profiles = [p for p in data.get("profiles") or [] if p["name"] in categories and p["enabled"] != "False"]
    if not profiles:
        return status   # firewall off on every connected network

    if any(p["allowInboundRules"] == "False" for p in profiles):
        names = " and ".join(p["name"] for p in profiles if p["allowInboundRules"] == "False")
        status.needed, status.fixable = True, False
        status.message = (f"Windows Firewall is set to block all incoming connections on the {names} network, "
                          "so speakers can't send clock-sync packets to this app. Turn off \"Blocks all incoming "
                          "connections\" in Windows Security > Firewall & network protection.")
        return status

    rules = [r for r in data.get("rules") or [] if _applies(r)]
    blocks = [r for r in rules if r["action"] == "Block" and any(_covers(r["profile"], p["name"]) for p in profiles)]
    uncovered = [p["name"] for p in profiles if p["defaultInbound"] != "Allow" and not any(
        r["action"] == "Allow" and _covers(r["profile"], p["name"]) for r in rules)]

    if blocks:
        status.needed = True
        status.block_rules = [r for r in blocks if r.get("local")]
        if len(status.block_rules) < len(blocks):
            status.fixable = False
            status.message = ("A firewall rule set by your organisation blocks this app from receiving the "
                              "speakers' packets. Ask your administrator to allow it.")
        else:
            status.message = ("Windows Firewall has a rule blocking this app (probably from an earlier "
                              "“Allow access?” prompt that was cancelled), so speakers can't send clock-sync "
                              "packets to this app.")
    elif uncovered:
        status.needed = True
        networks = " and ".join(uncovered) + (" networks" if len(uncovered) > 1 else " network")
        status.message = (f"Windows Firewall will block the speakers' clock-sync packets to this app on {networks}, "
                          "so AirPlay 2 speakers such as the HomePod won't start playing.")
    return status


def check(program: str | None = None) -> FirewallStatus:
    program = program or current_program()
    try:
        raw = _powershell(_CHECK_SCRIPT.replace("%PROGRAM%", _ps_quote(program)))
        return evaluate(json.loads(raw), program)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return FirewallStatus(program, error=str(exc))


def fix_script(status: FirewallStatus) -> str:
    """PowerShell that disables the blocking rules and adds an allow rule for the program."""
    name, program = rule_name(status.program), status.program
    lines = ["$ErrorActionPreference = 'Stop'", "try {"]
    for rule in status.block_rules:
        lines.append(f"  Disable-NetFirewallRule -Name {_ps_quote(rule['id'])}")
    lines += [
        f"  Remove-NetFirewallRule -DisplayName {_ps_quote(name)} -ErrorAction SilentlyContinue",
        f"  New-NetFirewallRule -DisplayName {_ps_quote(name)} -Description {_ps_quote(RULE_DESCRIPTION)} "
        f"-Direction Inbound -Action Allow -Protocol UDP -Program {_ps_quote(program)} "
        "-RemoteAddress LocalSubnet -Profile Any | Out-Null",
        "  exit 0",
        "} catch { exit 1 }",
    ]
    return "\n".join(lines)


ERROR_CANCELLED = 1223


def run_elevated_powershell(script: str, timeout_ms: int = 120_000) -> int:
    """Run `script` as administrator (shows the UAC prompt). Returns its exit code.

    Raises PermissionError if the user declines the UAC prompt.
    """
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40 | 0x100          # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}"
    info.nShow = 0                     # SW_HIDE
    shell32, kernel32 = ctypes.WinDLL("shell32", use_last_error=True), ctypes.WinDLL("kernel32")
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(ShellExecuteInfo)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error()
        if code == ERROR_CANCELLED:
            raise PermissionError("administrator permission was declined")
        raise ctypes.WinError(code)
    try:
        kernel32.WaitForSingleObject(wintypes.HANDLE(info.hProcess), timeout_ms)
        exit_code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(wintypes.HANDLE(info.hProcess), ctypes.byref(exit_code))
        return exit_code.value
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(info.hProcess))


def allow(status: FirewallStatus) -> FirewallStatus:
    """Ask for administrator rights, add the rule, then check again."""
    code = run_elevated_powershell(fix_script(status))
    if code != 0:
        raise RuntimeError(f"adding the firewall rule failed (exit code {code})")
    return check(status.program)
