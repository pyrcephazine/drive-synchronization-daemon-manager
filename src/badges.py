from gi.repository import Gtk


# Meaning, not position or text, determines each badge's color.
COLORS = {
    "schedule": ("#e7eefc", "#204a87"),
    "local-change": ("#fff0d4", "#714600"),
    "upload": ("#e0f2e9", "#205b3c"),
    "download": ("#efe6fa", "#583580"),
    "paused": ("#ececec", "#454545"),
}


class Badges(Gtk.Box):
    def __init__(self):
        super().__init__(spacing=6)
        self.set_halign(Gtk.Align.START)
        self.items = None

    def set_items(self, items):
        if items == self.items:
            return
        for child in self.get_children():
            child.destroy()
        for kind, text in items:
            background, foreground = COLORS[kind]
            badge = Gtk.Label(label=text)
            badge.set_selectable(True)
            style = Gtk.CssProvider()
            style.load_from_data((
                f"label {{ background-color: {background}; color: {foreground}; "
                "border-radius: 4px; padding: 4px 8px; font-weight: 600; }"
            ).encode())
            badge.get_style_context().add_provider(style, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            badge.get_style_context().add_class("badge-" + kind)
            self.pack_start(badge, False, False, 0)
        self.items = list(items)
        self.show_all()
