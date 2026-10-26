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


HOVER_BIG = ("#ffffff", 0.18)
HOVER_SMALL = ("#ffffff", 0.16)
PRESS_DARK = ("#000000", 0.22)
PRESS_KEY = ("#7C5CFF", 0.9)


class Button:
    """パネル上のボタン 1 つ。hover / press は「指している・押している」ときに重ねる色(色, 透明度)。
    label は、待機中の「もう一度押すと終了(5)」の文字を重ねる場所(待機中のボタンだけ持つ)。"""

    def __init__(self, ident, x, y, w, h, enabled=True, radius=0, hover=HOVER_SMALL, press=PRESS_DARK, label=None):
        self.id, self.x, self.y, self.w, self.h, self.enabled = ident, x, y, w, h, enabled
        self.radius, self.hover, self.press, self.label = radius, hover, press, label

    def hit(self, px, py):
        return self.enabled and self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h

    def highlight_key(self, style):
        """この形・この色の重ね画像の名前。同じ形のボタンは、同じ重ね画像を使い回す。"""
        return (int(self.w), int(self.h), int(self.radius), style[0], style[1])


def _tint(cv, b, hover, pressed, layered):
    """指している・押している色を、画像そのものに描く(layered のときは描かない。SteamVR 側の重ね画像が受け持つ)。"""
    if layered or not b.enabled:
        return
    if hover == b.id:
        cv.rrect(b.x, b.y, b.w, b.h, b.radius, b.hover[0], alpha=b.hover[1])
    if pressed == b.id:
        cv.rrect(b.x, b.y, b.w, b.h, b.radius, b.press[0], alpha=b.press[1])


def _small_button(cv, buttons, ident, label, x, y, w, h, hover, pressed, enabled=True, size=28, layered=False):
    b = Button(ident, x, y, w, h, enabled, radius=h / 2.0, hover=HOVER_SMALL, press=PRESS_DARK)
    buttons.append(b)
    cv.rrect(x, y, w, h, h / 2.0, "#ffffff", alpha=0.16 if enabled else 0.07)
    _tint(cv, b, hover, pressed, layered)
    cv.text(label, x + w / 2.0, y + h / 2.0 + size * 0.35, size, "#ffffff", True, "center", alpha=1.0 if enabled else 0.4)


def draw(snap, hover=None, pressed=None, cv=None, layered=False):
    """パネル全体を描く。(Canvas, [Button]) を返す。

    layered=True のときは、指している・押している色と、待機中のボタンの数字つきの文字を描かない
    (SteamVR 側で小さな画像を重ねて出す。これらが変わるたびに全体の画像を送り直すと、画面がちらつくため)。
    """
    cv = cv or cairo.Canvas(W, H)
    buttons = []
    cv.rect_gradient(0, 0, W, H, "#2A1B5E", "#0F0C29")

    # ヘッダー
    cv.image(LOGO, 48, 30, 96)
    cv.text("SteamVR リモート", 168, 82, 50, bold=True)
    cv.text("PC の SteamVR を Frame から止める・つなぎ直す", 170, 122, 24, "#b9b2e6")

    view = snap["view"]
    if view == "main":
        _draw_main(cv, buttons, snap, hover, pressed, layered)
    elif view == "pair":
        _draw_pair(cv, buttons, snap, hover, pressed, layered)
    else:
        _draw_search(cv, buttons, snap, hover, pressed, layered)
    return cv, buttons


def _draw_message(cv, snap, y, maxlines=2):
    if snap["msg"]:
        cv.paragraph(snap["msg"], 70, y, 27, W - 140, "#ffe6a8", False, "center", max_lines=maxlines)


