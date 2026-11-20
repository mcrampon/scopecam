"""Global keyboard / mouse-button events, read with `libinput debug-events`.

Wayland does not let apps see input meant for other windows, so we read the input
devices directly. That needs read access to /dev/input/event* (the `input` group).
"""

import re
import subprocess
import threading

from gi.repository import Gio, GLib

LINE = re.compile(r"\s(KEYBOARD_KEY|POINTER_BUTTON)\s.*?\b((?:KEY|BTN)_\w+) \((\d+)\) (pressed|released)")


def _tap_enabled():
    try:
        source = Gio.SettingsSchemaSource.get_default()
        if source and source.lookup("org.gnome.desktop.peripherals.touchpad", True):
            return Gio.Settings.new("org.gnome.desktop.peripherals.touchpad").get_boolean("tap-to-click")
    except GLib.Error:
        pass
    return True


class InputWatcher:
    """Calls on_key(code, name, pressed) / on_button(name, pressed) on the main loop."""

    def __init__(self, on_key=None, on_button=None, on_error=None):
        self.on_key = on_key
        self.on_button = on_button
        self.on_error = on_error
        self.proc = None

    def start(self):
        cmd = ["libinput", "debug-events", "--show-keycodes"]
        # libinput's own context has default settings: match the desktop's tap-to-click,
        # otherwise touchpad taps would not show up as clicks.
        cmd.append("--enable-tap" if _tap_enabled() else "--disable-tap")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as e:
            self._error(f"Could not run libinput: {e}")
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _error(self, msg):
        if self.on_error:
            GLib.idle_add(self.on_error, msg)

    def _read(self):
        seen = False
        for line in self.proc.stdout:
            m = LINE.search(line)
            if not m:
                seen = seen or "DEVICE_ADDED" in line
                continue
            kind, name, code, state = m.groups()
            pressed = state == "pressed"
            if kind == "KEYBOARD_KEY" and self.on_key:
                GLib.idle_add(self.on_key, int(code), name, pressed)
            elif kind == "POINTER_BUTTON" and self.on_button:
                GLib.idle_add(self.on_button, name, pressed)
        if self.proc and self.proc.poll() not in (None, -15) and not seen:
            err = self.proc.stderr.read().strip()
            self._error("Cannot read input devices (is your user in the 'input' group?)\n" + err[-300:])

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.proc = None
