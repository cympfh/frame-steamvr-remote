"""SteamVR のダッシュボードに出る「SteamVR リモート」パネル(OpenVR のオーバーレイ)。Frame で常駐する。

SteamVR が動いている間だけ動かす(systemd のユーザーユニットで SteamVR と一緒に起動・終了)。
PC の SteamVR の状態を出し、「SteamVR終了」「再起動」(どちらも 2 回押さないと実行しない)と、
ペアリング用の数字キーパッドを持つ。Frame と PC の SteamVR がつながっていなくても使える。
"""
import fcntl
import hashlib
import json
import os
import signal
import sys
import time

import fsr_cairo as cairo
import fsr_core as core
import fsr_model as model
import fsr_openvr as vr

OVERLAY_KEY = "dev.cympfh.frame-steamvr-remote.dashboard"
OVERLAY_NAME = "SteamVR リモート"
W, H = 1400, 900
PANEL_WIDTH_M = 2.2
EXIT_ALREADY_RUNNING = 3

HERE = os.path.dirname(os.path.realpath(__file__))
LOGO = os.path.join(HERE, "logo.png")
if not os.path.exists(LOGO):
    LOGO = os.path.join(HERE, "..", "assets", "logo.png")

NOTE = "Frame と PC の SteamVR がつながっていなくても使えます(同じ Wi-Fi/LAN にいれば足ります)"
KEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "消す", "0", "全消し"]


def snapshot(ctrl):
    """描画に要る状態を、ロックを持っている間にまとめて写し取る。"""
    with ctrl.lock:
        st = dict(ctrl.status) if ctrl.status else None
        snap = {
            "view": ctrl.view, "status": st, "pc": dict(ctrl.pc) if ctrl.pc else None, "msg": ctrl.msg,
            "code": ctrl.code, "busy": ctrl.busy, "pairing": ctrl.pairing, "offline_text": ctrl.offline_text,
            "offline_last": ctrl.offline_last, "enabled": ctrl.buttons_enabled(),
            "paired": core.is_paired(),
        }
        for cmd in model.OP_LABEL:
            snap["btn_" + cmd] = ctrl.button_label(cmd)
    return snap


class Button:
    def __init__(self, ident, x, y, w, h, enabled=True):
        self.id, self.x, self.y, self.w, self.h, self.enabled = ident, x, y, w, h, enabled

    def hit(self, px, py):
        return self.enabled and self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h


def _small_button(cv, buttons, ident, label, x, y, w, h, hover, pressed, enabled=True, size=28):
    buttons.append(Button(ident, x, y, w, h, enabled))
    cv.rrect(x, y, w, h, h / 2.0, "#ffffff", alpha=0.16 if enabled else 0.07)
    if enabled and hover == ident:
        cv.rrect(x, y, w, h, h / 2.0, "#ffffff", alpha=0.16)
    if enabled and pressed == ident:
        cv.rrect(x, y, w, h, h / 2.0, "#000000", alpha=0.22)
    cv.text(label, x + w / 2.0, y + h / 2.0 + size * 0.35, size, "#ffffff", True, "center", alpha=1.0 if enabled else 0.4)


def draw(snap, hover=None, pressed=None, cv=None):
    """パネル全体を描く。(Canvas, [Button]) を返す。"""
    own = cv is None
    cv = cv or cairo.Canvas(W, H)
    buttons = []
    cv.rect_gradient(0, 0, W, H, "#2A1B5E", "#0F0C29")

    # ヘッダー
    cv.image(LOGO, 48, 30, 96)
    cv.text("SteamVR リモート", 168, 82, 50, bold=True)
    cv.text("PC の SteamVR を Frame から止める・つなぎ直す", 170, 122, 24, "#b9b2e6")

    view = snap["view"]
    if view == "main":
        _draw_main(cv, buttons, snap, hover, pressed)
    elif view == "pair":
        _draw_pair(cv, buttons, snap, hover, pressed)
    else:
        _draw_search(cv, buttons, snap, hover, pressed)
    return cv, buttons


def _draw_message(cv, snap, y, maxlines=2):
    if snap["msg"]:
        cv.paragraph(snap["msg"], 70, y, 27, W - 140, "#ffe6a8", False, "center", max_lines=maxlines)


