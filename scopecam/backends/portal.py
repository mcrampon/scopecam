"""Generic backend built on xdg-desktop-portal (Screenshot + ScreenCast).

Works on any desktop with a portal implementation (KDE, wlroots, GNOME...). The system
shows its own source picker, and we cannot know where the pointer is, so click
highlighting is unavailable here.
"""

import secrets
from urllib.parse import unquote, urlparse

import cairo
from gi.repository import Gio, GLib

from ..geometry import Rect, desktop_rect, monitors
from .base import Backend, Stream

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
REQUEST = "org.freedesktop.portal.Request"

SOURCE_MONITOR, SOURCE_WINDOW = 1, 2
PORTAL_CURSOR = {0: 1, 1: 2}  # our modes -> portal bitmask (hidden, embedded)


class PortalBackend(Backend):
    name = "portal"

    def __init__(self, bus, config=None):
        self.bus = bus
        self.config = config
        sender = bus.get_unique_name()[1:].replace(".", "_")
        self._request_prefix = f"/org/freedesktop/portal/desktop/request/{sender}/"

    def _request(self, iface, method, make_args, callback):
        """Call a portal method that answers through a Request object's Response signal."""
        token = "scopecam_" + secrets.token_hex(6)
        path = self._request_prefix + token
        sub = None

        def on_response(_bus, _sender, _path, _iface, _signal, params):
            self.bus.signal_unsubscribe(sub)
            code, results = params.unpack()
            callback(code, results)

        sub = self.bus.signal_subscribe(PORTAL, REQUEST, "Response", path, None,
                                        Gio.DBusSignalFlags.NONE,
                                        on_response)

        def done(bus, result):
            try:
                bus.call_finish(result)
            except GLib.Error as e:
                self.bus.signal_unsubscribe(sub)
                callback(-1, {"error": e.message})

        self.bus.call(PORTAL, PORTAL_PATH, iface, method, make_args(token), None,
                      Gio.DBusCallFlags.NONE, -1, None, done)

    # --- screenshots ----------------------------------------------------------------

    def freeze(self, cursor, callback):
        def args(token):
            opts = {"handle_token": GLib.Variant("s", token), "interactive": GLib.Variant("b", False)}
            return GLib.Variant("(sa{sv})", ("", opts))

        def done(code, results):
            if code != 0:
                callback(None, results.get("error") or "Screenshot was cancelled or not allowed")
                return
            path = unquote(urlparse(results["uri"]).path)
            try:
                surface = cairo.ImageSurface.create_from_png(path)
            except Exception as e:  # noqa: BLE001
                callback(None, f"Could not read screenshot: {e}")
                return
            # The portal saves a file of its own; we only wanted the pixels.
            Gio.File.new_for_path(path).delete_async(GLib.PRIORITY_LOW, None, None)
            callback(surface, None)

        self._request("org.freedesktop.portal.Screenshot", "Screenshot", args, done)

    # --- streams ----------------------------------------------------------------------

    def open_stream(self, target, cursor, recording, callback, on_closed=None):
        iface = "org.freedesktop.portal.ScreenCast"
        state = {"session": None, "closed": False, "sub": None}

        def close():
            if state["closed"]:
                return
            state["closed"] = True
            if state["sub"]:
                self.bus.signal_unsubscribe(state["sub"])
            if state["session"]:
                self.bus.call(PORTAL, state["session"], "org.freedesktop.portal.Session", "Close",
                              None, None, Gio.DBusCallFlags.NONE, -1, None, None)

        def fail(msg):
            close()
            callback(None, msg)

        def create_args(token):
            return GLib.Variant("(a{sv})", ({
                "handle_token": GLib.Variant("s", token),
                "session_handle_token": GLib.Variant("s", "scopecam_" + secrets.token_hex(6)),
            },))

        def on_created(code, results):
            if code != 0:
                return fail(results.get("error") or "Screen capture was refused")
            state["session"] = results["session_handle"]
            state["sub"] = self.bus.signal_subscribe(
                PORTAL, "org.freedesktop.portal.Session", "Closed", state["session"], None,
                Gio.DBusSignalFlags.NONE, lambda *a: (not state["closed"]) and (close(), on_closed and on_closed()))
            self._request(iface, "SelectSources", select_args, on_selected)

        def select_args(token):
            window = target.kind in ("window", "pick-window")
            opts = {
                "handle_token": GLib.Variant("s", token),
                "types": GLib.Variant("u", SOURCE_WINDOW if window else SOURCE_MONITOR),
                "multiple": GLib.Variant("b", False),
                "cursor_mode": GLib.Variant("u", PORTAL_CURSOR[cursor]),
            }
            if not window and self.config is not None:
                # Remember the chosen monitor so the dialog is only shown once.
                opts["persist_mode"] = GLib.Variant("u", 2)
                if self.config.portal_restore_token:
                    opts["restore_token"] = GLib.Variant("s", self.config.portal_restore_token)
            return GLib.Variant("(oa{sv})", (state["session"], opts))

        def on_selected(code, results):
            if code != 0:
                return fail(results.get("error") or "No source selected")
            self._request(iface, "Start", lambda token: GLib.Variant(
                "(osa{sv})", (state["session"], "", {"handle_token": GLib.Variant("s", token)})), on_started)

        def on_started(code, results):
            if code != 0:
                return fail(results.get("error") or "Screen capture was cancelled")
            if self.config is not None and results.get("restore_token"):
                self.config.portal_restore_token = results["restore_token"]
                self.config.save()
            streams = results.get("streams") or []
            if not streams:
                return fail("No stream returned by the portal")
            node_id, props = streams[0]
            source_rect = None
            if "position" in props and "size" in props:
                source_rect = Rect(*props["position"], *props["size"])
            elif "size" in props:
                source_rect = Rect(0, 0, *props["size"])
            try:
                res, fds = self.bus.call_with_unix_fd_list_sync(
                    PORTAL, PORTAL_PATH, iface, "OpenPipeWireRemote",
                    GLib.Variant("(oa{sv})", (state["session"], {})), GLib.VariantType("(h)"),
                    Gio.DBusCallFlags.NONE, -1, None, None)
                fd = fds.get(res.unpack()[0])
            except GLib.Error as e:
                return fail(f"Could not open PipeWire remote: {e.message}")
            crop = None
            if target.kind == "area" and target.rect:
                src = source_rect or _monitor_containing(target.rect)
                inside = src and target.rect.intersect(src)
                if inside:
                    crop = Rect(inside.x - src.x, inside.y - src.y, inside.w, inside.h)
                    source_rect = src
            callback(Stream(node_id, fd, source_rect, crop, close), None)

        self._request(iface, "CreateSession", create_args, on_created)


def _monitor_containing(rect):
    cx, cy = rect.x + rect.w / 2, rect.y + rect.h / 2
    for m in monitors():
        if m.rect.contains(cx, cy):
            return m.rect
    return desktop_rect()