def _draw_main(cv, buttons, snap, hover, pressed, layered):
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
        b = Button(cmd, x, by, bw, bh, enabled, radius=44, hover=HOVER_BIG, press=PRESS_DARK)
        buttons.append(b)
        if armed:
            grad, fg = ("#FFB84D", "#E07B00"), "#2b1600"
            # 数字つきの文字(毎秒変わる)が入る場所。layered のときは SteamVR 側で重ねる
            b.label = {"x": x + 4, "y": by + bh / 2.0 - 50, "w": bw - 8, "h": 100, "fg": fg, "size": 50, "baseline": 68}
        elif cmd == "quit":
            grad, fg = ("#FF6FA6", "#D83A78"), "#ffffff"
        else:
            grad, fg = ("#35E6D0", "#1A9C8C"), "#08241f"
        a = 1.0 if enabled else 0.35
        cv.rrect(x, by + 10, bw, bh, 44, "#000000", alpha=0.35 * a)  # 影
        cv.rrect(x, by, bw, bh, 44, gradient=grad, alpha=a)
        _tint(cv, b, hover, pressed, layered)
        if armed:
            if not layered:
                cv.text(label, x + bw / 2.0, by + bh / 2.0 + 18, 50, fg, True, "center")
            cv.text("5 秒たつと取り消されます", x + bw / 2.0, by + bh - 50, 26, fg, False, "center")
        else:
            cv.text(label, x + bw / 2.0, by + bh / 2.0 + 22, 62, fg, True, "center", alpha=a)
    _draw_message(cv, snap, 765)
    cv.text(NOTE, 60, 868, 21, "#a39ccf")
    _small_button(cv, buttons, "repair", "ペアリングし直す", W - 60 - 340, 828, 340, 62, hover, pressed, layered=layered)


def _draw_search(cv, buttons, snap, hover, pressed, layered):
    offline = bool(snap["offline_text"]) and snap["paired"]
    cv.text("PC につながりません" if offline else "PC を探しています…", W / 2.0, 290, 52, bold=True, align="center")
    y = 350
    if offline:
        y = cv.paragraph(snap["offline_text"] + "\n確認すること: PC の電源 / PC のトレイに SteamVR リモートのアイコンがあるか / Frame と PC が同じ Wi-Fi か",
                         120, y, 26, W - 240, "#d6d1f5", False, "center")
        if snap["offline_last"]:
            cv.text(snap["offline_last"], W / 2.0, y + 14, 28, "#b9b2e6", False, "center")
    _draw_message(cv, snap, 640)
    _small_button(cv, buttons, "retry", "もう一度探す", W / 2.0 - 190, 700, 380, 76, hover, pressed, size=32, layered=layered)
    cv.text(NOTE, W / 2.0, 830, 22, "#a39ccf", False, "center")
    _small_button(cv, buttons, "repair", "ペアリングし直す", W - 60 - 340, 828, 340, 62, hover, pressed, layered=layered)


def _draw_pair(cv, buttons, snap, hover, pressed, layered):
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
        b = Button(ident, x, y, kw, kh, enabled, radius=26, hover=HOVER_SMALL, press=PRESS_KEY)
        buttons.append(b)
        cv.rrect(x, y, kw, kh, 26, "#ffffff", alpha=0.16 if enabled else 0.07)
        _tint(cv, b, hover, pressed, layered)
        cv.text(k, x + kw / 2.0, y + kh / 2.0 + (14 if len(k) > 1 else 18), 32 if len(k) > 1 else 52, "#ffffff", True, "center",
                alpha=1.0 if enabled else 0.4)
    _draw_message(cv, snap, 838, maxlines=1)
    if snap["paired"]:
        _small_button(cv, buttons, "back", "もどる", 60, 824, 220, 62, hover, pressed, layered=layered)


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


# ---------------------------------------------------------------- 重ね画像の中身
def highlight_rgba(key):
    """指している・押している色の重ね画像(ボタンと同じ大きさ・角丸の単色)。key は Button.highlight_key()。"""
    w, h, radius, color, alpha = key
    cv = cairo.Canvas(w, h)
    try:
        cv.rrect(0, 0, w, h, radius, color, alpha=alpha)
        return cv.rgba_solid(color)
    finally:
        cv.close()


def label_rgba(spec, text):
    """待機中のボタンの文字(背景は透明)。spec は Button.label。"""
    w, h = int(spec["w"]), int(spec["h"])
    cv = cairo.Canvas(w, h)
    try:
        cv.text(text, w / 2.0, spec["baseline"], spec["size"], spec["fg"], True, "center")
        return cv.rgba_solid(spec["fg"])
    finally:
        cv.close()


