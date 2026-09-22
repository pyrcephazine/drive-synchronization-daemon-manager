from gi.repository import Gtk

from .conflicts import allowed
from .transfer_view import display_path, file_table, text_label
from .transfers import format_bytes, format_time


class ConflictsView(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_border_width(14)
        self.app = app
        self.snapshot = None
        self.pending = False
        self.status = {}
        toolbar = Gtk.Box(spacing=10)
        self.checked = text_label("Not checked yet")
        toolbar.pack_start(self.checked, True, True, 0)
        self.spinner = Gtk.Spinner()
        toolbar.pack_start(self.spinner, False, False, 0)
        self.refresh_button = Gtk.Button(label="Refresh")
        self.refresh_button.connect("clicked", lambda *_: app.refresh_conflicts(force=True))
        toolbar.pack_start(self.refresh_button, False, False, 0)
        self.pack_start(toolbar, False, False, 0)
        self.pack_start(text_label("Review files that block sync or have changed on both sides. Select a file and choose which version to keep."), False, False, 0)
        self.model, pane = file_table(("File name", "Conflict", "Local size", "Remote size"))
        self.tree = pane.get_child()
        self.tree.get_selection().connect("changed", lambda *_: self.selection_changed())
        self.pack_start(pane, True, True, 0)
        self.details = text_label("Select a conflict to review its resolution options.")
        self.pack_start(self.details, False, False, 0)
        actions = Gtk.Box(spacing=10)
        self.buttons = {}
        for side, title in (("local", "Use local version…"), ("remote", "Use remote version…")):
            button = Gtk.Button(label=title)
            button.connect("clicked", lambda _button, side=side: app.resolve_conflict(self.selected(), side))
            actions.pack_start(button, False, False, 0)
            self.buttons[side] = button
        self.pack_start(actions, False, False, 0)
        self.message = text_label("Select Refresh to check for conflicts.")
        self.pack_start(self.message, False, False, 0)
        self.selection_changed()

    def selected(self):
        model, iterator = self.tree.get_selection().get_selected()
        if iterator is None or not self.snapshot:
            return None
        return self.snapshot["rows"][model[iterator][4]]

    def selection_changed(self):
        row = self.selected()
        busy = self.pending or bool(self.snapshot and self.snapshot.get("stale")) or self.status.get("running") or self.status.get("previewing") or self.status.get("phase") in ("repair", "repairing")
        for side, button in self.buttons.items():
            button.set_sensitive(bool(row and not busy and allowed(self.app.cfg, row, side)))
        if not row:
            message = "Select a conflict to review its resolution options."
        elif row.get("blocked"):
            message = row["blocked"]
        elif row["kind"] == "Duplicate name":
            sides = ", ".join(side for side in ("local", "remote") if len(row[side]) > 1) or "the reported sync folder"
            message = f"Duplicate names on {sides}. Rename the matching files or folders there, then refresh. Replacement is unavailable until each name identifies one item."
        elif row["kind"] == "File / folder conflict":
            message = "A file and a folder share this name. Rename one of them, then refresh."
        elif not any(allowed(self.app.cfg, row, side) for side in ("local", "remote")):
            message = "Automatic replacement is unavailable for these items. Review them in their folders or adjust the size limit in Settings, then refresh."
        else:
            message = "Use local replaces the remote file. Use remote replaces the local file. The replaced version is backed up locally."
            if row["kind"] == "Size limit conflict":
                message += " Only a version within the size limit can be kept."
        self.details.set_text(message)

    def render(self, snapshot):
        self.snapshot = snapshot
        self.model.clear()
        if snapshot:
            for index, row in enumerate(snapshot["rows"]):
                sizes = [format_bytes(row[side][0]["Size"]) if len(row[side]) == 1 and not row[side][0].get("IsDir")
                         else f"{len(row[side])} items" for side in ("local", "remote")]
                self.model.append((display_path(row["path"]), row["kind"], *sizes, index))
        self.checked.set_text("Last checked: " + format_time(snapshot["checked_at"]) if snapshot else "Not checked yet")
        self.selection_changed()

    def update_status(self, status, pending=False, error=None):
        self.status, self.pending = status, pending
        busy = status.get("running") or status.get("previewing") or status.get("phase") in ("repair", "repairing")
        self.refresh_button.set_sensitive(bool((self.app.configured or self.app.demo) and not busy and not pending))
        self.spinner.start() if pending else self.spinner.stop()
        if pending:
            message = "Checking or resolving conflicts…"
        elif error:
            message = "Could not check conflicts. " + str(error)
        elif busy:
            message = "Wait for the current operation to finish, then refresh."
        elif not self.app.configured and not self.app.demo:
            message = "Open Settings to create or import a connection first."
        elif self.snapshot and self.snapshot.get("stale"):
            message = "Sync changed during this check. Refresh before replacing files."
        elif self.snapshot:
            message = self.snapshot.get("note") or (f"{len(self.snapshot['rows'])} {'conflict' if len(self.snapshot['rows']) == 1 else 'conflicts'} found." if self.snapshot["rows"] else "No conflicts found.")
        else:
            message = "Select Refresh to check for conflicts."
        self.message.set_text(message)
        self.selection_changed()
