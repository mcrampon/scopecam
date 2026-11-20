"""Capture backends.

GnomeBackend talks to Mutter directly (no permission dialogs) and is used on GNOME.
PortalBackend uses xdg-desktop-portal and works on any desktop that implements it.
"""

import os


def detect(bus, config=None):
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    force = os.environ.get("SCOPECAM_BACKEND", "")
    if force != "portal" and ("gnome" in desktop or force == "gnome"):
        from .gnome import GnomeBackend

        backend = GnomeBackend(bus, config)
        if backend.available():
            return backend
    from .portal import PortalBackend

    return PortalBackend(bus, config)