class Panel:
    """マウスの動き・押す・離す(コントローラーのレーザー)を、ボタンの「押した」に変える。

    もう一つの仕事は「いつ全体の画像を OpenVR に送るか」を決めること。SetOverlayRaw は、呼ぶたびに
    直前の画像がいったん消えてから新しい画像が出る(Valve の開発者も「頻繁に呼ぶものではない」と言っている)ので、
    送るたびに画面が一瞬ちらつく。そのため、

      - 指している・押している色 → 画像に描かず、SteamVR 側の小さな重ね画像(サブビュー)を出し入れする(highlights())
      - 待機中の「もう一度押すと終了(5)」の数字 → 同じく、文字だけの重ね画像を入れ替える(label_specs())
      - 全体の画像 → 状態そのもの(PC の状態・ボタンの色・メッセージ・画面の切り替え)が変わったときだけ送る(next_frame())

    にしている。指す・動かす・数字が減る、では全体の画像は送らない。
    重ねる機能が使えない SteamVR のときは layered=False にして、色は押している間だけ全体の画像に描く(指している色は出さない)。
    """

    UPLOAD_MIN_INTERVAL = 0.25
    STATS_EVERY = 60.0

    def __init__(self, ctrl, clock=time.monotonic, layered=True):
        self.ctrl = ctrl
        self._clock = clock
        self.layered = layered
        self.buttons = []
        self.hover = None
        self.pressed = None
        self._last_label = {}
        self.dirty = True
        self.rev_drawn = -1
        self.last_upload = -1e9
        self.sent = None          # 最後に送った全体の画像の控え: (状態の文字列, 画像の指紋)
        self.counts = {"upload": 0, "same": 0}   # 数える: 送った回数、中身が同じで送らなかった回数
        self.total = {"upload": 0, "same": 0}
        self._win_start = clock()
        self.vis_seconds = 0.0    # 数えている間に、ダッシュボードが見えていた秒数

    def set_layered(self, on):
        if on != self.layered:
            self.layered = on
            self.dirty = True

    def on_mouse(self, kind, x, y):
        """x, y は左上が原点の px。"""
        ident = hit_test(self.buttons, x, y)
        if kind == "move":
            self.hover = ident
        elif kind == "down":
            self.pressed = ident
            self.hover = ident
            self._pressed_changed()
        elif kind == "up":
            fired = self.pressed
            self.pressed = None
            self._pressed_changed()
            if fired and fired == ident:  # 押したボタンの上で離したときだけ「押した」
                core.log.info("パネルのボタン: %s", fired if not fired.startswith("key:") else "key")
                return dispatch(self.ctrl, fired)
        elif kind == "leave":
            had = self.pressed is not None
            self.hover = None
            self.pressed = None
            if had:
                self._pressed_changed()
        return None

    def _pressed_changed(self):
        if not self.layered:   # 重ね画像が使えないときだけ、押している色を全体の画像に描く
            self.dirty = True

    # ------------------------------------------------------------ 重ねる画像
    def highlights(self):
        """いま重ねる色: {重ね画像の名前: (x, y)}。指しているボタンの色と、押しているボタンの色。"""
        if not self.layered:
            return {}
        out = {}
        by_id = {b.id: b for b in self.buttons}
        hb = by_id.get(self.hover)
        if hb is not None and hb.enabled:
            out[hb.highlight_key(hb.hover)] = (hb.x, hb.y)
        pb = by_id.get(self.pressed)
        if pb is not None and pb.enabled:
            out[pb.highlight_key(pb.press)] = (pb.x, pb.y)
        return out

    def label_specs(self):
        """待機中のボタンに重ねる文字: {ボタン名: {重ねる場所の情報..., "text": いまの文字}}。待機中のボタンが無ければ空。

        全体の画像が待機の見た目(橙色のボタン)になっている間だけ出す。待機が終わった直後は、全体の画像が戻るまでの
        ほんの少しの間、最後の文字を出したままにする(文字の無い橙色のボタンが見えないように)。
        """
        if not self.layered:
            return {}
        out = {}
        for b in self.buttons:
            if b.label:
                text, armed = self.ctrl.button_label(b.id)
                spec = dict(b.label)
                spec["text"] = text if armed else self._last_label.get(b.id, text)
                if armed:
                    self._last_label[b.id] = text
                out[b.id] = spec
        return out

    # ------------------------------------------------------------ 描く・送るかどうか
    def needs_draw(self):
        return self.dirty or self.ctrl.rev != self.rev_drawn

    def base_state(self, snap):
        """全体の画像の中身を決める状態(これが前と同じなら、画像も同じ)。

        待機中の数字は重ね画像のほうで出すので、ここには入れない(入れると毎秒送り直しになる)。
        """
        d = dict(snap)
        if self.layered:
            for cmd in model.OP_LABEL:
                label, armed = d["btn_" + cmd]
                d["btn_" + cmd] = ("", armed)
        else:
            d["_pressed"] = self.pressed
        d["_layered"] = self.layered
        return json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)

    def render(self, snap=None):
        """いまの状態を描いて、RGBA の bytearray にする(送る・送らないは決めない)。"""
        self.dirty = False
        self.rev_drawn = self.ctrl.rev
        snap = snap if snap is not None else snapshot(self.ctrl)
        cv, self.buttons = draw(snap, None, None if self.layered else self.pressed, layered=self.layered)
        try:
            return cv.rgba_bytes()
        finally:
            cv.close()

    def next_frame(self):
        """いま送るべき全体の画像があれば、その RGBA(完全に描き終えたもの)を返す。なければ None。

        送らない理由は 3 つ: 何も変わっていない / まだ早い(dirty のまま、間隔があいたら 1 回にまとめて送る) /
        状態も画像も、最後に送ったものと全く同じ。何も変わっていないときは、状態も見ずに None を返す(待機中の負荷はほぼゼロ)。
        """
        if not self.needs_draw():
            return None
        now = self._clock()
        if now - self.last_upload < self.UPLOAD_MIN_INTERVAL:
            return None
        rev = self.ctrl.rev
        snap = snapshot(self.ctrl)
        state = self.base_state(snap)
        if self.sent is not None and state == self.sent[0]:
            self.rev_drawn = rev
            self.dirty = False
            self.counts["same"] += 1
            return None
        frame = self.render(snap)
        self.rev_drawn = rev
        digest = hashlib.blake2b(frame, digest_size=16).digest()
        if self.sent is not None and digest == self.sent[1]:
            self.sent = (state, digest)
            self.counts["same"] += 1
            return None
        self.sent = (state, digest)
        self.last_upload = now
        self.counts["upload"] += 1
        return frame

    def stats_line(self, now=None, extra=None):
        """STATS_EVERY 秒ごとに 1 回、送った回数をログ用の 1 行にして返す(まだなら None)。数は 0 に戻す。"""
        now = self._clock() if now is None else now
        if now - self._win_start < self.STATS_EVERY:
            return None
        span = now - self._win_start
        c = self.counts
        line = None
        if c["upload"] or c["same"] or self.vis_seconds > 0:
            extra = extra() if callable(extra) else (extra or "")
            line = "パネルの画像送信: %d 秒間に 全体 %d 回(見た目が同じで省いた %d 回、ダッシュボードが見えていた %d 秒)%s" % (
                round(span), c["upload"], c["same"], round(self.vis_seconds), extra)
        for k in c:
            self.total[k] += c[k]
        self.counts = {"upload": 0, "same": 0}
        self.vis_seconds = 0.0
        self._win_start = now
        return line


