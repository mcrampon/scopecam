"""User settings, stored as JSON in $XDG_CONFIG_HOME/scopecam/config.json."""

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from gi.repository import GLib


def _user_dir(kind, fallback):
    path = GLib.get_user_special_dir(kind)
    return Path(path) if path else Path.home() / fallback


def default_screenshot_dir():
    # Same place GNOME's own tool uses.
    return str(_user_dir(GLib.UserDirectory.DIRECTORY_PICTURES, "Pictures") / "Screenshots")


def default_screencast_dir():
    return str(_user_dir(GLib.UserDirectory.DIRECTORY_VIDEOS, "Videos") / "Screencasts")


@dataclass
class Config:
    # Main UI state (remembered between runs)
    kind: str = "screenshot"  # screenshot | screencast
    mode: str = "region"  # region | screen | window
    screenshot_pointer: bool = False
    screencast_pointer: bool = True
    delay: int = 0
    record_desktop_audio: bool = False
    record_microphone: bool = False
    show_keys: bool = False
    show_clicks: bool = True
    encoding: str = "h264"  # key of recorder.ENCODINGS
    last_region: list = field(default_factory=list)  # [x, y, w, h] in logical px

    # Preferences
    screenshot_dir: str = field(default_factory=default_screenshot_dir)
    screencast_dir: str = field(default_factory=default_screencast_dir)
    screenshot_name: str = "Screenshot From %Y-%m-%d %H-%M-%S"
    screencast_name: str = "Screencast From %Y-%m-%d %H-%M-%S"
    copy_to_clipboard: bool = True
    framerate: int = 30
    keys_mode: str = "all"  # all | shortcuts
    portal_restore_token: str = ""

    @staticmethod
    def path():
        return Path(GLib.get_user_config_dir()) / "scopecam" / "config.json"

    @classmethod
    def load(cls):
        try:
            data = json.loads(cls.path().read_text())
        except (OSError, ValueError):
            data = {}
        # Older configs had one pointer setting for both kinds.
        if "show_pointer" in data:
            data.setdefault("screenshot_pointer", data["show_pointer"])
            data.setdefault("screencast_pointer", data["show_pointer"])
        if data.get("video_format") == "webm":
            data.setdefault("encoding", "vp8")
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self):
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, path)
