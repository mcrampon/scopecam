"""Control panel shown at the bottom of the capture overlay (GNOME-style).

Screenshot/screencast, region/screen/window, the shutter button, and the options,
collapsed under "Options" by default.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from .recorder import available_encodings  # noqa: E402

CSS = b"""
.scopecam-panel {
  background: @window_bg_color; color: @window_fg_color;
  border-radius: 24px; padding: 12px 14px; box-shadow: 0 2px 12px alpha(black, 0.4);
}
.scopecam-shutter { min-width: 56px; min-height: 56px; border-radius: 999px; padding: 0; }
.scopecam-shutter image { -gtk-icon-size: 24px; }
.scopecam-panel expander-widget > title { padding: 4px 0; }
"""


class ControlPanel(Gtk.Box):
    def __init__(self, selector):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10, width_request=520,
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=40)
        self.selector = selector
        self.app = selector.app
        cfg = self.app.config
        self.add_css_class("scopecam-panel")
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._loading = True

        # Top row: kind, mode, menu, close. Not focusable, so Enter always means "capture".
        top = Gtk.Box(spacing=10, can_focus=False)
        self.kind = Adw.ToggleGroup()
        self.kind.add(Adw.Toggle(name="screenshot", icon_name="camera-photo-symbolic", tooltip="Screenshot"))
        self.kind.add(Adw.Toggle(name="screencast", icon_name="camera-video-symbolic", tooltip="Screencast"))
        self.kind.set_active_name(selector.kind)
        self.kind.connect("notify::active-name", self._on_kind)
        top.append(self.kind)

        self.mode = Adw.ToggleGroup(homogeneous=True, hexpand=True)
        self.mode.add(Adw.Toggle(name="region", label="Region", icon_name="selection-mode-symbolic",
                                 tooltip="Region (R)"))
        self.mode.add(Adw.Toggle(name="screen", label="Screen", icon_name="video-display-symbolic",
                                 tooltip="Screen (S)"))
        self.mode.add(Adw.Toggle(name="window", label="Window", icon_name="focus-windows-symbolic",
                                 tooltip="Window (W)"))
        self.mode.set_active_name(selector.mode)
        self.mode.connect("notify::active-name", self._on_mode)
        top.append(self.mode)

        menu = Gio.Menu()
        menu.append("Preferences", "app.preferences")
        menu.append("Open Screenshots Folder", "app.open-folder::screenshot")
        menu.append("Open Screencasts Folder", "app.open-folder::screencast")
        menu.append("About Scopecam", "app.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu")
        menu_button.add_css_class("flat")
        menu_button.add_css_class("circular")
        top.append(menu_button)
        close = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Cancel (Esc)")
        close.add_css_class("flat")
        close.add_css_class("circular")
        close.connect("clicked", lambda *_: selector.cancel())
        top.append(close)
        self.append(top)

        self.shutter = Gtk.Button(halign=Gtk.Align.CENTER, can_focus=False)
        self.shutter.add_css_class("scopecam-shutter")
        self.shutter.connect("clicked", lambda *_: selector.confirm())
        self.append(self.shutter)

        # Options, collapsed by default.
        group = Adw.PreferencesGroup()
        self.shot_pointer = self._switch(group, "Show pointer", cfg.screenshot_pointer, tip="P")
        self.cast_pointer = self._switch(group, "Record pointer", cfg.screencast_pointer, tip="P")
        self.clicks = self._switch(group, "Highlight clicks", cfg.show_clicks)
        self.keys = self._switch(group, "Show typed keys", cfg.show_keys)
        self.desktop_audio = self._switch(group, "Record desktop audio", cfg.record_desktop_audio)
        self.mic = self._switch(group, "Record microphone", cfg.record_microphone)
        self.encodings = available_encodings()
        self.encoding = Adw.ComboRow(title="Encoding",
                                     model=Gtk.StringList.new([e.label for e in self.encodings.values()]))
        keys = list(self.encodings)
        if cfg.encoding in keys:
            self.encoding.set_selected(keys.index(cfg.encoding))
        self.encoding.connect("notify::selected", self._on_option)
        group.add(self.encoding)
        self.delay = Adw.SpinRow.new_with_range(0, 60, 1)
        self.delay.set_title("Delay")
        self.delay.set_subtitle("Seconds to wait after confirming")
        self.delay.set_value(cfg.delay)
        self.delay.connect("notify::value", self._on_option)
        group.add(self.delay)
        self.options = Gtk.Expander(label="Options", expanded=False)
        group.set_margin_top(8)
        self.options.set_child(group)
        self.append(self.options)
        self._loading = False

    def _switch(self, group, title, active, tip=None):
        row = Adw.SwitchRow(title=title, active=active)
        if tip:
            row.set_tooltip_text(f"Shortcut: {tip}")
        row.connect("notify::active", self._on_option)
        group.add(row)
        return row

    # --- events -----------------------------------------------------------------------

    def set_mode(self, mode):
        self.mode.set_active_name(mode)

    def toggle_pointer(self):
        row = self.shot_pointer if self.selector.kind == "screenshot" else self.cast_pointer
        row.set_active(not row.get_active())

    def _on_kind(self, *_):
        self.selector.set_kind(self.kind.get_active_name())

    def _on_mode(self, *_):
        mode = self.mode.get_active_name()
        cfg = self.app.config
        cfg.mode = mode  # remembered for next time
        cfg.save()
        self.selector.set_mode(mode)

    def _on_option(self, *_):
        if self._loading:
            return
        cfg = self.app.config
        cfg.screenshot_pointer = self.shot_pointer.get_active()
        cfg.screencast_pointer = self.cast_pointer.get_active()
        cfg.show_clicks = self.clicks.get_active()
        cfg.show_keys = self.keys.get_active()
        cfg.record_desktop_audio = self.desktop_audio.get_active()
        cfg.record_microphone = self.mic.get_active()
        if self.encodings:
            cfg.encoding = list(self.encodings)[self.encoding.get_selected()]
        cfg.delay = int(self.delay.get_value())
        cfg.save()
        self.refresh()
        self.selector.redraw()  # the pointer preview may have changed

    def refresh(self):
        cfg = self.app.config
        cast = self.selector.kind == "screencast"
        self.shot_pointer.set_visible(not cast)
        for row in (self.cast_pointer, self.clicks, self.keys, self.desktop_audio, self.mic, self.encoding):
            row.set_visible(cast)
        can_click = self.app.backend.has_pointer
        self.clicks.set_sensitive(cfg.screencast_pointer and can_click)
        self.clicks.set_subtitle("" if can_click else "Needs the Scopecam GNOME Shell extension")
        enc = self.encodings.get(cfg.encoding)
        self.encoding.set_subtitle(enc.hint if enc else "")
        self.shutter.set_icon_name("media-record-symbolic" if cast else "camera-photo-symbolic")
        self.shutter.set_tooltip_text("Record (Enter)" if cast else "Capture (Enter) · Copy (Ctrl+C)")
        for cls, on in (("suggested-action", not cast), ("destructive-action", cast)):
            (self.shutter.add_css_class if on else self.shutter.remove_css_class)(cls)