class Layers:
    """SteamVR 側の重ね画像(サブビュー)。パネルの上に小さな画像を重ね、位置と表示だけを切り替える。

    重ね画像の中身は、できたときと中身が変わるときにだけ送る(指す・動かすでは送らない)。
      - 色: 形・色ごとに 1 枚。指す・押すたびに、位置を動かして出す/隠すだけ。
      - 文字: 待機中のボタンごとに 2 枚(A/B)。数字が変わったら、隠れているほうに新しい文字を送って、
        読み込みが終わってから出し、そのあとで古いほうを隠す。見えている画像には、送らない。
    """

    LOAD_WAIT = 0.12     # 画像を送ってから出すまでの待ち(OpenVR は画像の読み込みが非同期)
    RETIRE_WAIT = 0.06   # 新しい文字を出してから古い文字を隠すまでの重なり

    def __init__(self, ov, parent, key_prefix=OVERLAY_KEY, clock=time.monotonic):
        self.ov, self.parent, self.prefix, self._clock = ov, parent, key_prefix, clock
        self.hl = {}        # 重ね画像の名前 -> {"h", "pos", "shown"}
        self.labels = {}    # ボタン名 -> {"bufs": [..], "texts": [..], "front", "pending", "retire"}
        self.counts = {"image": 0, "move": 0, "vis": 0}
        self.failed = None  # 失敗したときの理由(あれば、重ねるのをやめて全体の画像だけにする)
        self._n = 0
        self.by_alpha = False

    # ---- 下請け
    def _new(self, what):
        self._n += 1
        return self.ov.create_subview(self.parent, "%s.%s%d" % (self.prefix, what, self._n), "SteamVR リモート %s" % what)

    def _upload(self, handle, rgba, w, h):
        self.ov.set_raw(handle, rgba, int(w), int(h))
        self.counts["image"] += 1

    def _vis(self, handle, on):
        if self.by_alpha:
            self.ov.set_alpha(handle, 1.0 if on else 0.0)
        else:
            try:
                (self.ov.show if on else self.ov.hide)(handle)
            except vr.OpenVRError:
                if not self.ov.has_alpha:
                    raise
                self.by_alpha = True   # ShowOverlay が子に効かないときは、透明度で出し入れする
                core.log.info("重ね画像の表示は、ShowOverlay ではなく透明度で切り替えます")
                self.ov.set_alpha(handle, 1.0 if on else 0.0)
        self.counts["vis"] += 1

    def _move(self, rec, x, y):
        if rec["pos"] != (x, y):
            self.ov.set_subview_position(rec["h"], x, y)
            rec["pos"] = (x, y)
            self.counts["move"] += 1

    # ---- 使えるかの確認
    def probe(self):
        """重ねる機能が使えるかを、見えない 1 画素の画像で試す。使えなければ理由を返す(使えるなら None)。"""
        try:
            if not self.ov.has_subview:
                return "このバージョンの SteamVR には、サブビュー(IVROverlay_028)がありません"
            h = self.ov.create_subview(self.parent, self.prefix + ".probe", "SteamVR リモート probe")
            try:
                self.ov.set_raw(h, bytearray(4), 1, 1)
                self.ov.set_subview_position(h, 0, 0)
                self._vis(h, True)
                self._vis(h, False)
            finally:
                self.ov.destroy(h)
            return None
        except vr.OpenVRError as e:
            return str(e)

    # ---- 色
    def prepare(self, buttons):
        """ボタンに使う色の重ね画像を、先に作って送っておく(隠したまま)。指すときは、位置を動かして出すだけにするため。"""
        for b in buttons:
            if not b.enabled:
                continue
            for style in (b.hover, b.press):
                key = b.highlight_key(style)
                if key not in self.hl:
                    h = self._new("hl")
                    self._upload(h, highlight_rgba(key), key[0], key[1])
                    self.hl[key] = {"h": h, "pos": None, "shown": False}

    def sync_highlights(self, wanted):
        for key, rec in self.hl.items():
            if rec["shown"] and key not in wanted:
                self._vis(rec["h"], False)
                rec["shown"] = False
        for key, (x, y) in wanted.items():
            rec = self.hl.get(key)
            if rec is None:
                continue   # prepare() がまだ(全体の画像を送った直後)。次の周回で出す
            self._move(rec, x, y)
            if not rec["shown"]:
                self._vis(rec["h"], True)
                rec["shown"] = True

    # ---- 文字
    def sync_labels(self, specs, now=None):
        now = self._clock() if now is None else now
        for cmd in list(set(self.labels) | set(specs)):
            st = self.labels.setdefault(cmd, {"bufs": [None, None], "pos": [None, None], "texts": [None, None],
                                              "front": None, "pending": None, "retire": None})
            spec = specs.get(cmd)
            if spec is None:
                self._label_clear(st)
                continue
            text = spec["text"]
            pend = st["pending"]
            if pend is not None:
                if st["texts"][pend[0]] == text:
                    pass
                else:
                    self._label_load(st, pend[0], spec, now)    # 読み込み待ちのうちに、また変わった
            elif st["front"] is not None and st["texts"][st["front"]] == text:
                pass
            else:
                back = 0 if st["front"] is None else 1 - st["front"]
                self._label_load(st, back, spec, now)
        self.tick_labels(now)

    def _label_load(self, st, idx, spec, now):
        if st["bufs"][idx] is None:
            st["bufs"][idx] = self._new("label")
            st["texts"][idx] = None
        h = st["bufs"][idx]
        pos = (spec["x"], spec["y"])
        if st["texts"][idx] != spec["text"]:
            self._upload(h, label_rgba(spec, spec["text"]), spec["w"], spec["h"])
            st["texts"][idx] = spec["text"]
        if st["pos"][idx] != pos:
            self.ov.set_subview_position(h, *pos)
            st["pos"][idx] = pos
            self.counts["move"] += 1
        st["pending"] = (idx, now + self.LOAD_WAIT)

    def tick_labels(self, now):
        for st in self.labels.values():
            pend = st["pending"]
            if pend is not None and now >= pend[1]:
                idx = pend[0]
                self._vis(st["bufs"][idx], True)
                old = st["front"]
                st["front"], st["pending"] = idx, None
                if old is not None and old != idx:
                    st["retire"] = (old, now + self.RETIRE_WAIT)
            ret = st["retire"]
            if ret is not None and now >= ret[1]:
                self._vis(st["bufs"][ret[0]], False)
                st["retire"] = None

    def _label_clear(self, st):
        for idx in (0, 1):
            if st["bufs"][idx] is not None and (st["front"] == idx or (st["pending"] and st["pending"][0] == idx)
                                                or (st["retire"] and st["retire"][0] == idx)):
                self._vis(st["bufs"][idx], False)
        st["front"] = st["pending"] = st["retire"] = None

    def visible_label(self, cmd):
        """(確認・テスト用)いま出ている文字。"""
        st = self.labels.get(cmd)
        if not st or st["front"] is None:
            return None
        return st["texts"][st["front"]]

    def sync(self, panel, now=None):
        now = self._clock() if now is None else now
        self.sync_highlights(panel.highlights())
        self.sync_labels(panel.label_specs(), now)

    def stats(self):
        c = self.counts
        s = "、重ね画像の送信 %d 回(位置の変更 %d 回、出す・隠す %d 回)" % (c["image"], c["move"], c["vis"])
        for k in c:
            c[k] = 0
        return s

    def hide_all(self):
        for rec in self.hl.values():
            if rec["shown"]:
                try:
                    self._vis(rec["h"], False)
                except Exception:
                    pass
                rec["shown"] = False
        for st in self.labels.values():
            try:
                self._label_clear(st)
            except Exception:
                pass


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
    # 指す・押す・待機の数字は、全体の画像を送り直さず、SteamVR 側の重ね画像(サブビュー)で出す。使えなければ全体の画像だけにする
    layers = Layers(ov, main)
    why = layers.probe()
    if why:
        core.log.warning("重ね画像(サブビュー)が使えないので、指している色は出しません。使えない理由: %s", why)
        layers = None
    else:
        core.log.info("重ね画像(サブビュー)が使えます(%s)。指す・押す・待機の数字では、全体の画像を送り直しません", ov.version)
    panel = Panel(ctrl, layered=layers is not None)
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
                if layers is not None:
                    try:
                        if frame is not None:
                            layers.prepare(panel.buttons)   # このあと使う色の重ね画像を、隠したまま先に送っておく
                        layers.sync(panel, now)
                    except vr.OpenVRError as e:
                        # 重ね画像が途中で使えなくなった: 重ねるのをやめて、全体の画像だけにする(パネルは落とさない)
                        core.log.warning("重ね画像でエラー。以後は重ねずに、全体の画像だけで出します: %s", e)
                        layers.hide_all()
                        layers = None
                        panel.set_layered(False)
            if visible["v"]:
                panel.vis_seconds += now - last_loop
            last_loop = now
            line = panel.stats_line(now, layers.stats if layers is not None else None)
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
        if layers is not None:
            layers.hide_all()
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
