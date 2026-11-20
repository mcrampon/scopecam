"""GNOME backend: Mutter's ScreenCast D-Bus API plus the companion Shell extension.

Mutter's API does not prompt the user, and RecordArea/RecordWindow give us exactly the
pixels we want. The extension (extension/scopecam@mcrampon) adds what Mutter does not
expose to apps: the window list, the pointer position, and a stop button in the top bar.
"""

import json

from gi.repository import Gio, GLib

from ..geometry import Rect, desktop_rect
from .base import Backend, Stream, Target, WindowInfo

MUTTER = "org.gnome.Mutter.ScreenCast"
MUTTER_PATH = "/org/gnome/Mutter/ScreenCast"
EXT = "io.github.mcrampon.Scopecam.Shell"
EXT_PATH = "/io/github/mcrampon/Scopecam/Shell"


class GnomeBackend(Backend):
    name = "gnome"
    has_indicator = True

    def __init__(self, bus, config=None):
        self.bus = bus
        self.config = config
        self._stop_callbacks = []
        self._stop_sub = None

    # --- availability -------------------------------------------------------

    def _has_owner(self, name):
        try:
            res = self.bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                     "NameHasOwner", GLib.Variant("(s)", (name,)), None,
                                     Gio.DBusCallFlags.NONE, 1000, None)
            return res.unpack()[0]
        except GLib.Error:
            return False

    def available(self):
        return self._has_owner(MUTTER)

    @property
    def has_extension(self):
        return self._has_owner(EXT)

    @property
    def has_pointer(self):
        return self.has_extension

    @property
    def lists_windows(self):
        return self.has_extension

    def _ext_call(self, method, args=None, reply=None, timeout=1000):
        return self.bus.call_sync(EXT, EXT_PATH, EXT, method, args, reply, Gio.DBusCallFlags.NONE, timeout, None)

    # --- screenshots ----------------------------------------------------------

    def freeze(self, cursor, callback):
        from ..capture import grab_frame

        area = desktop_rect()
        target = Target("area", area)

        def on_stream(stream, error):
            if error:
                callback(None, error)
                return
            grab_frame(stream, lambda surface, err: (stream.close(), callback(surface, err)))

        self.open_stream(target, cursor, False, on_stream)

    # --- windows & pointer ----------------------------------------------------

    def list_windows(self, exclude_pid):
        if not self.has_extension:
            return []
        try:
            (data,) = self._ext_call("GetWindows", GLib.Variant("(u)", (exclude_pid,))).unpack()
        except GLib.Error as e:
            print("scopecam: GetWindows failed:", e.message)
            return []
        return [WindowInfo(w["id"], w["title"], w.get("app", ""), Rect(w["x"], w["y"], w["w"], w["h"]))
                for w in json.loads(data)]

    def pointer(self, window_id, callback):
        def done(bus, result):
            try:
                x, y, _mods, rect = bus.call_finish(result).unpack()
            except GLib.Error:
                callback(None, None, None)
                return
            callback(x, y, Rect(*rect) if window_id and rect[2] > 0 else None)

        self.bus.call(EXT, EXT_PATH, EXT, "GetPointer", GLib.Variant("(t)", (window_id,)), None,
                      Gio.DBusCallFlags.NONE, 500, None, done)

    def set_indicator(self, active):
        if not self.has_extension:
            return
        try:
            self._ext_call("SetRecording", GLib.Variant("(b)", (active,)))
        except GLib.Error as e:
            print("scopecam: SetRecording failed:", e.message)

    def connect_stop_requested(self, callback):
        self._stop_callbacks.append(callback)
        if self._stop_sub is None:
            self._stop_sub = self.bus.signal_subscribe(
                EXT, EXT, "StopRequested", EXT_PATH, None, Gio.DBusSignalFlags.NONE,
                lambda *a: [cb() for cb in self._stop_callbacks])

    # --- streams --------------------------------------------------------------

    def open_stream(self, target, cursor, recording, callback, on_closed=None):
        if target.kind.startswith("pick-"):
            # Without the extension we cannot list windows: let the portal dialog pick.
            from .portal import PortalBackend

            return PortalBackend(self.bus, self.config).open_stream(target, cursor, recording, callback, on_closed)
        bus = self.bus
        subs = []
        state = {"closed": False, "session": None}

        def call(path, iface, method, args, reply=None):
            return bus.call_sync(MUTTER, path, iface, method, args,
                                 GLib.VariantType(reply) if reply else None,
                                 Gio.DBusCallFlags.NONE, 5000, None)

        def close():
            if state["closed"]:
                return
            state["closed"] = True
            for s in subs:
                bus.signal_unsubscribe(s)
            if state["session"]:
                try:
                    call(state["session"], MUTTER + ".Session", "Stop", None)
                except GLib.Error:
                    pass  # already gone

        try:
            props = {"is-recording": GLib.Variant("b", recording)}
            (session,) = call(MUTTER_PATH, MUTTER, "CreateSession", GLib.Variant("(a{sv})", (props,)), "(o)").unpack()
            state["session"] = session
            rprops = {"cursor-mode": GLib.Variant("u", cursor)}
            if target.kind == "window":
                rprops["window-id"] = GLib.Variant("t", target.window_id)
                (stream_path,) = call(session, MUTTER + ".Session", "RecordWindow",
                                      GLib.Variant("(a{sv})", (rprops,)), "(o)").unpack()
            elif target.kind == "monitor":
                (stream_path,) = call(session, MUTTER + ".Session", "RecordMonitor",
                                      GLib.Variant("(sa{sv})", (target.connector, rprops)), "(o)").unpack()
            else:
                r = target.rect
                (stream_path,) = call(session, MUTTER + ".Session", "RecordArea",
                                      GLib.Variant("(iiiia{sv})", (r.x, r.y, r.w, r.h, rprops)), "(o)").unpack()
        except GLib.Error as e:
            close()
            callback(None, f"Could not start screen capture: {e.message}")
            return

        def on_node(_bus, _sender, _path, _iface, _signal, params):
            (node_id,) = params.unpack()
            callback(Stream(node_id, None, target.rect, None, close), None)

        def on_session_closed(*_):
            was_closed = state["closed"]
            close()
            if not was_closed and on_closed:
                on_closed()

        subs.append(bus.signal_subscribe(MUTTER, MUTTER + ".Stream", "PipeWireStreamAdded", stream_path, None,
                                         Gio.DBusSignalFlags.NONE, on_node))
        subs.append(bus.signal_subscribe(MUTTER, MUTTER + ".Session", "Closed", session, None,
                                         Gio.DBusSignalFlags.NONE, on_session_closed))
        try:
            call(session, MUTTER + ".Session", "Start", None)
        except GLib.Error as e:
            close()
            callback(None, f"Could not start screen capture: {e.message}")
