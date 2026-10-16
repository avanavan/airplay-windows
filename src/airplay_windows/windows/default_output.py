"""Read and change the Windows default playback device.

Reading uses the documented IMMDeviceEnumerator. Windows has no documented way
to change the default, so this uses the IPolicyConfig COM interface that the
Sound control panel itself uses (as do tools such as SoundSwitch and
AudioDeviceCmdlets). Device ids are endpoint id strings, the same ones the
`soundcard` package reports as Speaker.id.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

E_RENDER = 0
CONSOLE, MULTIMEDIA, COMMUNICATIONS = 0, 1, 2
# The roles the Sound settings page means by "default output". The communications
# role (used by call apps) is left alone so calls don't go to a delayed speaker.
DEFAULT_ROLES = (CONSOLE, MULTIMEDIA)

_CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_CLSID_PolicyConfigClient = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"
_IID_IPolicyConfig = "{F8679F50-850A-41CF-9C72-430F290290C8}"
_CLSCTX_ALL = 0x17


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> _GUID:
    guid = _GUID()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    return guid


def _call(obj: ctypes.c_void_p, index: int, argtypes: list, *args):
    """Call method `index` of a COM object's vtable; a failed HRESULT raises OSError."""
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])(obj, *args)


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])(obj)


def _create(clsid: str, iid: str) -> ctypes.c_void_p:
    _com_initialize()
    obj = ctypes.c_void_p()
    ctypes.oledll.ole32.CoCreateInstance(ctypes.byref(_guid(clsid)), None, _CLSCTX_ALL,
                                         ctypes.byref(_guid(iid)), ctypes.byref(obj))
    return obj


def _com_initialize() -> None:
    # S_FALSE (already initialised) and RPC_E_CHANGED_MODE (other model) are both fine.
    ctypes.windll.ole32.CoInitializeEx(None, 0)


def get_default_output(role: int = MULTIMEDIA) -> str | None:
    """Endpoint id of the default playback device for `role`, or None if there is none."""
    enumerator = _create(_CLSID_MMDeviceEnumerator, _IID_IMMDeviceEnumerator)
    device = ctypes.c_void_p()
    try:
        try:
            # IMMDeviceEnumerator::GetDefaultAudioEndpoint (vtable slot 4)
            _call(enumerator, 4, [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                  E_RENDER, role, ctypes.byref(device))
        except OSError:
            return None   # E_NOTFOUND: no playback device at all
        text = ctypes.c_void_p()
        _call(device, 5, [ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(text))  # IMMDevice::GetId
        try:
            return ctypes.wstring_at(text.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(text)
    finally:
        _release(device)
        _release(enumerator)


def set_default_output(device_id: str, roles=DEFAULT_ROLES) -> None:
    """Make `device_id` the default playback device for each role in `roles`."""
    policy = _create(_CLSID_PolicyConfigClient, _IID_IPolicyConfig)
    try:
        for role in roles:
            # IPolicyConfig::SetDefaultEndpoint (vtable slot 13)
            _call(policy, 13, [wintypes.LPCWSTR, ctypes.c_int], device_id, role)
    finally:
        _release(policy)
