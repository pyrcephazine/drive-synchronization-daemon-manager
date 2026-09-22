from pathlib import Path
import cairo
import subprocess
import tempfile
import traceback

from src.config import APP_NAME, Settings
from src.gui import Application, FIELDS, GLib, Gdk, GdkPixbuf, Gtk, SettingsDialog, icon_pixbuf
from src.icons import INDICATOR_FILES, application_icon_path, icon_path

ROOT = Path(__file__).resolve().parents[1]
temporary = tempfile.TemporaryDirectory(prefix="localdrive-gui-")
app = Application(Path(temporary.name) / "config.json", ROOT / "bin/drive-synchronization-daemon-manager", demo=True)
app.set_application_id("io.github.localdrivesync.Smoke")
app.cfg = Settings()
app.cfg.remote = "google:"
result = {"failed": False}


def screenshot(window, name):
    # Render our own widget, never the shared desktop: another application may
    # cover the window while the user keeps working during the smoke test.
    window.check_resize()
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, window.get_allocated_width(), window.get_allocated_height())
    window.draw(cairo.Context(surface))
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    surface.write_to_png(str(output / name))


def inspect_settings(dialog):
    try:
        assert dialog.get_titlebar() is None, "Settings unexpectedly has a custom titlebar"
        assert not dialog.get_style_context().has_class("csd"), "Settings has client-side decorations"
        assert len(dialog.widgets) == sum(len(fields) for _, fields in FIELDS)
        assert dialog.collect() == app.cfg, "Settings round-trip changed defaults"
        dialog.widgets["transfers"][0].set_value(2)
        assert dialog.collect().transfers == 2
        screenshot(dialog, "settings.png")
        notebook = dialog.widgets["interval_seconds"][0].get_ancestor(Gtk.Notebook)
        notebook.set_current_page(1)
        GLib.timeout_add(250, inspect_schedule, dialog)
        return False
    except Exception:
        result["failed"] = True
        traceback.print_exc()
    app.quit()
    return False


def inspect_schedule(dialog):
    try:
        assert dialog.collect().watch_local
        assert dialog.collect().watch_debounce_seconds == 2
        assert dialog.collect().full_scan_interval_seconds == 3600
        screenshot(dialog, "settings-schedule.png")
        dialog.destroy()
        if app.tray.available():
            app.window.close()
            GLib.timeout_add(200, inspect_closed)
            return False
        print("PASS: all settings, round-trip, WM-decorated dialogs (tray unavailable)")
    except Exception:
        result["failed"] = True
        traceback.print_exc()
    app.quit()
    return False


def inspect_closed():
    try:
        assert not app.window.get_visible(), "Close should hide to tray"
        app.present()
        assert app.window.get_visible()
        print("PASS: all settings, round-trip, WM-decorated dialogs, tray close/reopen")
    except Exception:
        result["failed"] = True
        traceback.print_exc()
    app.quit()
    return False


