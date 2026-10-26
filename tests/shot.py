"""開発用: 画面の見た目を PNG にする(Frame 上で `python3 tests/shot.py 出力フォルダ`)。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "frame"))
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib  # noqa: E402
import fsr_gui  # noqa: E402


def pump():
    for _ in range(40):
        while Gtk.events_pending():
            Gtk.main_iteration()


def shot(app_state, path):
    fsr_gui.install_css()
    app = fsr_gui.App()
    content = app.content
    content.get_parent().remove(content)
    off = Gtk.OffscreenWindow()
    off.set_default_size(1500, 960)
    off.add(content)
    off.show_all()
    app.stack.set_transition_type(Gtk.StackTransitionType.NONE)
    app_state(app)
    pump()
    off.get_pixbuf().savev(path, "png", [], [])
    print("wrote", path)


out = sys.argv[1] if len(sys.argv) > 1 else "."
os.makedirs(out, exist_ok=True)
shot(lambda a: a._ui_status({"name": "MY-PC", "steamvr": "running", "vrchat": True}, {"ip": "1.2.3.4"}), os.path.join(out, "main-running.png"))
shot(lambda a: a._ui_status({"name": "MY-PC", "steamvr": "stopped", "vrchat": False}, {"ip": "1.2.3.4"}), os.path.join(out, "main-stopped.png"))
def pair(a):
    a.code = "48"
    a._render_code()
    a.stack.set_visible_child_name("pair")
    a._ui_unpaired({"name": "MY-PC"})
shot(pair, os.path.join(out, "pair.png"))
shot(lambda a: a._ui_offline("PC が見つかりません。PC が起動していて、トレイのアプリが動いているか確認してください"), os.path.join(out, "offline.png"))
