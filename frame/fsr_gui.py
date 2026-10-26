"""frame-steamvr-remote の画面(GTK3)。SteamVR の中のデスクトップでも押しやすい大きさにしてある。"""
import os
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

import fsr_core as core  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
LOGO = os.path.join(HERE, "logo.png")
if not os.path.exists(LOGO):
    LOGO = os.path.join(HERE, "..", "assets", "logo.png")

CSS = b"""
window, .root { background-image: linear-gradient(to bottom, #2A1B5E, #0F0C29); color: #ffffff; }
label { color: #ffffff; }
.title { font-size: 46px; font-weight: bold; }
.sub { font-size: 24px; color: #b9b2e6; }
.card { background-color: rgba(255,255,255,0.09); border-radius: 32px; padding: 28px 36px; }
.pcname { font-size: 52px; font-weight: bold; }
.state { font-size: 34px; font-weight: bold; }
.msg { font-size: 26px; color: #ffe6a8; }
.hint { font-size: 24px; color: #d6d1f5; }
.bigbtn { font-size: 54px; font-weight: bold; color: #ffffff; border: none; border-radius: 36px;
          min-height: 190px; text-shadow: none; box-shadow: 0 8px 0 rgba(0,0,0,0.35); }
.bigbtn:disabled { opacity: 0.35; }
.quit { background-image: linear-gradient(to bottom, #FF6FA6, #D83A78); }
.restart { background-image: linear-gradient(to bottom, #35E6D0, #1A9C8C); color: #08241f; }
.armed { background-image: linear-gradient(to bottom, #FFB84D, #E07B00); color: #2b1600; }
.small { font-size: 26px; color: #ffffff; border: none; border-radius: 24px; min-height: 64px; padding: 4px 28px;
         background-image: none; background-color: rgba(255,255,255,0.16); text-shadow: none; box-shadow: none; }
.key { font-size: 48px; font-weight: bold; color: #ffffff; border: none; border-radius: 24px; min-width: 130px; min-height: 104px;
       background-image: none; background-color: rgba(255,255,255,0.16); text-shadow: none; box-shadow: none; }
.keysm { font-size: 30px; }
.key:active { background-color: #7C5CFF; }
.code { font-size: 78px; font-weight: bold; letter-spacing: 14px; color: #2BD9C4; }
"""


def _label(text, cls, wrap=False, xalign=0.5):
    l = Gtk.Label(label=text)
    l.get_style_context().add_class(cls)
    l.set_xalign(xalign)
    if wrap:
        l.set_line_wrap(True)
        l.set_max_width_chars(48)
        l.set_justify(Gtk.Justification.CENTER if xalign == 0.5 else Gtk.Justification.LEFT)
    return l