def inspect_main():
    try:
        assert app.window.get_titlebar() is None, "Main window unexpectedly has a custom titlebar"
        assert not app.window.get_style_context().has_class("csd"), "Main window has client-side decorations"
        assert len(app.window.menu_bar.get_children()) == 2
        assert app.window.get_title() == APP_NAME + ": Demo"
        file_menu = app.window.menu_bar.get_children()[0].get_submenu()
        assert file_menu.get_children()[0].get_label() == "_New connection…"
        assert app.window.notebook.get_n_pages() == 4
        assert app.window.values["cycle"].items == [("schedule", "Once every 30 seconds"), ("local-change", "After local changes")]
        assert app.window.values["direction"].items == [("upload", "Local to remote"), ("download", "Remote to local")]
        for key in ("cycle", "direction"):
            badges = app.window.values[key]
            for widget, (kind, text) in zip(badges.get_children(), badges.items):
                assert widget.get_text() == text
                assert widget.get_style_context().has_class("badge-" + kind)
        assert "files" not in app.window.values
        app.configured = True  # Demo blocks every actual service-control action.
        for scheduled in (True, False):
            status = {"phase": "ready" if scheduled else "paused", "label": "Ready" if scheduled else "Paused", "scheduled": scheduled}
            app.window.update(status, "")
            app.tray.update(status)
            assert app.window.pause_button.get_sensitive() == scheduled
            assert app.window.resume_button.get_sensitive() != scheduled
            assert app.window.sync_items[2].get_sensitive() == scheduled
            assert app.window.sync_items[3].get_sensitive() != scheduled
            assert app.tray.pause_item.get_sensitive() == scheduled
            assert app.tray.resume_item.get_sensitive() != scheduled
            if not scheduled:
                assert app.window.values["cycle"].items == [("paused", "Paused")]
        repair_status = {"phase": "repair", "label": "Sync needs repair", "scheduled": True,
                         "installation": {"state": "repairable", "issues": [{"message": "Restore missing file."}]}}
        app.window.update(repair_status, "")
        app.tray.update(repair_status)
        assert app.window.repair_button.get_visible()
        assert app.window.repair_button.get_label() == "Retry repair"
        assert not app.window.sync_button.get_sensitive()
        assert not app.window.resume_button.get_sensitive()
        assert app.window.pause_button.get_sensitive()
        assert not app.tray.sync_item.get_sensitive()
        screenshot(app.window, "repair.png")
        app.window.update(app.last_status, "")
        expected = GdkPixbuf.Pixbuf.new_from_file(application_icon_path())
        assert app.window.get_icon().get_pixels() == expected.get_pixels(), "Main window must use drive-multidisk.svg"
        assert app.window.phase_icon.get_pixel_size() == 64, "Full-size SVGs must not expand the overview"
        assert app.window.phase_icon.get_gicon()[0].get_file().get_path() == icon_path("ready")
        for phase in INDICATOR_FILES:
            app.tray.update({"phase": phase, "label": phase})
            assert app.tray.current_icon == icon_path(phase)
            if app.tray.status_icon:
                size = app.tray.status_icon.get_size() or 24
                pixbuf = app.tray.status_icon.get_pixbuf()
                assert pixbuf.get_pixels() == icon_pixbuf(icon_path(phase), size).get_pixels()
                assert max(pixbuf.get_width(), pixbuf.get_height()) <= size
        app.tray.update(app.last_status)
        xid = app.window.get_window().get_xid()
        output = subprocess.check_output(["xprop", "-id", hex(xid), "_NET_FRAME_EXTENTS", "_GTK_FRAME_EXTENTS"], text=True)
        print(output)
        assert "_NET_FRAME_EXTENTS(CARDINAL)" in output, "Window manager did not supply frame decorations"
        assert "_GTK_FRAME_EXTENTS(CARDINAL)" not in output, "GTK supplied client frame extents"
        screenshot(app.window, "overview.png")
        print("Tray embedded:", app.tray.available())
        app.window.notebook.set_current_page(1)
        GLib.timeout_add(800, inspect_transfers)
    except Exception:
        result["failed"] = True
        traceback.print_exc()
        app.quit()
    return False


def inspect_transfers():
    try:
        view = app.window.transfers
        assert not app.transfer_pending
        assert len(view.models["uploads"]) == 1
        assert len(view.models["downloads"]) == 1
        assert len(view.models["other"]) == 1
        assert "120.00 GB free of 500.00 GB" == view.storage["local"].get_text()
        assert "11.00 GB free of 15.00 GB" == view.storage["remote"].get_text()
        assert view.checked.get_text().startswith("Last checked: ")
        screenshot(app.window, "transfers.png")
        view.update_status({"running": True})
        assert not view.refresh_button.get_sensitive()
        assert "running" in view.message.get_text()
        view.update_status(app.last_status)
        app.show_activity()
        assert app.window.notebook.get_current_page() == 3
        app.window.notebook.set_current_page(2)
        GLib.timeout_add(800, inspect_conflicts)
    except Exception:
        result["failed"] = True
        traceback.print_exc()
        app.quit()
    return False


def inspect_conflicts():
    try:
        view = app.window.conflicts
        assert not app.conflict_pending
        assert len(view.model) == 1
        view.tree.get_selection().select_path(0)
        assert all(button.get_sensitive() for button in view.buttons.values())
        screenshot(app.window, "conflicts.png")
        view.update_status({"running": True})
        assert not any(button.get_sensitive() for button in view.buttons.values())
        assert not view.refresh_button.get_sensitive()
        view.update_status(app.last_status)
        app.window.notebook.set_current_page(0)
        dialog = SettingsDialog(app, app.cfg)
        GLib.timeout_add(800, inspect_settings, dialog)
    except Exception:
        result["failed"] = True
        traceback.print_exc()
        app.quit()
    return False


GLib.timeout_add(1500, inspect_main)
app.run([str(ROOT / "bin/drive-synchronization-daemon-manager")])
temporary.cleanup()
raise SystemExit(1 if result["failed"] else 0)
