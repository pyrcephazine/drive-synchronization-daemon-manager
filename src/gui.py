from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from functools import lru_cache
import os
from pathlib import Path
import sys
import time

# Enforce this before importing GTK. On Wayland, use XWayland for WM decorations.
os.environ["GTK_CSD"] = "0"
os.environ["GDK_BACKEND"] = "x11"

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk

from .config import APP_NAME, Settings, SyncError, detect_legacy
from .badges import Badges
from .account_dialog import AccountDialog
from .accounts import cleanup_staging
from .rclone import require_rclone
from .engine import Runner, common_flags
from .icons import application_icon_path, icon_path
from .service import Manager
from .transfers import scan, format_bytes, format_time
from .transfer_view import TransfersView
from .conflict_view import ConflictsView
from .conflicts import scan as scan_conflicts, resolve as resolve_conflict, reported_snapshot



@lru_cache(maxsize=32)
def icon_pixbuf(path, size):
    # Render at the panel's requested size; do not repeatedly parse complex SVGs.
    return GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)


def file_icon(path):
    return Gio.FileIcon.new(Gio.File.new_for_path(path))


def label(text, wrap=False):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(wrap)
    if wrap:
        widget.set_max_width_chars(85)
    return widget


def muted(text):
    widget = label(text, True)
    widget.get_style_context().add_class("dim-label")
    return widget


def scroll(child):
    widget = Gtk.ScrolledWindow()
    widget.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    widget.add(child)
    return widget


def menu_item(text, callback):
    item = Gtk.MenuItem.new_with_mnemonic(text)
    item.connect("activate", lambda _item: callback())
    return item


class Tray:
    def __init__(self, app):
        self.app = app
        self.menu = Gtk.Menu()
        self.status_item = Gtk.MenuItem(label=APP_NAME)
        self.status_item.set_sensitive(False)
        self.menu.append(self.status_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.menu.append(menu_item("_Show window", app.present))
        self.menu.append(menu_item("_Open local folder", app.open_folder))
        self.sync_item = menu_item("_Sync now", app.sync_now)
        self.menu.append(self.sync_item)
        self.pause_item = menu_item("_Pause automatic sync", app.pause)
        self.menu.append(self.pause_item)
        self.resume_item = menu_item("_Resume automatic sync", app.resume)
        self.menu.append(self.resume_item)
        self.menu.append(menu_item("_Settings…", app.settings))
        self.menu.append(menu_item("View _activity", app.show_activity))
        self.menu.append(Gtk.SeparatorMenuItem())
        self.menu.append(menu_item("_Quit app", app.quit))
        self.menu.show_all()
        self.indicator = None
        self.status_icon = None
        self.current_icon = icon_path("setup")
        # MATE's traditional Notification Area speaks XEmbed directly.
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
        if "MATE" not in desktop:
            try:
                gi.require_version("AyatanaAppIndicator3", "0.1")
                from gi.repository import AyatanaAppIndicator3 as Indicator
                self.indicator = Indicator.Indicator.new(
                    "rclone-local-sync", icon_path("setup"), Indicator.IndicatorCategory.APPLICATION_STATUS)
                self.indicator.set_menu(self.menu)
                self.indicator.set_status(Indicator.IndicatorStatus.ACTIVE)
                self.indicator.set_title(APP_NAME)
            except (ValueError, ImportError):
                pass
        if self.indicator is None:
            self.status_icon = Gtk.StatusIcon()
            self.status_icon.connect("size-changed", self.resize_icon)
            self.resize_icon(self.status_icon, self.status_icon.get_size() or 24)
            self.status_icon.set_title(APP_NAME)
            self.status_icon.set_name("rclone-local-sync")
            self.status_icon.set_visible(True)
            self.status_icon.connect("activate", lambda _icon: app.present())
            self.status_icon.connect("popup-menu", self.popup)

    def resize_icon(self, icon, size):
        icon.set_from_pixbuf(icon_pixbuf(self.current_icon, max(1, size)))
        return True

    def popup(self, icon, button, activate_time):
        self.menu.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, activate_time)

    def available(self):
        if self.status_icon:
            return self.status_icon.is_embedded()
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                  "NameHasOwner", GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
                                  GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
            return reply.unpack()[0]
        except GLib.Error:
            return False

    def update(self, status):
        description = APP_NAME + ": " + status["label"]
        self.status_item.set_label(status["label"])
        selected_icon = icon_path(status["phase"])
        changed = selected_icon != self.current_icon
        self.current_icon = selected_icon
        if self.status_icon:
            if changed:
                self.resize_icon(self.status_icon, self.status_icon.get_size() or 24)
            self.status_icon.set_tooltip_text(description)
        else:
            self.indicator.set_icon_full(selected_icon, description)
        self.sync_item.set_sensitive(self.app.configured and not status.get("running") and not status.get("previewing") and status["phase"] not in ("repair", "repairing"))
        self.pause_item.set_sensitive(self.app.configured and bool(status.get("scheduled")))
        self.resume_item.set_sensitive(self.app.configured and not status.get("scheduled") and status["phase"] not in ("repair", "repairing"))


