"""Point the Windows default output at a chosen device while speakers play, then put it back."""

from __future__ import annotations

import logging

from airplay_windows.core.settings import Settings

log = logging.getLogger(__name__)


class OutputSwitcher:
    """What to restore is saved before switching, so a crash can be undone at the next start."""

    def __init__(self, settings: Settings, get_default=None, set_default=None, roles=None):
        if get_default is None or set_default is None or roles is None:
            from airplay_windows.windows import default_output

            get_default = get_default or default_output.get_default_output
            set_default = set_default or default_output.set_default_output
            roles = roles or default_output.DEFAULT_ROLES
        self.settings = settings
        self.get_default, self.set_default, self.roles = get_default, set_default, tuple(roles)

    @property
    def active(self) -> dict | None:
        return self.settings.data.get("restore_output")

    def activate(self, target: str | None) -> bool:
        """Switch to `target` unless a switch is already in place. True if Windows changed."""
        if not target or self.active:
            return False
        try:
            previous = {str(role): self.get_default(role) for role in self.roles}
            if all(p == target for p in previous.values()):
                return False   # already the default: nothing to put back later
            self.settings.data["restore_output"] = {"target": target, "previous": previous}
            self.settings.save()
            self.set_default(target, self.roles)
            return True
        except OSError as exc:
            self.settings.data.pop("restore_output", None)
            log.warning("Could not change the Windows default output: %s", exc)
            return False

    def restore(self) -> bool:
        """Put the previous default back for each role still pointing at our device."""
        state = self.active
        if not state:
            return False
        changed = False
        for role, previous in state["previous"].items():
            try:
                # If the user picked another device meanwhile, leave their choice alone.
                if previous and previous != state["target"] and self.get_default(int(role)) == state["target"]:
                    self.set_default(previous, [int(role)])
                    changed = True
            except OSError as exc:   # e.g. the previous device was unplugged
                log.warning("Could not restore the Windows default output: %s", exc)
        self.settings.data.pop("restore_output", None)
        self.settings.save()
        return changed

    recover = restore   # at startup: undo a switch left behind by a crash
