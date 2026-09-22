from gi.repository import GObject, Gtk, Pango

from .transfers import format_bytes, format_time, format_totals, totals


def text_label(text=""):
    widget = Gtk.Label(label=text, xalign=0)
    widget.set_line_wrap(True)
    widget.set_max_width_chars(90)
    widget.set_selectable(True)
    return widget


def display_path(value):
    # Keep one row per file even when a real filename contains control characters.
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in value)


def file_table(columns):
    model = Gtk.ListStore(*([str] * len(columns)), GObject.TYPE_INT64)
    tree = Gtk.TreeView(model=model)
    tree.set_headers_visible(True)
    tree.set_enable_search(True)
    tree.set_search_column(0)
    tree.set_tooltip_column(0)
    for index, title in enumerate(columns):
        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn(title, renderer, text=index)
        column.set_resizable(True)
        column.set_sort_column_id(len(columns) if title == "Size" else index)
        if index == 0:
            renderer.set_property("ellipsize", Pango.EllipsizeMode.MIDDLE)
            column.set_expand(True)
            column.set_min_width(100)
        tree.append_column(column)
    pane = Gtk.ScrolledWindow()
    pane.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    pane.set_shadow_type(Gtk.ShadowType.IN)
    pane.set_min_content_height(110)
    pane.add(tree)
    return model, pane


class TransfersView(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.set_border_width(14)
        self.app = app
        toolbar = Gtk.Box(spacing=10)
        self.checked = text_label("Not checked yet")
        toolbar.pack_start(self.checked, True, True, 0)
        self.spinner = Gtk.Spinner()
        toolbar.pack_start(self.spinner, False, False, 0)
        self.refresh_button = Gtk.Button(label="Refresh")
        self.refresh_button.connect("clicked", lambda _button: app.refresh_transfers(force=True))
        toolbar.pack_start(self.refresh_button, False, False, 0)
        self.pack_start(toolbar, False, False, 0)

        grid = Gtk.Grid(column_spacing=20, row_spacing=7)
        self.storage = {}
        for row, (key, title) in enumerate((
                ("local", "Local disk"), ("remote", "Remote storage"),
                ("local_eligible", "Local files included in sync"), ("remote_eligible", "Remote files included in sync"),
                ("reserve", "Minimum free disk space"))):
            grid.attach(text_label(title), 0, row, 1, 1)
            value = text_label("Not checked yet")
            value.set_hexpand(True)
            grid.attach(value, 1, row, 1, 1)
            self.storage[key] = value
        self.pack_start(grid, False, False, 0)
        lists = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        lists.set_position(405)
        self.models, self.summaries = {}, {}
        for key, title in (("uploads", "Pending uploads"), ("downloads", "Pending downloads")):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            heading = text_label(title)
            heading.get_style_context().add_class("heading")
            box.pack_start(heading, False, False, 0)
            self.summaries[key] = text_label("Not checked yet")
            box.pack_start(self.summaries[key], False, False, 0)
            model, pane = file_table(("File name", "Size", "Action"))
            self.models[key] = model
            box.pack_start(pane, True, True, 0)
            if key == "uploads":
                lists.pack1(box, True, False)
            else:
                box.set_margin_start(10)
                lists.pack2(box, True, False)
        self.pack_start(lists, True, True, 0)

        self.other = Gtk.Expander(label="Deletions and folder changes")
        model, pane = file_table(("File or folder", "Location", "Action"))
        self.models["other"] = model
        self.other.add(pane)
        self.pack_start(self.other, False, False, 0)
        self.message = text_label()
        self.message.set_lines(3)
        self.message.set_ellipsize(Pango.EllipsizeMode.END)
        self.pack_start(self.message, False, False, 0)
        self.snapshot = None

    @staticmethod
    def capacity_text(value):
        free, total, used = value.get("free"), value.get("total"), value.get("used")
        if free is not None and total is not None:
            return f"{format_bytes(free)} free of {format_bytes(total)}"
        if free is not None:
            return f"{format_bytes(free)} free · Total not reported"
        if total is not None:
            return f"{format_bytes(total)} total · Free space not reported"
        if used is not None:
            return f"{format_bytes(used)} used · Free space not reported"
        return "Not reported"

    def render(self, snapshot):
        self.snapshot = snapshot
        for key in self.models:
            self.models[key].clear()
        if snapshot is None:
            for value in self.storage.values():
                value.set_text("Not checked yet")
            for value in self.summaries.values():
                value.set_text("Not checked yet")
            self.other.set_label("Deletions and folder changes")
            self.checked.set_text("Not checked yet")
            return
        local, remote = snapshot["local"], snapshot["remote"]
        self.storage["local"].set_text(self.capacity_text(local))
        self.storage["remote"].set_text(self.capacity_text(remote))
        self.storage["local"].set_tooltip_text(local.get("error"))
        self.storage["remote"].set_tooltip_text(remote.get("quota_error"))
        for side, data in (("local", local), ("remote", remote)):
            self.storage[side + "_eligible"].set_text(format_totals(data.get("eligible")))
        self.storage["reserve"].set_text(format_bytes(local.get("reserve")))
        for key in ("uploads", "downloads"):
            entries = snapshot[key]
            self.summaries[key].set_text("Unavailable" if snapshot["error"] else format_totals(totals(entries)))
            for entry in entries:
                self.models[key].append((display_path(entry["path"]), format_bytes(entry["size"]), entry["action"],
                                         entry["size"] if entry["size"] is not None else -1))
        for entry in snapshot["other"]:
            self.models["other"].append((display_path(entry["path"]), entry["side"], entry["action"], -1))
        self.other.set_label(f"Deletions and folder changes ({len(snapshot['other']):,})")
        self.checked.set_text("Last checked: " + format_time(snapshot["checked_at"]))

    def update_status(self, status, pending=False, error=None):
        busy = bool(status.get("running") or status.get("previewing"))
        self.refresh_button.set_sensitive((self.app.configured or self.app.demo) and not busy and not pending)
        self.spinner.start() if pending else self.spinner.stop()
        snapshot = self.snapshot
        if pending:
            message = "Checking for changes…"
        elif not self.app.configured and not self.app.demo:
            message = "Open Settings to create or import a connection, then check for pending changes here."
        elif error:
            message = "Could not refresh pending changes. " + str(error)
        elif busy:
            message = "A sync or preview is running. This list refreshes when it finishes."
        elif snapshot and snapshot["error"]:
            message = "Could not check pending changes. " + snapshot["error"]
        elif snapshot and snapshot["stale"]:
            message = "Sync history changed during this check. Select Refresh to update the list."
        elif snapshot:
            message = "No pending changes found." if not any(snapshot[k] for k in ("uploads", "downloads", "other")) else "These changes were pending when last checked."
        else:
            message = "Select Refresh to check for pending changes."
        self.message.set_text(message)
        self.message.set_tooltip_text(message)