# All numeric quantities have explicit units. No arbitrary rclone flags are accepted.
FIELDS = [
    ("Connection", [
        ("name", "Connection name", "text", None),
        ("local_dir", "Local folder", "folder", None),
        ("remote", "Remote folder", "remote", None),
        ("rclone_config", "rclone configuration file", "file", None),
    ]),
    ("Schedule", [
        ("schedule_enabled", "Sync automatically", "bool", None),
        ("interval_seconds", "Check for changes every (seconds)", (30, 604800), None),
        ("startup_delay_seconds", "Wait after login (seconds)", (5, 86400), None),
        ("watch_local", "Check after local changes", "bool", None),
        ("watch_debounce_seconds", "Wait after local changes (seconds)", (1, 60), None),
        ("full_scan_interval_seconds", "Full comparison every (seconds)", (300, 604800), None),
        ("start_at_login", "Start automatic sync at login", "bool", None),
    ]),
    ("Files", [
        ("max_size_bytes", "Maximum file size (GB)", "gb", "Use 0 for no limit. Skipped files are kept."),
        ("excludes", "Files and folders to skip", "multiline", "One pattern per line, such as *.tmp or /node_modules/**."),
        ("google_docs", "Google Docs, Sheets, and Slides", [("url", "Browser shortcuts (.url)"), ("skip", "Skip Google documents")]),
        ("create_empty_dirs", "Sync empty folders", "bool", None),
        ("track_renames", "Track renamed files", "bool", None),
    ]),
    ("Safety", [
        ("backup_dir", "Recovery folder", "folder", "When remote changes replace or delete local files, save the original files here. Backups are kept until you delete them."),
        ("conflict_resolve", "When both versions change", [("newer", "Use newer version"), ("none", "Keep both versions"), ("path1", "Use local version"), ("path2", "Use remote version")], "Both versions are preserved. Renamed copies use numbered conflict filenames."),
        ("max_delete_percent", "Maximum deletions per sync (%)", (1, 100), "Stop the sync if planned deletions exceed this percentage. Renaming a large folder can also trigger this limit."),
        ("min_free_bytes", "Minimum free disk space (GB)", "gb", None),
        ("compare", "Compare files by", [("size,modtime", "Size and modification time"), ("size,modtime,checksum", "Size, modification time, and checksum")]),
    ]),
    ("Performance", [
        ("upload_kib", "Upload speed limit (KiB per second)", (0, 1000000000), "Use 0 for no limit."),
        ("download_kib", "Download speed limit (KiB per second)", (0, 1000000000), "Use 0 for no limit."),
        ("transfers", "Files to transfer at once", (1, 64), None),
        ("checkers", "Files to check at once", (1, 128), None),
        ("drive_chunk_mib", "Google Drive upload chunk size (MiB)", (1, 1024), "Use a power of 2, such as 16, 32, or 64."),
        ("fast_list", "Read folder lists in fewer requests", "bool", "Reduce requests to remote storage by reading more folders at once. This uses more memory."),
        ("cpu_nice", "CPU priority adjustment", (0, 19), "0 gives sync normal priority. Higher values give other apps more CPU time. 19 is the lowest sync priority."),
        ("io_priority", "Disk access priority", (0, 7), "0 is the highest priority. 7 gives other apps priority for disk access."),
    ]),
    ("Reliability", [
        ("retries", "Retry attempts", (1, 20), None),
        ("retry_delay_seconds", "Delay between retries (seconds)", (0, 3600), None),
        ("connect_timeout_seconds", "Connection timeout (seconds)", (5, 3600), None),
        ("io_timeout_seconds", "Timeout with no data transfer (seconds)", (30, 86400), None),
        ("log_level", "Activity log detail", [("INFO", "Standard"), ("NOTICE", "Important events only"), ("DEBUG", "Detailed troubleshooting")], "Detailed troubleshooting logs may include private filenames."),
    ]),
    ("Desktop", [
        ("tray_at_login", "Show tray icon at login", "bool", None),
        ("close_to_tray", "Keep tray icon after closing the window", "bool", None),
        ("notify_errors", "Notify when sync needs attention", "bool", None),
        ("notify_success", "Notify when sync finishes successfully", "bool", None),
    ]),
]