class App(Gtk.Window):
    def __init__(self):
        super().__init__(title="SteamVR リモート")
        self.set_default_size(1280, 900)
        self.set_position(Gtk.WindowPosition.CENTER)
        try:
            self.set_icon_from_file(LOGO)
        except Exception:  # アイコンが無くても動く
            pass
        self.get_style_context().add_class("root")
        self.busy = False
        self.armed = None  # (cmd, 期限)
        self.code = ""
        self.pairing_in_progress = False
        self.pc = None
        self.status = None
        self.stop = False

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        outer.set_border_width(40)
        outer.pack_start(self._header(), False, False, 0)
        outer.pack_start(self.stack, True, True, 0)
        self.msg = _label("", "msg", wrap=True)
        outer.pack_end(self.msg, False, False, 0)
        self.content = outer
        self.add(outer)
        self.stack.add_named(self._page_search(), "search")
        self.stack.add_named(self._page_main(), "main")
        self.stack.add_named(self._page_pair(), "pair")
        self.stack.set_visible_child_name("search")
        self.connect("destroy", self._on_destroy)
        self.connect("key-press-event", self._on_key)

    # ---------------------------------------------------------- 画面の部品
    def _header(self):
        box = Gtk.Box(spacing=24)
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(LOGO, 96, 96)
            box.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
        except Exception:
            pass
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        col.pack_start(_label("SteamVR リモート", "title", xalign=0), False, False, 0)
        col.pack_start(_label("PC の SteamVR を Frame から止める・つなぎ直す", "sub", xalign=0), False, False, 0)
        box.pack_start(col, True, True, 0)
        return box

    def _page_search(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        box.set_valign(Gtk.Align.CENTER)
        self.search_label = _label("PC を探しています…", "state")
        box.pack_start(self.search_label, False, False, 0)
        self.search_hint = _label("", "hint", wrap=True)
        box.pack_start(self.search_hint, False, False, 0)
        btn = Gtk.Button(label="もう一度探す")
        btn.get_style_context().add_class("small")
        btn.set_halign(Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: self._kick())
        box.pack_start(btn, False, False, 0)
        return box

    def _page_main(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=26)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("card")
        self.pc_label = _label("-", "pcname")
        self.state_label = _label("-", "state")
        self.note_label = _label("", "hint", wrap=True)
        for w in (self.pc_label, self.state_label, self.note_label):
            card.pack_start(w, False, False, 0)
        box.pack_start(card, False, False, 0)
        row = Gtk.Box(spacing=30, homogeneous=True)
        self.btn_quit = Gtk.Button(label="SteamVR終了")
        self.btn_quit.get_style_context().add_class("bigbtn")
        self.btn_quit.get_style_context().add_class("quit")
        self.btn_quit.connect("clicked", lambda *_: self._operate("quit"))
        self.btn_restart = Gtk.Button(label="再起動")
        self.btn_restart.get_style_context().add_class("bigbtn")
        self.btn_restart.get_style_context().add_class("restart")
        self.btn_restart.connect("clicked", lambda *_: self._operate("restart"))
        row.pack_start(self.btn_quit, True, True, 0)
        row.pack_start(self.btn_restart, True, True, 0)
        box.pack_start(row, False, False, 0)
        foot = Gtk.Box(spacing=16)
        again = Gtk.Button(label="ペアリングし直す")
        again.get_style_context().add_class("small")
        again.connect("clicked", lambda *_: self._show_pair())
        foot.pack_end(again, False, False, 0)
        box.pack_end(foot, False, False, 0)
        return box

    def _page_pair(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.pair_hint = _label(
            "PC のタスクトレイの SteamVR リモートのアイコンを右クリックして、\n「Frame とペアリングする」を選び、出てきた 6 桁のコードを入力してください。",
            "hint", wrap=True)
        box.pack_start(self.pair_hint, False, False, 0)
        self.pair_pc = _label("", "sub")
        box.pack_start(self.pair_pc, False, False, 0)
        self.code_label = _label("", "code")
        box.pack_start(self.code_label, False, False, 6)
        grid = Gtk.Grid(column_spacing=18, row_spacing=14)
        grid.set_halign(Gtk.Align.CENTER)
        keys = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "消す", "0", "全消し"]
        for i, k in enumerate(keys):
            b = Gtk.Button(label=k)
            b.get_style_context().add_class("key")
            if len(k) > 1:
                b.get_style_context().add_class("keysm")
            b.connect("clicked", self._on_key_button, k)
            grid.attach(b, i % 3, i // 3, 1, 1)
        box.pack_start(grid, False, False, 0)
        self._render_code()
        return box

    # ---------------------------------------------------------- ペアリング入力
    def _render_code(self):
        digits = list(self.code) + ["・"] * (6 - len(self.code))
        self.code_label.set_text(" ".join(digits))

    def _on_key_button(self, _b, k):
        if self.pairing_in_progress:
            return
        if k == "消す":
            self.code = self.code[:-1]
        elif k == "全消し":
            self.code = ""
        elif len(self.code) < 6:
            self.code += k
        self._render_code()
        if len(self.code) == 6:
            self._submit_code()

    def _on_key(self, _w, ev):
        if self.stack.get_visible_child_name() != "pair":
            return False
        name = Gdk.keyval_name(ev.keyval) or ""
        if name.startswith("KP_") and name[3:].isdigit():
            name = name[3:]
        if len(name) == 1 and name.isdigit():
            self._on_key_button(None, name)
            return True
        if name == "BackSpace":
            self._on_key_button(None, "消す")
            return True
        return False

    def _show_pair(self):
        self.code = ""
        self._render_code()
        self.stack.set_visible_child_name("pair")
        self._set_msg("")

    def _submit_code(self):
        code = self.code
        pc = self.pc
        if not pc:
            self._set_msg("PC が見つかりません。PC のアプリが動いているか確認してください")
            self.code = ""
            self._render_code()
            return
        self.pairing_in_progress = True
        self._set_msg("ペアリングしています…")

        def work():
            try:
                name = core.pair(pc["ip"], int(pc.get("port") or core.CONTROL_PORT_DEFAULT), code, pc.get("name", ""))
                GLib.idle_add(self._pair_done, name, None)
            except core.RemoteError as e:
                core.log.error("ペアリング失敗: %s", e)
                GLib.idle_add(self._pair_done, None, str(e))
            except Exception as e:  # 想定外でも画面は固まらせない
                core.log.exception("ペアリングで想定外のエラー")
                GLib.idle_add(self._pair_done, None, "想定外のエラー: %s" % e)
        threading.Thread(target=work, daemon=True).start()

    def _pair_done(self, name, err):
        self.pairing_in_progress = False
        self.code = ""
        self._render_code()
        if err:
            self._set_msg(err)
        else:
            self._set_msg("ペアリングしました: %s" % name)
            self.stack.set_visible_child_name("search")
            self._kick()
        return False

    # ---------------------------------------------------------- 操作
    def _set_msg(self, text):
        self.msg.set_text(text)

    def _operate(self, cmd):
        if self.busy:
            return
        label = {"quit": "終了", "restart": "再起動"}[cmd]
        st = self.status or {}
        if st.get("vrchat"):
            now = time.time()
            if not (self.armed and self.armed[0] == cmd and self.armed[1] > now):
                self.armed = (cmd, now + 6)
                self._refresh_buttons()
                self._set_msg("VRChat が起動中です。SteamVR を止めると VRChat も閉じることがあります。それでも「%s」するなら、もう一度押してください" % label)
                GLib.timeout_add_seconds(6, self._disarm)
                return
        self.armed = None
        self.busy = True
        self._refresh_buttons()
        self._set_msg("SteamVR を%sしています…(最大 1 分ほどかかります)" % label)

        def work():
            try:
                r, _pc = core.request_with_discovery(cmd, timeout=90)
                ok = r.get("ok")
                detail = r.get("detail", "")
                GLib.idle_add(self._op_done, cmd, ok, detail, r.get("steamvr"), None)
            except core.RemoteError as e:
                core.log.error("%s に失敗: %s", cmd, e)
                GLib.idle_add(self._op_done, cmd, False, "", None, str(e))
            except Exception as e:
                core.log.exception("操作で想定外のエラー")
                GLib.idle_add(self._op_done, cmd, False, "", None, "想定外のエラー: %s" % e)
        threading.Thread(target=work, daemon=True).start()

    def _disarm(self):
        if self.armed and self.armed[1] <= time.time():
            self.armed = None
            self._refresh_buttons()
        return False

    def _op_done(self, cmd, ok, detail, state, err):
        self.busy = False
        label = {"quit": "終了", "restart": "再起動"}[cmd]
        if err:
            self._set_msg(err)
        elif ok:
            self._set_msg("SteamVR を%sしました" % label if cmd == "quit" or state == "running" else
                          "SteamVR の起動を始めました。立ち上がるまで少し待ってください")
        else:
            self._set_msg("SteamVR の%sに失敗しました(%s)" % (label, detail))
        if state:
            (self.status or {}).update({"steamvr": state})
        self._refresh_buttons()
        self._kick()
        return False

    def _refresh_buttons(self):
        st = self.status or {}
        enabled = bool(st) and not self.busy
        self.btn_quit.set_sensitive(enabled)
        self.btn_restart.set_sensitive(enabled)
        for cmd, btn, normal, cls in (("quit", self.btn_quit, "SteamVR終了", "quit"),
                                      ("restart", self.btn_restart, "再起動", "restart")):
            armed = bool(self.armed and self.armed[0] == cmd and self.armed[1] > time.time())
            ctx = btn.get_style_context()
            if armed:
                ctx.remove_class(cls)
                ctx.add_class("armed")
                btn.set_label("もう一度押して実行")
            else:
                ctx.remove_class("armed")
                ctx.add_class(cls)
                btn.set_label(normal)

    # ---------------------------------------------------------- 状態の更新
    def _kick(self):
        self._wake.set()

    def start(self):
        self._wake = threading.Event()
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _on_destroy(self, *_):
        self.stop = True
        if hasattr(self, "_wake"):
            self._wake.set()
        Gtk.main_quit()

    def _poll_loop(self):
        while not self.stop:
            if not self.busy and not self.pairing_in_progress:
                try:
                    self._refresh_once()
                except Exception:
                    core.log.exception("状態の更新で想定外のエラー")
            self._wake.wait(4)
            self._wake.clear()

    def _refresh_once(self):
        cfg = core.load_config()
        if not core.is_paired(cfg):
            pc, pcs = core.find_pc(timeout=1.5)
            if pc is None:
                opening = [p for p in pcs if p.get("pairing")]
                pc = opening[0] if len(opening) == 1 else (pcs[0] if len(pcs) == 1 else None)
            GLib.idle_add(self._ui_unpaired, pc)
            return
        try:
            r, pc = core.request_with_discovery("status", timeout=6)
            GLib.idle_add(self._ui_status, r, pc)
        except core.RemoteError as e:
            if e.code in ("auth", "unpaired"):
                GLib.idle_add(self._ui_need_pair, str(e))
            else:
                GLib.idle_add(self._ui_offline, str(e))

    def _ui_unpaired(self, pc):
        self.pc = pc
        self.status = None
        if self.stack.get_visible_child_name() != "pair":
            self.stack.set_visible_child_name("pair")
        self.pair_pc.set_text("見つかった PC: %s" % pc.get("name") if pc else "PC を探しています… (PC のアプリが動いているか確認してください)")
        return False

    def _ui_need_pair(self, text):
        self.status = None
        self._set_msg(text)
        if self.stack.get_visible_child_name() != "pair":
            self._show_pair()
        self._set_msg(text)
        return False

    def _ui_offline(self, text):
        self.status = None
        if self.stack.get_visible_child_name() == "pair":
            return False
        self.stack.set_visible_child_name("search")
        self.search_label.set_text("PC につながりません")
        self.search_hint.set_text(text)
        self._refresh_buttons()
        return False

    def _ui_status(self, r, pc):
        self.pc = pc
        self.status = r
        if self.stack.get_visible_child_name() != "main":
            self.stack.set_visible_child_name("main")
        self.pc_label.set_text(r.get("name") or "PC")
        running = r.get("steamvr") == "running"
        self.state_label.set_text(("● SteamVR 起動中" if running else "○ SteamVR 停止中"))
        self.note_label.set_text("VRChat も起動中です(SteamVR を止めると閉じることがあります)" if r.get("vrchat") else "")
        self._refresh_buttons()
        return False


def install_css():
    prov = Gtk.CssProvider()
    prov.load_from_data(CSS)
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def main():
    install_css()
    app = App()
    app.show_all()
    app.start()
    Gtk.main()
    return 0