def _draw_main(cv, buttons, snap, hover, pressed):
    st = snap["status"] or {}
    # PC の名前と状態
    cv.rrect(60, 160, W - 120, 190, 36, "#ffffff", alpha=0.09)
    cv.text(st.get("name") or "PC", W / 2.0, 235, 58, bold=True, align="center")
    running = st.get("steamvr") == "running"
    cv.text("● SteamVR 起動中" if running else "○ SteamVR 停止中", W / 2.0, 292, 38,
            "#2BD9C4" if running else "#b9b2e6", True, "center")
    if st.get("vrchat"):
        cv.text("VRChat も起動中です(SteamVR を止めると閉じることがあります)", W / 2.0, 334, 24, "#ffe6a8", False, "center")
    cv.text("押すのは PC の SteamVR です。どちらのボタンも、2 回押すと実行します。", W / 2.0, 395, 25, "#d6d1f5", False, "center")
    # 大きなボタン
    bw, bh, by = 640, 280, 425
    for i, cmd in enumerate(("quit", "restart")):
        x = 60 + i * (bw + 40)
        label, armed = snap["btn_" + cmd]
        enabled = snap["enabled"]
        buttons.append(Button(cmd, x, by, bw, bh, enabled))
        if armed:
            grad, fg = ("#FFB84D", "#E07B00"), "#2b1600"
        elif cmd == "quit":
            grad, fg = ("#FF6FA6", "#D83A78"), "#ffffff"
        else:
            grad, fg = ("#35E6D0", "#1A9C8C"), "#08241f"
        a = 1.0 if enabled else 0.35
        cv.rrect(x, by + 10, bw, bh, 44, "#000000", alpha=0.35 * a)  # 影
        cv.rrect(x, by, bw, bh, 44, gradient=grad, alpha=a)
        if enabled and hover == cmd:
            cv.rrect(x, by, bw, bh, 44, "#ffffff", alpha=0.18)
        if enabled and pressed == cmd:
            cv.rrect(x, by, bw, bh, 44, "#000000", alpha=0.22)
        if armed:
            cv.text(label, x + bw / 2.0, by + bh / 2.0 + 18, 50, fg, True, "center")
            cv.text("5 秒たつと取り消されます", x + bw / 2.0, by + bh - 50, 26, fg, False, "center")
        else:
            cv.text(label, x + bw / 2.0, by + bh / 2.0 + 22, 62, fg, True, "center", alpha=a)
    _draw_message(cv, snap, 765)
    cv.text(NOTE, 60, 868, 21, "#a39ccf")
    _small_button(cv, buttons, "repair", "ペアリングし直す", W - 60 - 340, 828, 340, 62, hover, pressed)


def _draw_search(cv, buttons, snap, hover, pressed):
    offline = bool(snap["offline_text"]) and snap["paired"]
    cv.text("PC につながりません" if offline else "PC を探しています…", W / 2.0, 290, 52, bold=True, align="center")
    y = 350
    if offline:
        y = cv.paragraph(snap["offline_text"] + "\n確認すること: PC の電源 / PC のトレイに SteamVR リモートのアイコンがあるか / Frame と PC が同じ Wi-Fi か",
                         120, y, 26, W - 240, "#d6d1f5", False, "center")
        if snap["offline_last"]:
            cv.text(snap["offline_last"], W / 2.0, y + 14, 28, "#b9b2e6", False, "center")
    _draw_message(cv, snap, 640)
    _small_button(cv, buttons, "retry", "もう一度探す", W / 2.0 - 190, 700, 380, 76, hover, pressed, size=32)
    cv.text(NOTE, W / 2.0, 830, 22, "#a39ccf", False, "center")
    _small_button(cv, buttons, "repair", "ペアリングし直す", W - 60 - 340, 828, 340, 62, hover, pressed)