class SettingsDialog(Gtk.Dialog):
    def __init__(self, app, cfg, is_new=False):
        super().__init__(title="New connection" if is_new else "Settings: " + APP_NAME,
                         transient_for=app.window, modal=True, use_header_bar=False)
        self.set_default_size(760, 660)
        self.app, self.base, self.is_new = app, cfg, is_new
        self.widgets = {}
        self.saving = False
        self.expected = app.config_path.read_text() if app.config_path.exists() else None
        self.connect("delete-event", lambda *_: self.saving)
        self.connect("destroy", lambda *_: setattr(app, "settings_dialog", None))
        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.save_button = self.add_button("_Create connection" if is_new or (not app.configured and not cfg.adopted_legacy) else "_Apply settings", Gtk.ResponseType.APPLY)
        content = self.get_content_area()
        content.set_spacing(10)
        content.set_border_width(12)
        if cfg.initialized:
            content.pack_start(muted("To change folders or file filters, create a new connection."), False, False, 0)
        notebook = Gtk.Notebook()
        notebook.set_scrollable(True)
        content.pack_start(notebook, True, True, 0)
        locked = set(cfg.identity()) if cfg.initialized else set()
        for tab, fields in FIELDS:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
            box.set_border_width(16)
            for field in fields:
                key, title, kind = field[:3]
                help_text = field[3] if len(field) > 3 else None
                self.add_field(box, key, title, kind, help_text, key in locked)
            if tab == "Safety":
                box.pack_start(muted("Before syncing, the app checks folder access, duplicate names, and files that exceed the size limit. "
                                     "Google Drive moves deleted files to Trash. Other providers may delete files permanently."), False, False, 0)
                box.pack_start(muted("Sync history folder: keep this folder and its contents.\n" + cfg.state_dir), False, False, 0)
            if tab == "Files":
                box.pack_start(muted("Shortcuts open Google Docs, Sheets, and Slides in your browser. "
                                     "Deleting a synced shortcut can delete the original document from Google Drive."), False, False, 0)
            notebook.append_page(scroll(box), Gtk.Label(label=tab))
        self.connect("response", self.response)
        self.widgets["rclone_config"][0].connect("focus-out-event", lambda *_: self.load_remotes() or False)
        self.show_all()
        self.load_remotes()

    def add_field(self, box, key, title, kind, help_text, locked):
        group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        value = getattr(self.base, key)
        if kind == "bool":
            widget = Gtk.CheckButton(label=title)
            widget.set_active(value)
            group.pack_start(widget, False, False, 0)
        else:
            group.pack_start(label(title), False, False, 0)
            if isinstance(kind, list):
                widget = Gtk.ComboBoxText()
                for code, text in kind:
                    widget.append(code, text)
                widget.set_active_id(value)
            elif isinstance(kind, tuple) or kind == "gb":
                low, high = kind if isinstance(kind, tuple) else (0, 1000000)
                widget = Gtk.SpinButton.new_with_range(low, high, 1 if kind != "gb" else 0.1)
                widget.set_digits(3 if kind == "gb" else 0)
                widget.set_value(value / 1e9 if kind == "gb" else value)
                widget.set_halign(Gtk.Align.START)
                widget.set_numeric(True)
            elif kind == "multiline":
                widget = Gtk.TextView()
                widget.set_monospace(True)
                widget.get_buffer().set_text("\n".join(value))
                pane = scroll(widget)
                pane.set_min_content_height(110)
                group.pack_start(pane, False, False, 0)
            elif kind == "remote":
                widget = Gtk.ComboBoxText.new_with_entry()
                widget.get_child().set_text(value)
            else:
                widget = Gtk.Entry()
                widget.set_text(value)
                widget.set_hexpand(True)
            if kind in ("folder", "file", "remote"):
                row = Gtk.Box(spacing=8)
                row.pack_start(widget, True, True, 0)
                browse = Gtk.Button(label="Connect account…" if kind == "remote" else "Browse…")
                browse.set_sensitive(not locked)
                if kind == "remote":
                    browse.connect("clicked", lambda _button: self.connect_account())
                else:
                    browse.connect("clicked", lambda _button: self.browse(widget, kind == "folder"))
                row.pack_start(browse, False, False, 0)
                group.pack_start(row, False, False, 0)
            elif kind != "multiline":
                group.pack_start(widget, False, False, 0)
        widget.set_sensitive(not locked)
        if help_text:
            group.pack_start(muted(help_text), False, False, 0)
            widget.set_tooltip_text(help_text)
        self.widgets[key] = (widget, kind)
        box.pack_start(group, False, False, 0)

    def browse(self, entry, folder):
        dialog = Gtk.FileChooserDialog(title="Select a folder" if folder else "Select a file", transient_for=self,
                                       action=Gtk.FileChooserAction.SELECT_FOLDER if folder else Gtk.FileChooserAction.OPEN,
                                       use_header_bar=False)
        dialog.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Select", Gtk.ResponseType.OK)
        current = Path(entry.get_text()).expanduser()
        if current.exists():
            dialog.set_filename(str(current))
        if dialog.run() == Gtk.ResponseType.OK:
            entry.set_text(dialog.get_filename())
            if entry is self.widgets["rclone_config"][0]:
                self.load_remotes()
        dialog.destroy()

    def connect_account(self):
        if self.app.demo:
            self.app.message("Demo mode", "Account setup is available outside demo mode.", parent=self)
            return
        try:
            AccountDialog(self, self.collect())
        except (SyncError, OSError) as error:
            self.app.message("Could not connect account", str(error), error=True, parent=self)

    def load_remotes(self):
        if self.app.demo:
            return
        cfg = self.collect()
        def job():
            return Runner().call([cfg.rclone_binary, "listremotes", *common_flags(cfg)], timeout=15).splitlines()
        def done(remotes):
            if self.get_visible():
                widget = self.widgets["remote"][0]
                value = widget.get_child().get_text()
                widget.remove_all()
                for remote in remotes:
                    widget.append_text(remote)
                widget.get_child().set_text(value)
        self.app.background(job, done, report=False)

    def collect(self):
        data = asdict(self.base)
        for key, (widget, kind) in self.widgets.items():
            if not widget.get_sensitive():
                continue
            if kind == "bool":
                value = widget.get_active()
            elif kind == "gb":
                value = round(widget.get_value() * 1e9)
            elif isinstance(kind, tuple):
                value = widget.get_value_as_int()
            elif isinstance(kind, list):
                value = widget.get_active_id()
            elif kind == "multiline":
                buffer = widget.get_buffer()
                value = [line.strip() for line in buffer.get_text(*buffer.get_bounds(), True).splitlines() if line.strip()]
            elif kind == "remote":
                value = widget.get_child().get_text().strip()
            else:
                value = widget.get_text().strip()
                if kind in ("folder", "file"):
                    value = str(Path(value).expanduser())
            data[key] = value
        return Settings(**data)

    def response(self, _dialog, response):
        if self.saving:
            return
        if response != Gtk.ResponseType.APPLY:
            self.destroy()
            return
        if self.app.demo:
            self.app.message("Demo mode", "You can explore settings in this demo. Your files, saved settings, and sync schedule stay the same.")
            return
        try:
            cfg = self.collect()
            cfg.validate()
        except (SyncError, ValueError) as error:
            self.app.message("Check these settings", str(error), error=True, parent=self)
            return
        if not cfg.initialized and not (cfg.state / "setup-approved.json").exists():
            if not self.app.question("Create this sync connection?",
                                     f"Remote folder: {cfg.remote}\nLocal folder: {cfg.local_dir}\n\n"
                                     "Files download to the empty local folder. Edits and deletions then sync in both directions."
                                     + ("\nThis replaces the current connection. Its files and history are kept." if self.is_new and self.app.configured else ""), parent=self):
                return
        self.saving = True
        self.save_button.set_sensitive(False)
        self.set_response_sensitive(Gtk.ResponseType.CANCEL, False)
        def finished(result):
            self.app.cfg = result
            self.app.configured = True
            self.app.refresh_installation()
            self.destroy()
        def failed(error):
            self.saving = False
            self.save_button.set_sensitive(True)
            self.set_response_sensitive(Gtk.ResponseType.CANCEL, True)
            self.app.message("Could not save settings", str(error), error=True, parent=self)
        self.app.background(lambda: self.app.manager.apply(cfg, expected=self.expected), finished, on_error=failed)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME + (": Demo" if app.demo else ""))
        self.app = app
        self.set_default_size(900, 740)
        self.set_wmclass("DriveSynchronizationDaemonManager", "DriveSynchronizationDaemonManager")
        self.set_icon_from_file(application_icon_path())
        self.connect("delete-event", self.on_close)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)
        self.menu_bar = Gtk.MenuBar()
        for title, entries in [
            ("_File", [("_New connection…", app.new_connection), ("_Open local folder", app.open_folder),
                       ("_Settings…", app.settings), ("Review _repair…", app.review_repair),
                       ("Review clean_up…", app.review_cleanup), ("_Remove connection…", app.remove_connection), None,
                       ("_Close window", self.close), ("_Quit app", app.quit)]),
            ("_Sync", [("_Sync now", app.sync_now), ("_Preview changes", app.preview),
                       ("_Pause automatic sync", app.pause), ("_Resume automatic sync", app.resume), ("S_top sync or preview", app.stop_current),
                       ("Open _recovery folder", app.open_backups)]),
        ]:
            root = Gtk.MenuItem.new_with_mnemonic(title)
            menu = Gtk.Menu()
            for entry in entries:
                menu.append(Gtk.SeparatorMenuItem() if entry is None else menu_item(*entry))
            root.set_submenu(menu)
            self.menu_bar.append(root)
            if title == "_Sync":
                self.sync_items = menu.get_children()
        outer.pack_start(self.menu_bar, False, False, 0)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        body.set_border_width(24)
        outer.pack_start(body, True, True, 0)
        heading = Gtk.Box(spacing=16)
        self.phase_icon = Gtk.Image.new_from_gicon(file_icon(icon_path("setup")), Gtk.IconSize.DIALOG)
        self.phase_icon.set_pixel_size(64)
        self.current_icon = icon_path("setup")
        heading.pack_start(self.phase_icon, False, False, 0)
        self.title_label = label("Set up a sync connection")
        self.title_label.set_valign(Gtk.Align.CENTER)
        self.title_label.get_style_context().add_class("title")
        heading.pack_start(self.title_label, True, True, 0)
        self.repair_button = Gtk.Button(label="Review repair…")
        self.repair_button.set_no_show_all(True)
        self.repair_button.connect("clicked", lambda *_: app.review_repair())
        heading.pack_end(self.repair_button, False, False, 0)
        body.pack_start(heading, False, False, 0)
        self.notebook = Gtk.Notebook()
        body.pack_start(self.notebook, True, True, 0)
        overview = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        overview.set_border_width(20)
        grid = Gtk.Grid(column_spacing=20, row_spacing=15)
        self.values = {}
        for row, (key, title) in enumerate([("name", "Connection name"), ("local", "Local folder"), ("remote", "Remote folder"),
                                            ("cycle", "Check for changes"), ("checked", "Last check for changes"), ("last", "Last successful sync"),
                                            ("max_size", "Maximum file size"), ("direction", "Sync direction"),
                                            ("documents", "Google Docs, Sheets, and Slides"), ("excludes", "Files and folders to skip"),
                                            ("conflicts", "When both versions change")]):
            grid.attach(muted(title), 0, row, 1, 1)
            widget = Badges() if key in ("cycle", "direction") else label("-", True)
            if isinstance(widget, Gtk.Label):
                widget.set_selectable(True)
            widget.set_hexpand(True)
            self.values[key] = widget
            grid.attach(widget, 1, row, 1, 1)
        overview.pack_start(grid, False, False, 0)
        self.notice = label("", True)
        self.notice.set_selectable(True)
        overview.pack_start(self.notice, False, False, 0)
        self.notebook.append_page(scroll(overview), Gtk.Label(label="Overview"))
        self.transfers = TransfersView(app)
        self.notebook.append_page(self.transfers, Gtk.Label(label="Transfers"))
        self.conflicts = ConflictsView(app)
        self.notebook.append_page(self.conflicts, Gtk.Label(label="Conflicts"))
        activity = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        activity.set_border_width(12)
        self.preview_logs = Gtk.CheckButton(label="Show preview log")
        self.preview_logs.connect("toggled", lambda _button: app.refresh())
        activity.pack_start(self.preview_logs, False, False, 0)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        activity.pack_start(scroll(self.log_view), True, True, 0)
        self.notebook.append_page(activity, Gtk.Label(label="Activity"))
        self.notebook.connect("switch-page", lambda _book, _page, index: app.refresh_transfers() if index == 1 else app.refresh_conflicts() if index == 2 else None)
        actions = Gtk.Box(spacing=9)
        self.sync_button = Gtk.Button(label="Sync now")
        self.sync_button.get_style_context().add_class("suggested-action")
        self.sync_button.connect("clicked", lambda _button: app.sync_now())
        actions.pack_start(self.sync_button, False, False, 0)
        self.pause_button = Gtk.Button(label="Pause automatic sync")
        self.pause_button.connect("clicked", lambda _button: app.pause())
        actions.pack_start(self.pause_button, False, False, 0)
        self.resume_button = Gtk.Button(label="Resume automatic sync")
        self.resume_button.connect("clicked", lambda _button: app.resume())
        actions.pack_start(self.resume_button, False, False, 0)
        self.pause_button.set_tooltip_text("Pause automatic sync. Any sync already running continues until it finishes.")
        open_button = Gtk.Button(label="Open local folder")
        open_button.connect("clicked", lambda _button: app.open_folder())
        actions.pack_start(open_button, False, False, 0)
        settings_button = Gtk.Button(label="Settings…")
        settings_button.connect("clicked", lambda _button: app.settings())
        actions.pack_end(settings_button, False, False, 0)
        body.pack_start(actions, False, False, 0)
        self.connect("key-press-event", self.key_press)

    def key_press(self, _window, event):
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            key = Gdk.keyval_name(event.keyval).lower()
            action = {"r": self.app.sync_now, "o": self.app.open_folder, "comma": self.app.settings,
                      "w": self.close, "q": self.app.quit}.get(key)
            if action:
                action()
                return True
        return False

    def on_close(self, *_args):
        if self.app.cfg.close_to_tray and self.app.tray.available():
            self.hide()
        else:
            self.app.quit()
        return True

    def update(self, status, logs):
        cfg = self.app.cfg
        selected_icon = icon_path(status["phase"])
        if selected_icon != self.current_icon:
            self.phase_icon.set_from_gicon(file_icon(selected_icon), Gtk.IconSize.DIALOG)
            self.current_icon = selected_icon
        self.title_label.set_markup('<span size="x-large" weight="bold">' + GLib.markup_escape_text(status["label"]) + "</span>")
        self.values["name"].set_text(cfg.name)
        self.values["local"].set_text(cfg.local_dir)
        self.values["remote"].set_text(cfg.remote or "Not selected")
        amount, unit = (cfg.interval_seconds // 60, "minute") if cfg.interval_seconds % 60 == 0 else (cfg.interval_seconds, "second")
        interval = f"{amount} {unit}{'' if amount == 1 else 's'}"
        checks = [("schedule", f"Once every {interval}")]
        if cfg.watch_local:
            checks.append(("local-change", "After local changes"))
        self.values["cycle"].set_items(checks if status.get("scheduled") else [("paused", "Paused")])
        self.values["checked"].set_text(format_time(status.get("detail", {}).get("finished")))
        self.values["last"].set_text(status.get("last_success") or "No successful sync yet")
        self.values["max_size"].set_text(format_bytes(cfg.max_size_bytes) if cfg.max_size_bytes else "No size limit")
        self.values["direction"].set_items([("upload", "Local to remote"), ("download", "Remote to local")])
        self.values["documents"].set_text("Browser shortcuts (.url)" if cfg.google_docs == "url" else "Skipped")
        self.values["excludes"].set_text(", ".join(cfg.excludes) if cfg.excludes else "None")
        self.values["conflicts"].set_text({"newer": "Use newer version", "none": "Keep both versions", "path1": "Use local version", "path2": "Use remote version"}[cfg.conflict_resolve])
        if self.app.demo:
            notice = "Demo mode. Your files and sync settings stay the same."
        elif status["phase"] == "unconfigured":
            notice = "An existing Google Drive connection is ready to import. Open Settings and select Apply settings." if cfg.adopted_legacy else "Open Settings to choose a local folder and a remote folder, then create your connection."
        elif status["phase"] in ("repair", "repairing"):
            health = status.get("installation", {})
            notice = health.get("error") or "\n".join(dict.fromkeys(item["message"] for item in health.get("issues", [])))
        elif status["phase"] == "error":
            notice = status.get("detail", {}).get("message") or "Open Activity for details."
        elif not cfg.initialized:
            notice = "Wait for the first sync to finish before editing files."
        else:
            notice = ""
        self.notice.set_text(notice)
        repair = status["phase"] in ("repair", "repairing")
        self.repair_button.set_visible(repair)
        self.repair_button.set_sensitive(not self.app.repair_running)
        health = status.get("installation", {})
        self.repair_button.set_label("Retry repair" if health.get("state") == "repairable" else "Review repair…")
        busy = bool(status.get("running") or status.get("previewing") or repair)
        self.sync_button.set_sensitive(self.app.configured and not busy)
        self.pause_button.set_sensitive(self.app.configured and bool(status.get("scheduled")))
        self.resume_button.set_sensitive(self.app.configured and not status.get("scheduled") and not repair)
        for item, enabled in zip(self.sync_items, (not busy, not busy, bool(status.get("scheduled")),
                                                  not status.get("scheduled") and not repair, bool(status.get("running") or status.get("previewing")), True)):
            item.set_sensitive(self.app.configured and enabled)
        buffer = self.log_view.get_buffer()
        if buffer.get_text(*buffer.get_bounds(), True) != logs:
            buffer.set_text(logs)
            self.log_view.scroll_to_iter(buffer.get_end_iter(), 0.0, False, 0, 1)


class Application(Gtk.Application):
    def __init__(self, config_path, launcher, tray=False, demo=False):
        super().__init__(application_id="io.github.localdrivesync.App" + (".Demo" if demo else ""))
        self.manager = Manager(config_path, launcher)
        self.config_path = Path(config_path)
        self.demo, self.start_in_tray = demo, tray
        self.configured = self.config_path.exists() and not demo
        self.load_error = None
        try:
            self.cfg = Settings.load(self.config_path) if self.configured else (detect_legacy() or Settings())
        except SyncError as error:
            self.cfg, self.load_error, self.configured = Settings(), str(error), False
        self.window, self.tray = None, None
        self.settings_dialog, self.account_dialog = None, None
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="localdrive-ui")
        self.refresh_pending = False
        self.last_status = {}
        self.last_notification = None
        self.first = True
        self.transfer_snapshot = None
        self.transfer_pending = False
        self.transfer_checked = 0.0
        self.transfer_error = None
        self.transfer_runner = None
        self.last_transfer_cycle = None
        self.conflict_snapshot = None
        self.conflict_pending = False
        self.conflict_checked = 0.0
        self.conflict_runner = None
        self.conflict_error = None
        self.last_conflict_report = None
        self.closing = False
        self.repair_running = False
        self.health_checked = 0.0
        self.health = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        if not self.demo:
            cleanup_staging()
        Gtk.Window.set_default_icon_from_file(application_icon_path())
        settings = Gtk.Settings.get_default()
        settings.set_property("gtk-dialogs-use-header", False)
        settings.set_property("gtk-shell-shows-menubar", False)
        self.hold()

    def do_activate(self):
        if self.window is None:
            self.window = MainWindow(self)
            self.tray = Tray(self)
            self.window.show_all()
            if self.start_in_tray and self.configured:
                self.window.hide()
                GLib.timeout_add_seconds(3, self.check_tray)
            self.refresh()
            GLib.timeout_add_seconds(3, self.refresh)
            if self.load_error:
                self.message("Cannot load settings", self.load_error, error=True)
        else:
            self.present()

    def do_shutdown(self):
        self.closing = True
        if self.account_dialog:
            self.account_dialog.cleanup()
        if self.transfer_runner:
            self.transfer_runner.interrupt()
        if self.conflict_runner:
            self.conflict_runner.interrupt()
        self.pool.shutdown(wait=False, cancel_futures=True)
        Gtk.Application.do_shutdown(self)

    def check_tray(self):
        if not self.tray.available():
            self.present()
            self.message("Tray icon unavailable", "To show the tray icon, add the Notification Area applet to your MATE panel "
                         "or turn on indicator support in your desktop settings. Sync works without the tray icon.")
        return False

    def background(self, work, success=None, report=True, on_error=None):
        future = self.pool.submit(work)
        def completed(future):
            if self.closing:
                return
            try:
                result = future.result()
            except Exception as error:
                def failure(error=error):
                    if on_error:
                        on_error(error)
                    elif report:
                        self.message(APP_NAME, str(error), error=True)
                    return False
                GLib.idle_add(failure)
            else:
                if success:
                    def finish():
                        success(result)
                        return False
                    GLib.idle_add(finish)
        future.add_done_callback(completed)

    def refresh(self):
        if self.refresh_pending or self.window is None:
            return True
        self.refresh_pending = True
        preview = self.window.preview_logs.get_active()
        def work():
            if self.demo:
                return ({"phase": "ready", "label": "Automatic sync is on", "scheduled": True,
                         "last_success": "Just now (demo)", "running": False, "detail": {}},
                        "Sample activity for demo mode. Your files and sync settings stay the same.\n\n"
                        "Safety checks passed: folder access verified, health markers match, and no file size conflicts found.\n"
                        "Sync finished successfully.\n")
            if time.monotonic() - self.health_checked >= 30 or self.health is None:
                try:
                    self.health = self.manager.maintain()
                except SyncError:
                    self.health = self.manager.inspect_installation()
                self.health_checked = time.monotonic()
            health = self.health
            if health["state"] == "removed":
                self.configured = False
                self.cfg = Settings(rclone_binary=self.cfg.rclone_binary, rclone_config=self.cfg.rclone_config)
            if not self.config_path.exists() or health["state"] == "removed":
                repair = health["state"] not in ("unconfigured", "removed", "healthy")
                return ({"phase": "repair" if repair else "unconfigured",
                         "label": "Sync needs repair" if repair else "Set up a sync connection",
                         "scheduled": False, "installation": health}, "")
            if health["state"] in ("healthy", "repairable"):
                self.cfg = Settings.load(self.config_path)
                self.configured = True
            return self.manager.status(self.cfg, health=health), self.manager.logs(self.cfg, preview)
        def done(result):
            self.refresh_pending = False
            status, logs = result
            self.window.update(status, logs)
            self.tray.update(status)
            token = status.get("detail", {}).get("finished")
            if self.last_status and token and token != self.last_notification:
                self.last_notification = token
                if status["phase"] == "error" and self.cfg.notify_errors:
                    self.notify("Sync needs attention", status.get("detail", {}).get("message", "Open Activity for details."))
                elif status.get("detail", {}).get("phase") == "success" and self.cfg.notify_success:
                    self.notify("Sync finished", "Your local and remote folders are synced for files included in this connection.")
            elif not self.last_status:
                self.last_notification = token
            self.last_status = status
            if self.transfer_snapshot and self.transfer_snapshot["fingerprint"] != self.cfg.fingerprint():
                self.transfer_snapshot = None
                self.transfer_checked = 0
                self.window.transfers.render(None)
            if self.last_transfer_cycle != token:
                self.transfer_checked = 0
                if self.transfer_snapshot:
                    self.transfer_snapshot["stale"] = True
            if self.conflict_snapshot and (self.conflict_snapshot["fingerprint"] != self.cfg.fingerprint() or self.last_transfer_cycle != token):
                self.conflict_snapshot = None
                self.conflict_checked = 0
                self.window.conflicts.render(None)
            report_key = (self.cfg.fingerprint(), token)
            if not self.demo and report_key != self.last_conflict_report:
                self.last_conflict_report = report_key
                reported = reported_snapshot(self.cfg, status.get("detail", {}))
                if reported:
                    self.conflict_snapshot = reported
                    self.conflict_error = None
                    self.conflict_checked = 0
                    self.window.conflicts.render(reported)
            self.last_transfer_cycle = token
            self.window.conflicts.update_status(status, self.conflict_pending, self.conflict_error)
            if self.window.get_visible() and self.window.notebook.get_current_page() == 2:
                self.refresh_conflicts()
            self.window.transfers.update_status(status, self.transfer_pending, self.transfer_error)
            if self.window.get_visible() and self.window.notebook.get_current_page() == 1:
                self.refresh_transfers()
        def failed(error):
            self.refresh_pending = False
            status = {"phase": "error", "label": "Sync status unavailable", "detail": {"message": str(error)}}
            self.window.update(status, str(error))
            self.tray.update(status)
            self.last_status = status
            self.window.transfers.update_status(status, self.transfer_pending, str(error))
            self.window.conflicts.render(None)
            self.conflict_snapshot = None
            self.window.conflicts.update_status(status, self.conflict_pending, str(error))
        self.background(work, done, on_error=failed)
        return True

    def present(self):
        if self.window:
            self.window.show_all()
            self.window.present()

    def notify(self, title, text):
        notification = Gio.Notification.new(title)
        notification.set_icon(file_icon(application_icon_path()))
        notification.set_body(text[:500])
        self.send_notification("sync-status", notification)

    def message(self, title, text, error=False, parent=None):
        dialog = Gtk.MessageDialog(transient_for=parent or self.window, modal=True, use_header_bar=False,
                                   message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO,
                                   buttons=Gtk.ButtonsType.CLOSE, text=title)
        dialog.format_secondary_text(text)
        dialog.run()
        dialog.destroy()

    def question(self, title, text, parent=None):
        dialog = Gtk.MessageDialog(transient_for=parent or self.window, modal=True, use_header_bar=False,
                                   message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO, text=title)
        dialog.format_secondary_text(text)
        dialog.set_default_response(Gtk.ResponseType.NO)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.YES

    def settings(self):
        self.present()
        if self.settings_dialog:
            self.settings_dialog.present()
            return
        self.settings_dialog = SettingsDialog(self, self.cfg)

    def new_connection(self):
        self.present()
        if self.settings_dialog:
            self.settings_dialog.present()
            return
        cfg = Settings(rclone_binary=self.cfg.rclone_binary, rclone_config=self.cfg.rclone_config)
        cfg.local_dir = str(Path.home() / ("drive-" + cfg.profile_id[:6]))
        self.settings_dialog = SettingsDialog(self, cfg, is_new=True)

    def control(self, action):
        if self.demo:
            self.message("Demo mode", "You can explore the app in this demo. Your files and sync schedule stay the same.")
        elif not self.configured:
            self.settings()
        else:
            self.background(action, lambda _result: self.refresh_installation())

    def refresh_installation(self):
        self.health = None
        self.health_checked = 0
        self.refresh()

    def review_repair(self):
        if self.demo:
            self.message("Demo mode", "Installation repair is available outside demo mode.")
            return
        if self.repair_running:
            return
        def present(report):
            if report["state"] in ("healthy", "removed", "unconfigured"):
                self.message("Installation checked", "No repair is needed.")
                return
            automatic = all(item["automatic"] for item in report["issues"])
            fixable = all(item["automatic"] or item["replaceable"] for item in report["issues"])
            details = "\n\n".join(item["message"] + "\n" + item["path"] for item in report["issues"])
            if not fixable:
                self.message("Repair needs attention", details)
                return
            if not automatic and not self.question("Repair these installation files?", details +
                                                   "\n\nExisting definitions will be archived. Reviewed masks and activation links will be replaced."):
                return
            self.repair_running = True
            status = dict(self.last_status, phase="repairing", label="Repairing sync…", installation=report)
            self.window.update(status, "")
            token = None if automatic else report["token"]
            def finished(_result):
                self.repair_running = False
                self.refresh_installation()
            def failed(error):
                self.repair_running = False
                self.message("Could not repair sync", str(error), error=True)
                self.refresh_installation()
            self.background(lambda: self.manager.maintain(retry=True, reviewed_token=token), finished, on_error=failed)
        self.background(self.manager.inspect_installation, present)

    def review_cleanup(self):
        if self.demo:
            self.message("Demo mode", "Installation cleanup is available outside demo mode.")
            return
        def present(candidates):
            if not candidates:
                self.message("Cleanup checked", "No obsolete service files were found.")
                return
            dialog = Gtk.Dialog(title="Review cleanup", transient_for=self.window, modal=True, use_header_bar=False)
            dialog.set_default_size(680, 400)
            dialog.add_buttons("_Cancel", Gtk.ResponseType.CANCEL, "_Archive selected", Gtk.ResponseType.APPLY)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            box.set_border_width(16)
            box.pack_start(label("Selected service definitions will be disabled and archived. Synced files and accounts are kept.", True), False, False, 0)
            choices = []
            for item in candidates:
                check = Gtk.CheckButton(label=Path(item["path"]).name + ": " + item["message"])
                check.set_tooltip_text(item["path"])
                box.pack_start(check, False, False, 0)
                choices.append((check, item))
            dialog.get_content_area().pack_start(scroll(box), True, True, 0)
            dialog.show_all()
            response = dialog.run()
            selected = [item for check, item in choices if check.get_active()]
            dialog.destroy()
            if response == Gtk.ResponseType.APPLY and selected:
                self.background(lambda: self.manager.cleanup(selected), lambda _: self.refresh_installation())
        self.background(self.manager.installation.cleanup_candidates, present)

    def remove_connection(self):
        if self.demo:
            self.message("Demo mode", "Connection removal is available outside demo mode.")
            return
        if not self.configured:
            return
        if self.question("Remove this connection?", "Syncing will stop and the app's services will be removed. Your local and remote files, accounts, and recovery backups are kept."):
            def finished(leftovers):
                self.configured = False
                self.refresh_installation()
                if leftovers:
                    self.message("Connection removed", "Modified installation files were kept:\n" + "\n".join(leftovers))
            self.background(self.manager.remove_connection, finished)

    def sync_now(self):
        self.control(self.manager.sync_now)

    def preview(self):
        self.show_activity()
        self.window.preview_logs.set_active(True)
        self.control(self.manager.preview)

    def pause(self):
        self.control(self.manager.pause)

    def resume(self):
        self.control(self.manager.resume)

    def stop_current(self):
        self.control(self.manager.stop_current)

    def show_activity(self):
        self.present()
        self.window.notebook.set_current_page(3)

    def refresh_conflicts(self, force=False):
        if self.window is None or self.conflict_pending:
            return
        status = self.last_status
        if (status.get("running") or status.get("previewing") or status.get("phase") in ("repair", "repairing")
                or (not self.configured and not self.demo)):
            return
        from .changes import watcher_observation
        previous_local = (self.conflict_snapshot or {}).get("observation", {}) or {}
        previous_local = previous_local.get("local")
        watched = watcher_observation(self.cfg) if self.configured and not self.demo else None
        local_changed = watched is not None and previous_local is not None and watched != previous_local
        if not force and not local_changed and time.monotonic() - self.conflict_checked < 30:
            return
        self.conflict_pending, self.conflict_error = True, None
        self.conflict_runner = Runner()
        cfg, runner = self.cfg, self.conflict_runner
        previous = self.conflict_snapshot
        self.window.conflicts.update_status(status, True)

        def work():
            if self.demo:
                from .engine import timestamp
                return {"fingerprint": cfg.fingerprint(), "history": None, "checked_at": timestamp(), "note": None,
                        "rows": [{"path": "Documents/Project notes.txt", "kind": "Both versions changed",
                                  "local": [{"Path": "Documents/Project notes.txt", "Size": 2400}],
                                  "remote": [{"Path": "Documents/Project notes.txt", "Size": 3100}]}]}
            return scan_conflicts(cfg, runner, previous=previous, force=force)

        def done(snapshot):
            self.conflict_pending, self.conflict_runner = False, None
            self.conflict_checked = time.monotonic()
            if snapshot.get("stale") and self.conflict_snapshot and self.conflict_snapshot.get("reported"):
                self.conflict_checked = 0
            elif cfg.fingerprint() == self.cfg.fingerprint():
                self.conflict_snapshot = snapshot
                self.window.conflicts.render(snapshot)
            else:
                self.conflict_checked = 0
            self.window.conflicts.update_status(self.last_status)

        def failed(error):
            self.conflict_pending, self.conflict_runner = False, None
            self.conflict_checked = time.monotonic()
            self.conflict_error = str(error)
            if self.conflict_snapshot:
                self.conflict_snapshot["stale"] = True
            self.window.conflicts.update_status(self.last_status, error=error)

        self.background(work, done, on_error=failed)

    def resolve_conflict(self, row, side):
        if not row or not self.conflict_snapshot or self.conflict_pending:
            return
        if self.demo:
            self.message("Demo mode", "Conflict resolution is available outside demo mode. Your files stay the same.")
            return
        snapshot, cfg = self.conflict_snapshot, self.cfg
        destination = "remote" if side == "local" else "local"
        from .transfer_view import display_path
        if not self.question(f"Replace the {destination} file?",
                             f"{display_path(row['path'])}\n\nKeep the {side} version and replace the {destination} version. "
                             "The replaced file will be backed up locally. Close editors for this file before continuing."):
            return
        self.conflict_pending = True
        self.conflict_runner = Runner()
        runner = self.conflict_runner
        self.window.conflicts.update_status(self.last_status, True)

        def finished(backup):
            self.conflict_pending, self.conflict_runner = False, None
            self.conflict_snapshot = None
            self.window.conflicts.render(None)
            self.transfer_checked = 0
            self.message("Conflict resolved", f"The {side} version is now on both sides.\nPrevious version saved in:\n{backup}\n\nRefresh Conflicts to review any remaining items, then sync normally.")
            self.refresh_conflicts(force=True)

        def failed(error):
            self.conflict_pending, self.conflict_runner = False, None
            if self.conflict_snapshot:
                self.conflict_snapshot["stale"] = True
            self.window.conflicts.update_status(self.last_status, error=error)
            self.message("Could not resolve conflict", str(error), error=True)

        self.background(lambda: resolve_conflict(cfg, snapshot, row, side, runner), finished, on_error=failed)

    def refresh_transfers(self, force=False):
        if self.window is None or self.transfer_pending:
            return
        status = self.last_status
        if status.get("running") or status.get("previewing") or (not self.configured and not self.demo):
            self.window.transfers.update_status(status, self.transfer_pending)
            return
        if not force and time.monotonic() - self.transfer_checked < 60:
            return
        self.transfer_pending, self.transfer_error = True, None
        self.transfer_runner = Runner()
        cfg = self.cfg
        previous = self.transfer_snapshot
        self.window.transfers.update_status(status, True)

        def work():
            if self.demo:
                from .transfers import timestamp
                return {"fingerprint": cfg.fingerprint(), "checked_at": timestamp(), "history": None,
                        "uploads": [{"path": "Documents/Project notes.txt", "size": 2400, "action": "Copy"}],
                        "downloads": [{"path": "Photos/Lake.jpg", "size": 3200000, "action": "Copy"}],
                        "other": [{"path": "Old draft.txt", "side": "Remote", "action": "Delete"}],
                        "local": {"free": 120e9, "total": 500e9, "reserve": 1e9,
                                  "eligible": {"files": 210, "bytes": 4e9, "unknown": 0}},
                        "remote": {"free": 11e9, "total": 15e9,
                                   "eligible": {"files": 210, "bytes": 4e9, "unknown": 0}},
                        "error": None, "stale": False}
            return scan(cfg, self.transfer_runner, previous=previous, force=force, cache=True)

        def done(snapshot):
            self.transfer_pending, self.transfer_runner = False, None
            self.transfer_checked = time.monotonic()
            if cfg.fingerprint() == self.cfg.fingerprint():
                self.transfer_snapshot = snapshot
                self.window.transfers.render(snapshot)
            else:
                self.transfer_checked = 0
            self.window.transfers.update_status(self.last_status)

        def failed(error):
            self.transfer_pending, self.transfer_runner = False, None
            self.transfer_error = str(error)
            self.transfer_checked = time.monotonic()
            self.window.transfers.update_status(self.last_status, error=error)

        self.background(work, done, on_error=failed)

    def open_path(self, path):
        try:
            if not Path(path).exists():
                self.message("Folder not created yet", "The app creates this folder when it first needs to save files there:\n" + str(path))
                return
            Gtk.show_uri_on_window(self.window, Path(path).as_uri(), Gdk.CURRENT_TIME)
        except GLib.Error as error:
            self.message("Could not open folder", str(error), error=True)

    def open_folder(self):
        self.open_path(self.cfg.local_dir)

    def open_backups(self):
        self.open_path(self.cfg.backup_dir)



def main(config_path, launcher, tray=False, demo=False):
    if not os.environ.get("DISPLAY"):
        print(APP_NAME + " needs an X11 or XWayland desktop session to open a window. "
              "Start the app in a desktop session with DISPLAY set. Background sync can run without a display.", file=sys.stderr)
        return 2
    app = Application(config_path, launcher, tray=tray, demo=demo)
    try:
        require_rclone(app.cfg.rclone_binary)
    except SyncError as error:
        dialog = Gtk.MessageDialog(modal=True, use_header_bar=False,
                                   message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                   text="Cannot start " + APP_NAME)
        dialog.format_secondary_text(str(error))
        dialog.run()
        dialog.destroy()
        app.pool.shutdown(wait=False, cancel_futures=True)
        return 1
    return app.run([str(launcher)])
