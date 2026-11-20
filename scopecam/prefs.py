"""Preferences dialog: where and how captures are saved."""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .config import default_screencast_dir, default_screenshot_dir  # noqa: E402

FRAMERATES = [24, 30, 60]
KEY_MODES = [("all", "All keys"), ("shortcuts", "Shortcuts only")]


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, app):
        super().__init__(title="Preferences")
        self.app = app
        cfg = app.config
        page = Adw.PreferencesPage()

        shots = Adw.PreferencesGroup(title="Screenshots")
        shots.add(self._folder_row("screenshot_dir", default_screenshot_dir))
        shots.add(self._name_row("screenshot_name", ".png"))
        clip = Adw.SwitchRow(title="Copy to clipboard", active=cfg.copy_to_clipboard)
        clip.connect("notify::active", lambda r, _p: self._set("copy_to_clipboard", r.get_active()))
        shots.add(clip)
        page.add(shots)

        casts = Adw.PreferencesGroup(title="Screencasts")
        casts.add(self._folder_row("screencast_dir", default_screencast_dir))
        casts.add(self._name_row("screencast_name", None))
        casts.add(self._combo("Frame rate", [f"{f} fps" for f in FRAMERATES], FRAMERATES, "framerate"))
        casts.add(self._combo("Typed keys", [label for _k, label in KEY_MODES], [k for k, _l in KEY_MODES],
                              "keys_mode", "“Shortcuts only” never shows plain typing, e.g. passwords"))
        page.add(casts)

        about = Adw.PreferencesGroup(title="Integration")
        backend = app.backend
        row = Adw.ActionRow(title="Capture backend")
        if backend.name == "gnome":
            row.set_subtitle("GNOME (Mutter)" + ("  ·  Shell extension active" if backend.has_extension
                                                  else "  ·  Shell extension not running: window picking uses the "
                                                       "system dialog, click highlights are unavailable"))
        else:
            row.set_subtitle("xdg-desktop-portal")
        about.add(row)
        page.add(about)
        self.add(page)

    def _set(self, key, value):
        setattr(self.app.config, key, value)
        self.app.config.save()

    def _folder_row(self, key, default):
        row = Adw.ActionRow(title="Folder", subtitle=getattr(self.app.config, key))
        row.set_subtitle_selectable(True)
        reset = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Use Default")
        reset.add_css_class("flat")
        pick = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Choose Folder")
        pick.add_css_class("flat")

        def chosen(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
            except GLib.Error:
                return
            if folder and folder.get_path():
                self._set(key, folder.get_path())
                row.set_subtitle(folder.get_path())

        def pick_folder(*_):
            dialog = Gtk.FileDialog(title="Choose Folder")
            current = Gio.File.new_for_path(getattr(self.app.config, key))
            if current.query_exists():
                dialog.set_initial_folder(current)
            dialog.select_folder(self.get_root(), None, chosen)

        def reset_folder(*_):
            self._set(key, default())
            row.set_subtitle(default())

        pick.connect("clicked", pick_folder)
        reset.connect("clicked", reset_folder)
        row.add_suffix(reset)
        row.add_suffix(pick)
        row.set_activatable_widget(pick)
        return row

    def _name_row(self, key, _ext):
        row = Adw.EntryRow(title="File name (strftime pattern)", text=getattr(self.app.config, key))
        row.set_show_apply_button(True)
        row.connect("apply", lambda r: self._set(key, r.get_text().strip() or getattr(type(self.app.config)(), key)))
        return row

    def _combo(self, title, labels, values, key, subtitle=None):
        row = Adw.ComboRow(title=title, model=Gtk.StringList.new(labels))
        if subtitle:
            row.set_subtitle(subtitle)
        current = getattr(self.app.config, key)
        if current in values:
            row.set_selected(values.index(current))
        row.connect("notify::selected", lambda r, _p: self._set(key, values[r.get_selected()]))
        return row
