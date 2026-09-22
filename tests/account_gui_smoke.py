import os
from pathlib import Path
import tempfile
import traceback

import cairo
from src.config import Settings
from src.gui import Application, GLib, Gtk, SettingsDialog
from src.account_dialog import AccountDialog

ROOT = Path(__file__).resolve().parents[1]
temporary = tempfile.TemporaryDirectory(prefix="localdrive-account-gui-")
root = Path(temporary.name)
os.environ["XDG_CONFIG_HOME"] = str(root / "config")
os.environ["XDG_STATE_HOME"] = str(root / "state")
app = Application(root / "config.json", ROOT / "bin/drive-synchronization-daemon-manager", demo=True)
app.set_application_id("io.github.localdrivesync.AccountSmoke")
app.cfg = Settings(rclone_config=str(root / "rclone.conf"))
result = {"failed": False}
step = 0


def screenshot(window, name):
    window.check_resize()
    assert window.get_allocated_width() <= 800, "Account controls expanded the window"
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, window.get_allocated_width(), window.get_allocated_height())
    window.draw(cairo.Context(surface))
    surface.write_to_png(str(ROOT / "dist" / name))


def advance():
    global step, parent, dialog
    try:
        if app.window is None:
            return True
        if step == 0:
            parent = SettingsDialog(app, app.cfg, is_new=True)
            dialog = AccountDialog(parent, app.cfg)
            step = 1
        elif dialog.pending:
            return True
        elif step == 1:
            assert dialog.get_titlebar() is None
            assert dialog.provider.get_active_id() == "drive"
            dialog.name.set_text("local test")
            dialog.provider.set_active_id("local")
            screenshot(dialog, "connect-account.png")
            dialog.respond(dialog, Gtk.ResponseType.APPLY)
            step = 2
        elif step == 2:
            assert dialog.option["Name"] == "config_fs_advanced", dialog.status.get_text()
            assert dialog.value() == "false"
            screenshot(dialog, "connect-account-question.png")
            dialog.respond(dialog, Gtk.ResponseType.APPLY)
            step = 3
        elif step == 3:
            assert dialog.ready, dialog.status.get_text()
            dialog.respond(dialog, Gtk.ResponseType.APPLY)
            step = 4
        elif step == 4:
            assert dialog.closed
            assert parent.widgets["remote"][0].get_child().get_text() == "local test:"
            assert "[local test]" in (root / "rclone.conf").read_text()
            assert not dialog.session.work.exists()
            assert app.account_dialog is None
            parent.destroy()
            print("PASS: native account window, provider selection, question, save, remote refresh, cleanup")
            app.quit()
            return False
    except Exception:
        result["failed"] = True
        traceback.print_exc()
        app.quit()
        return False
    return True


def timeout():
    result["failed"] = True
    print("FAIL: account wizard timed out at step", step)
    app.quit()
    return False


GLib.timeout_add(250, advance)
GLib.timeout_add_seconds(30, timeout)
app.run([str(ROOT / "bin/drive-synchronization-daemon-manager")])
temporary.cleanup()
raise SystemExit(1 if result["failed"] else 0)