def _draw_pair(cv, buttons, snap, hover, pressed):
    cv.paragraph("PC のタスクトレイの SteamVR リモートのアイコンを右クリックして、「Frame とペアリングする」を選び、出てきた 6 桁のコードを入力してください。",
                 90, 190, 25, W - 180, "#d6d1f5", False, "center", max_lines=2)
    pc = snap["pc"]
    cv.text(("見つかった PC: %s" % pc.get("name")) if pc and pc.get("name") else
            ("PC が見つかりました" if pc else "PC を探しています…(PC のアプリが動いているか確認してください)"),
            W / 2.0, 292, 26, "#b9b2e6", False, "center")
    digits = list(snap["code"]) + ["・"] * (6 - len(snap["code"]))
    cv.text(" ".join(digits), W / 2.0, 375, 84, "#2BD9C4", True, "center")
    kw, kh, gx, gy = 220, 92, 22, 14
    x0 = (W - (3 * kw + 2 * gx)) / 2.0
    y0 = 400
    for i, k in enumerate(KEYS):
        x, y = x0 + (i % 3) * (kw + gx), y0 + (i // 3) * (kh + gy)
        ident = "key:" + k
        enabled = not snap["pairing"]
        buttons.append(Button(ident, x, y, kw, kh, enabled))
        cv.rrect(x, y, kw, kh, 26, "#ffffff", alpha=0.16 if enabled else 0.07)
        if enabled and hover == ident:
            cv.rrect(x, y, kw, kh, 26, "#ffffff", alpha=0.16)
        if enabled and pressed == ident:
            cv.rrect(x, y, kw, kh, 26, "#7C5CFF", alpha=0.9)
        cv.text(k, x + kw / 2.0, y + kh / 2.0 + (14 if len(k) > 1 else 18), 32 if len(k) > 1 else 52, "#ffffff", True, "center",
                alpha=1.0 if enabled else 0.4)
    _draw_message(cv, snap, 838, maxlines=1)
    if snap["paired"]:
        _small_button(cv, buttons, "back", "もどる", 60, 824, 220, 62, hover, pressed)


# ---------------------------------------------------------------- 入力
def to_panel_xy(x, y):
    """OpenVR のマウス座標(左下が原点・単位は SetOverlayMouseScale の W×H)を、画像の座標(左上が原点)にする。"""
    return x, H - y


def dispatch(ctrl, ident):
    """押されたボタンの処理。"""
    if ident in model.OP_LABEL:
        return ctrl.press_op(ident)
    if ident == "repair":
        ctrl.show_pair()
    elif ident == "retry":
        ctrl.kick()
    elif ident == "back":
        ctrl.back()
    elif ident.startswith("key:"):
        ctrl.key(ident[4:])
    return None


def hit_test(buttons, x, y):
    for b in reversed(buttons):
        if b.hit(x, y):
            return b.id
    return None


class Panel:
    """マウスの動き・押す・離す(コントローラーのレーザー)を、ボタンの「押した」に変える。

    もう一つの仕事は「いつ画像を OpenVR に送るか」を決めること。SetOverlayRaw は、呼ぶたびに
    直前の画像がいったん消えてから新しい画像が出る(Valve の開発者も「頻繁に呼ぶものではない」と言っている)ので、
    送る回数が多いと、そのまま画面のちらつきになる。そのため、次のときだけ送る:
      - 見える中身(状態・ボタンの文字・押している/指している場所)が、最後に送った画像と違うとき
      - 前に送ってから UPLOAD_MIN_INTERVAL 秒たっているとき(続けて変わっても、まとめて 1 回)
      - 指している場所だけが変わったときは、HOVER_SETTLE 秒そこに留まってから(手ぶれで境目を行き来しても送らない)
    """

    UPLOAD_MIN_INTERVAL = 0.25
    HOVER_SETTLE = 0.15
    STATS_EVERY = 60.0

    def __init__(self, ctrl, clock=time.monotonic):
        self.ctrl = ctrl
        self._clock = clock
        self.buttons = []
        self.hover = None
        self.pressed = None
        self.dirty = True
        self.rev_drawn = -1
        self.hover_since = 0.0
        self.last_upload = -1e9
        self.sent = None          # 最後に送った画像の控え: (状態の文字列, 指している所, 押している所, 画像の指紋)
        self.counts = {"upload": 0, "same": 0}   # 数える: 送った回数、中身が同じで送らなかった回数
        self.total = {"upload": 0, "same": 0}
        self._win_start = clock()
        self.vis_seconds = 0.0    # 数えている間に、ダッシュボードが見えていた秒数

    def on_mouse(self, kind, x, y):
        """x, y は左上が原点の px。"""
        ident = hit_test(self.buttons, x, y)
        if kind == "move":
            if ident != self.hover:
                self.hover = ident
                self.hover_since = self._clock()
                self.dirty = True
        elif kind == "down":
            self.pressed = ident
            if ident != self.hover:
                self.hover_since = self._clock()
            self.hover = ident
            self.dirty = True
        elif kind == "up":
            fired = self.pressed
            self.pressed = None
            self.dirty = True
            if fired and fired == ident:  # 押したボタンの上で離したときだけ「押した」
                core.log.info("パネルのボタン: %s", fired if not fired.startswith("key:") else "key")
                return dispatch(self.ctrl, fired)
        elif kind == "leave":
            if self.hover is not None or self.pressed is not None:
                self.hover_since = self._clock()
            self.hover = None
            self.pressed = None
            self.dirty = True
        return None

    # ------------------------------------------------------------ 描く・送るかどうか
    def needs_draw(self):
        return self.dirty or self.ctrl.rev != self.rev_drawn

    def render(self, snap=None):
        """いまの状態を描いて、RGBA の bytearray にする(送る・送らないは決めない)。"""
        self.dirty = False
        self.rev_drawn = self.ctrl.rev
        snap = snap if snap is not None else snapshot(self.ctrl)
        cv, self.buttons = draw(snap, self.hover, self.pressed)
        try:
            return cv.rgba_bytes()
        finally:
            cv.close()

    def next_frame(self):
        """いま送るべき画像があれば、その RGBA(完全に描き終えたもの)を返す。なければ None。

        送らない理由は 4 つ: 何も変わっていない / まだ早い(dirty のまま、間隔があいたら 1 回にまとめて送る) /
        指している場所だけが変わって、まだ落ち着いていない / 描いてみたら、最後に送った画像と全く同じだった。
        何も変わっていないときは、状態も見ずに None を返す(待機中の負荷はほぼゼロ)。
        """
        if not self.needs_draw():
            return None
        now = self._clock()
        if now - self.last_upload < self.UPLOAD_MIN_INTERVAL:
            return None
        rev = self.ctrl.rev
        snap = snapshot(self.ctrl)
        state = json.dumps(snap, sort_keys=True, ensure_ascii=False, default=str)
        if (self.sent is not None and state == self.sent[0] and self.pressed == self.sent[2]
                and self.hover != self.sent[1] and now - self.hover_since < self.HOVER_SETTLE):
            return None
        frame = self.render(snap)
        self.rev_drawn = rev
        digest = hashlib.blake2b(frame, digest_size=16).digest()
        if self.sent is not None and digest == self.sent[3]:
            self.counts["same"] += 1
            return None
        self.sent = (state, self.hover, self.pressed, digest)
        self.last_upload = now
        self.counts["upload"] += 1
        return frame

    def stats_line(self, now=None):
        """STATS_EVERY 秒ごとに 1 回、送った回数をログ用の 1 行にして返す(まだなら None)。数は 0 に戻す。"""
        now = self._clock() if now is None else now
        if now - self._win_start < self.STATS_EVERY:
            return None
        span = now - self._win_start
        c = self.counts
        line = None
        if c["upload"] or c["same"] or self.vis_seconds > 0:
            line = "パネルの画像送信: %d 秒間に %d 回(見た目が同じで省いた %d 回、ダッシュボードが見えていた %d 秒)" % (
                round(span), c["upload"], c["same"], round(self.vis_seconds))
        for k in c:
            self.total[k] += c[k]
        self.counts = {"upload": 0, "same": 0}
        self.vis_seconds = 0.0
        self._win_start = now
        return line


# ---------------------------------------------------------------- 常駐
def _lock():
    os.makedirs(core.STATE_DIR, exist_ok=True)
    fd = os.open(os.path.join(core.STATE_DIR, "overlay.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            pid = int(os.pread(fd, 32, 0).decode().strip() or 0)
            if pid > 1:
                os.kill(pid, signal.SIGUSR1)  # 動いているパネルに「ダッシュボードで開いて」と頼む
                core.log.info("すでに動いているパネル(pid %s)に、開くよう頼んだ", pid)
        except (OSError, ValueError):
            pass
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.pwrite(fd, str(os.getpid()).encode(), 0)
    return fd


def _connect(stop):
    """SteamVR につなぐ。動いていなければ待つ。"""
    ov = vr.Overlay()
    last = ""
    while not stop["flag"]:
        try:
            ov.init()
            return ov
        except vr.OpenVRError as e:
            if str(e) != last:
                core.log.info("SteamVR を待っています: %s", e)
                last = str(e)
            time.sleep(5)
    return None


def run():
    """パネルを作って常駐する。終了コード: 0=SteamVR が終わった(systemd が起動し直す)、3=すでに別のパネルが動いている。"""
    core.setup_logging(also_stderr=True)
    fd = _lock()
    if fd is None:
        return EXIT_ALREADY_RUNNING
    stop = {"flag": False, "open": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGUSR1, lambda *_: stop.update(open=True))
    ov = _connect(stop)
    if ov is None:
        return 0
    try:
        main, thumb = ov.create_dashboard(OVERLAY_KEY, OVERLAY_NAME)
    except vr.OpenVRError as e:
        core.log.error("パネルを作れません: %s", e)
        ov.shutdown()
        return 1
    core.log.info("パネルを作った(handle=%s thumbnail=%s)", main, thumb)
    try:
        ov.set_width(main, PANEL_WIDTH_M)
        ov.set_mouse(main, W, H)
        ov.set_flag(main, vr.FLAG_ENABLE_CLICK_STABILIZATION, True)
        if os.path.exists(LOGO):
            ov.set_from_file(thumb, os.path.realpath(LOGO))
    except vr.OpenVRError as e:
        core.log.error("パネルの設定に失敗: %s", e)

    ctrl = model.Controller()
    visible = {"v": False}
    ctrl.start(lambda: 4.0 if visible["v"] else 20.0)
    panel = Panel(ctrl)
    code = 0
    last_vis_check = 0.0
    last_loop = time.monotonic()
    try:
        while not stop["flag"]:
            now = time.monotonic()
            if now - last_vis_check > 0.5:
                last_vis_check = now
                v = ov.dashboard_visible()
                if v and not visible["v"]:
                    ctrl.kick()  # ダッシュボードを開いたら、すぐ最新の状態にする
                visible["v"] = v
            if stop["open"]:
                stop["open"] = False
                ov.show_dashboard(OVERLAY_KEY)
            for ev in ov.poll(main):
                t = ev.eventType
                if t in (vr.EV_QUIT, vr.EV_PROCESS_QUIT):
                    core.log.info("SteamVR から終了の知らせ(イベント %s)が来たので、パネルを閉じる(systemd が 5 秒後に起動し直す。SteamVR が本当に終わるときは、SteamVR と一緒に止まる)", t)
                    stop["flag"] = True
                elif t in (vr.EV_MOUSE_MOVE, vr.EV_MOUSE_DOWN, vr.EV_MOUSE_UP):
                    kind = {vr.EV_MOUSE_MOVE: "move", vr.EV_MOUSE_DOWN: "down", vr.EV_MOUSE_UP: "up"}[t]
                    if t != vr.EV_MOUSE_MOVE and ev.button != 1:
                        continue
                    panel.on_mouse(kind, *to_panel_xy(ev.x, ev.y))
                elif t == vr.EV_FOCUS_LEAVE:
                    panel.on_mouse("leave", -1, -1)
            ctrl.tick()
            if visible["v"] or panel.sent is None:
                frame = panel.next_frame()  # 見た目が変わったときだけ画像が返る(待機中は None)
                if frame is not None:
                    ov.set_raw(main, frame, W, H)
            if visible["v"]:
                panel.vis_seconds += now - last_loop
            last_loop = now
            line = panel.stats_line(now)
            if line:
                core.log.info(line)
            time.sleep(0.016 if visible["v"] else 0.25)
    except vr.OpenVRError as e:
        core.log.error("OpenVR のエラー: %s", e)
        code = 1
    except Exception:
        core.log.exception("パネルで想定外のエラー")
        code = 1
    finally:
        ctrl.stop()
        try:
            ov.destroy(main)
        except Exception:
            pass
        ov.shutdown()
        core.log.info("パネルを閉じた")
    return code


def render_preview(out_dir, scenario="all"):
    """開発用: OpenVR を使わず、いくつかの状態の見た目を PNG にする。"""
    os.makedirs(out_dir, exist_ok=True)
    status = {"name": "MOUSE5080", "steamvr": "running", "vrchat": True}
    base = {"view": "main", "status": status, "pc": {"name": "MOUSE5080", "ip": "192.168.11.24"}, "msg": "", "code": "",
            "busy": False, "pairing": False, "offline_text": "", "offline_last": "", "enabled": True, "paired": True,
            "btn_quit": ("SteamVR終了", False), "btn_restart": ("再起動", False)}
    shots = {
        "main": dict(base),
        "main-armed": dict(base, btn_quit=("もう一度押すと終了(4)", True),
                           msg="VRChat が起動中です。SteamVR を止めると VRChat も閉じることがあります。「SteamVR終了」をもう一度押すと実行します(5 秒以内)"),
        "main-stopped": dict(base, status={"name": "MOUSE5080", "steamvr": "stopped", "vrchat": False}),
        "search-offline": dict(base, view="search", status=None, offline_text="PC につながりません(PC のアプリが動いているか、Windows のファイアウォールを確認してください)",
                               offline_last="前につながった PC: MOUSE5080 (192.168.11.24)", enabled=False),
        "pair": dict(base, view="pair", status=None, code="482", enabled=False),
    }
    for name, snap in shots.items():
        cv, _b = draw(snap, hover="quit" if name == "main" else None)
        cv.save_png(os.path.join(out_dir, name + ".png"))
        cv.close()
    return list(shots)
