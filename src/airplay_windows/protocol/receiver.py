"""An AirPlay receiver as advertised over Bonjour (`_raop._tcp`), and what its TXT record means."""

from __future__ import annotations

from dataclasses import dataclass, field

ENCRYPTION_TYPES = {0: "none", 1: "RSA", 3: "FairPlay", 4: "MFi-SAP", 5: "FairPlay SAPv2.5"}
CODECS = {0: "PCM", 1: "ALAC", 2: "AAC", 3: "AAC-ELD"}

# Status flags ("sf") bits.
SF_PIN_REQUIRED = 0x8            # with SF_DEVICE_VERIFICATION: a new PIN every time
SF_PASSWORD_REQUIRED = 0x80      # "Require Password" (AirPlay 2 password pairing)
SF_DEVICE_VERIFICATION = 0x200   # Apple TV style on-screen PIN pairing

MODEL_NAMES = {
    "AudioAccessory1,1": "HomePod", "AudioAccessory1,2": "HomePod",
    "AudioAccessory5,1": "HomePod mini", "AudioAccessory6,1": "HomePod (2nd gen)",
    "AppleTV3,1": "Apple TV (3rd gen)", "AppleTV3,2": "Apple TV (3rd gen)",
    "AppleTV5,3": "Apple TV HD", "AppleTV6,2": "Apple TV 4K", "AppleTV11,1": "Apple TV 4K",
    "AppleTV14,1": "Apple TV 4K",
}


@dataclass
class Receiver:
    """One `_raop._tcp` service. `service_name` is "<hardware id>@<name>"."""

    service_name: str
    host: str
    port: int
    txt: dict[str, str] = field(default_factory=dict)
    is_manual: bool = False          # added by address; no TXT record to go by
    manual_encryption: bool = False  # for a manual receiver: use RSA/AES like an AirPort Express

    @classmethod
    def manual(cls, host: str, port: int = 7000, encrypt: bool = False) -> "Receiver":
        return cls(f"000000000000@{host}", host, port, is_manual=True, manual_encryption=encrypt)

    # -- identity --

    @property
    def device_id(self) -> str:
        return self.service_name.split("@", 1)[0] if "@" in self.service_name else ""

    @property
    def name(self) -> str:
        return self.service_name.split("@", 1)[1] if "@" in self.service_name else self.service_name

    @property
    def key(self) -> str:
        """Stable identity for settings: the hardware id, else the address."""
        if self.is_manual or not self.device_id or set(self.device_id) == {"0"}:
            return self.host
        return self.device_id

    @property
    def model(self) -> str | None:
        return self.txt.get("am")

    @property
    def model_name(self) -> str:
        model = self.model or ""
        if model in MODEL_NAMES:
            return MODEL_NAMES[model]
        if model.startswith("AirPort"):
            return "AirPort Express"
        return model or ("Added by address" if self.is_manual else "AirPlay speaker")

    # -- capabilities --

    @property
    def encryption_types(self) -> set[str]:
        if self.is_manual:
            return {"RSA"} if self.manual_encryption else {"none"}
        return {ENCRYPTION_TYPES[n] for n in _int_list(self.txt.get("et")) if n in ENCRYPTION_TYPES}

    @property
    def codecs(self) -> set[str]:
        return {CODECS[n] for n in _int_list(self.txt.get("cn")) if n in CODECS}

    @property
    def rsa_encryption(self) -> bool:
        """The receiver lists RSA, so the audio is AES-encrypted under an RSA-wrapped key."""
        return "RSA" in self.encryption_types

    @property
    def needs_auth_setup(self) -> bool:
        """MFi receivers (and unknown manual ones) expect a POST /auth-setup before OPTIONS."""
        return "MFi-SAP" in self.encryption_types or (self.is_manual and not self.rsa_encryption)

    @property
    def legacy_password(self) -> bool:
        """An AirPlay 1 password, checked with RTSP digest authentication."""
        return self.txt.get("pw", "").lower() == "true"

    @property
    def digest_lowercase(self) -> bool:
        return self.txt.get("da", "").lower() == "true"

    @property
    def status_flags(self) -> int:
        try:
            return int(self.txt.get("sf", "0"), 16)
        except ValueError:
            return 0

    @property
    def requires_password_pairing(self) -> bool:
        """"Require Password" on a HomePod or AirPlay 2 receiver: pair over HomeKit with the password."""
        return bool(self.status_flags & SF_PASSWORD_REQUIRED)

    @property
    def requires_pin_pairing(self) -> bool:
        """An on-screen PIN (Apple TV); not supported."""
        flags = self.status_flags
        return bool(flags & SF_DEVICE_VERIFICATION) and not flags & SF_PASSWORD_REQUIRED

    @property
    def needs_password(self) -> bool:
        return self.requires_password_pairing or self.legacy_password


def _int_list(value: str | None) -> list[int]:
    out = []
    for part in (value or "").split(","):
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out
