from gi.repository import Gtk, Pango

from .accounts import AccountSetup
from .config import SyncError


def text(value):
    if isinstance(value, bool):
        return str(value).lower()
    return "" if value is None else str(value)


def fit_choices(widget):
    for renderer in widget.get_cells():
        if isinstance(renderer, Gtk.CellRendererText):
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
            renderer.set_property("max-width-chars", 50)


class AccountDialog(Gtk.Dialog):
    def __init__(self, parent, cfg):
        super().__init__(title="Connect account", transient_for=parent, modal=True, use_header_bar=False)
        self.set_default_size(600, 420)
        self.parent, self.app = parent, parent.app
        self.session = AccountSetup(cfg.rclone_binary, cfg.rclone_config)
        self.closed, self.pending, self.ready = False, False, False
        self.option = None
        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.next_button = self.add_button("_Continue", Gtk.ResponseType.APPLY)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.box.set_border_width(20)
        pane = Gtk.ScrolledWindow()
        pane.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        pane.add(self.box)
        self.get_content_area().pack_start(pane, True, True, 0)
        self.status = Gtk.Label(xalign=0, wrap=True)
        self.status.set_max_width_chars(65)
        self.status.set_margin_start(20)
        self.status.set_margin_end(20)
        self.status.set_margin_bottom(12)
        self.get_content_area().pack_start(self.status, False, False, 0)
        self.connect("response", self.respond)
        self.connect("delete-event", self.cancel)
        self.connect("destroy", lambda *_: self.cleanup())
        self.app.account_dialog = self
        self.show_all()
        self.run_step(self.session.open, self.show_providers)

    def clear(self):
        for child in self.box.get_children():
            child.destroy()

    def heading(self, title):
        widget = Gtk.Label(label=title, xalign=0, wrap=True)
        widget.set_max_width_chars(65)
        widget.set_selectable(True)
        self.box.pack_start(widget, False, False, 0)

    def show_providers(self, providers):
        self.clear()
        self.heading("Account name")
        self.name = Gtk.Entry()
        self.box.pack_start(self.name, False, False, 0)
        self.heading("Storage provider")
        self.provider = Gtk.ComboBoxText()
        for provider in sorted(providers, key=lambda item: item["Description"].casefold()):
            self.provider.append(provider["Name"], provider["Description"])
        self.provider.set_active_id("drive")
        fit_choices(self.provider)
        self.box.pack_start(self.provider, False, False, 0)
        self.box.show_all()

    def show_question(self, result):
        self.clear()
        self.ready = result.get("State") == ""
        if self.ready:
            self.heading("Account ready to save")
            self.option = None
            self.next_button.set_label("_Save account")
        else:
            self.option = option = result.get("Option")
            if not option:
                raise SyncError("rclone returned an incomplete setup question. Cancel and try again.")
            help_text = option.get("Help", "").strip()
            summary, _, details = help_text.partition("\n\n")
            self.heading(summary or option["Name"].replace("_", " ").capitalize())
            examples = option.get("Examples") or []
            default = text(option.get("Default"))
            if examples and option.get("Exclusive"):
                self.answer = Gtk.ComboBoxText()
                if not option.get("Required") and not default:
                    self.answer.append("", "Default")
                for example in examples:
                    value = text(example["Value"])
                    description = example.get("Help", "").split("\n", 1)[0]
                    self.answer.append(value, description or value)
                self.answer.set_active_id(default)
            elif option.get("Type") == "bool":
                self.answer = Gtk.ComboBoxText()
                self.answer.append("true", "Yes")
                self.answer.append("false", "No")
                self.answer.set_active_id(default)
            elif examples and not option.get("IsPassword"):
                self.answer = Gtk.ComboBoxText.new_with_entry()
                for example in examples:
                    self.answer.append_text(text(example["Value"]))
                self.answer.get_child().set_text(default)
            else:
                self.answer = Gtk.Entry()
                self.answer.set_visibility(not option.get("IsPassword"))
                self.answer.set_text(default)
            self.box.pack_start(self.answer, False, False, 0)
            if isinstance(self.answer, Gtk.ComboBoxText):
                fit_choices(self.answer)
            if details:
                expander = Gtk.Expander(label="Details")
                description = Gtk.Label(label=details, xalign=0, wrap=True, selectable=True)
                description.set_max_width_chars(65)
                expander.add(description)
                self.box.pack_start(expander, False, False, 0)
            if result.get("Error"):
                self.status.set_text("Check your answer and try again.")
        self.box.show_all()

    def value(self):
        if isinstance(self.answer, Gtk.Entry):
            return self.answer.get_text()
        if self.answer.get_has_entry():
            return self.answer.get_child().get_text()
        return self.answer.get_active_id()

    def run_step(self, work, done):
        self.pending = True
        self.next_button.set_sensitive(False)
        self.box.set_sensitive(False)
        self.status.set_text("Connecting… Complete sign-in in your browser if it opens.")
        def success(result):
            if self.closed:
                return
            self.pending = False
            self.next_button.set_sensitive(True)
            self.box.set_sensitive(True)
            self.status.set_text("")
            try:
                done(result)
            except SyncError as error:
                failure(error)
        def failure(error):
            if self.closed:
                return
            self.pending = False
            self.next_button.set_sensitive(True)
            self.box.set_sensitive(True)
            self.status.set_text(str(error))
        self.app.background(work, success, on_error=failure)

    def respond(self, _dialog, response):
        if response != Gtk.ResponseType.APPLY:
            self.cancel()
            return
        if self.pending:
            return
        if self.ready:
            self.run_step(self.session.save, self.saved)
        elif self.option:
            value = self.value()
            if value is None or (self.option.get("Required") and not value):
                self.status.set_text("Enter a value to continue.")
                return
            self.run_step(lambda: self.session.answer(value), self.show_question)
        elif hasattr(self, "name"):
            name, provider = self.name.get_text().strip(), self.provider.get_active_id()
            if not provider:
                self.status.set_text("Select a storage provider.")
                return
            self.run_step(lambda: self.session.start(name, provider), self.show_question)

    def saved(self, remote):
        self.parent.widgets["remote"][0].get_child().set_text(remote)
        self.parent.load_remotes()
        self.destroy()

    def cancel(self, *_args):
        if self.pending and self.ready:
            return True
        self.destroy()
        return True

    def cleanup(self):
        if not self.closed:
            self.closed = True
            self.session.close()
            self.app.account_dialog = None
