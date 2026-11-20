"""Brief white flash after a screenshot (like GNOME's own tool).

It also gives Scopecam keyboard focus for a moment. Wayland only lets the focused app
put something on the clipboard, and screenshots taken without a selection overlay
(shortcut, single screen) would otherwise have no window at all.
"""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

CSS = b".scopecam-flash { background: white; }"
FOCUS_TIMEOUT_MS = 1000
FADE_US = 250_000
START_OPACITY = 0.6


class Flash(Gtk.Window):
    """on_focus(focused: bool) is called once, while the window is still mapped."""

    def __init__(self, app, monitor, on_focus, startup_id=None):
        super().__init__(application=app, decorated=False, title="Scopecam")
        self._on_focus = on_focus
        self._fired = False
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.add_css_class("scopecam-flash")
        self.set_opacity(START_OPACITY)
        self.set_can_target(False)
        if monitor is not None and monitor.gdk is not None:
            self.fullscreen_on_monitor(monitor.gdk)
        else:
            self.fullscreen()
        if startup_id:
            self.set_startup_id(startup_id)
        self.connect("notify::is-active", lambda *_: self.is_active() and self._fire(True))
        self._timeout = GLib.timeout_add(FOCUS_TIMEOUT_MS, lambda: self._fire(False) and False)
        self.present()

    def _fire(self, focused):
        if self._fired:
            return
        self._fired = True
        if self._timeout:
            GLib.source_remove(self._timeout)
            self._timeout = 0
        self._on_focus(focused)
        start = None

        def fade(_widget, clock):
            nonlocal start
            now = clock.get_frame_time()
            start = start or now
            progress = (now - start) / FADE_US
            if progress >= 1:
                self.destroy()
                return GLib.SOURCE_REMOVE
            self.set_opacity(START_OPACITY * (1 - progress))
            return GLib.SOURCE_CONTINUE

        self.add_tick_callback(fade)
        # The fade runs on frame ticks; if the window never got mapped there are none.
        GLib.timeout_add(FADE_US // 1000 + 500, lambda: self.destroy() or False)
