"""OpenVR のパネルの、描画の配置・マウス入力・関数表の並びのテスト。SteamVR には つながない。"""
import os
import re
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="fsr-test-ov-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")
os.environ["XDG_STATE_HOME"] = os.path.join(_TMP, "state")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "frame"))
sys.path.insert(0, os.path.dirname(__file__))

import fsr_arm as arm  # noqa: E402
import fsr_cairo  # noqa: E402
import fsr_openvr as vr  # noqa: E402
import fsr_overlay as ov  # noqa: E402
from test_arm import Clock, FakeCore  # noqa: E402
import fsr_model as model  # noqa: E402

try:
    fsr_cairo.lib()
    HAVE_CAIRO = True
except OSError:
    HAVE_CAIRO = False


class OpenvrTableTest(unittest.TestCase):
    def test_fn_orders_are_sane(self):
        for ver, order, n in (("IVROverlay_027", vr.FN_ORDER_027, 80), ("IVROverlay_028", vr.FN_ORDER_028, 82)):
            self.assertEqual(len(order), n, ver)
            self.assertEqual(len(set(order)), n, ver)
            self.assertEqual(order[0], "FindOverlay")
            self.assertEqual(order[-1], "CloseMessageOverlay")
            self.assertIs(vr.FN_ORDERS[ver], order)
        # 028 は 027 に 2 つ足しただけ(並びが版で違うことの確認)
        self.assertEqual(sorted(set(vr.FN_ORDER_028) - set(vr.FN_ORDER_027)), ["CreateSubviewOverlay", "SetSubviewPosition"])
        self.assertEqual(vr.FN_ORDER_028.index("CreateDashboardOverlay"), vr.FN_ORDER_027.index("CreateDashboardOverlay") + 2)
        self.assertEqual(vr.OVERLAY_VERSIONS[0], "IVROverlay_028")

    def test_fn_order_027_matches_header_if_present(self):
        for h in (os.environ.get("OPENVR_CAPI_H", ""), "/opt/steamvr/tools/hellovr_vulkan_linux/src/openvr/headers/openvr_capi.h"):
            if h and os.path.exists(h):
                with open(h) as f:
                    t = f.read()
                if 'IVROverlay_Version = "IVROverlay_027"' not in t:
                    self.skipTest("このヘッダーは 027 ではない")
                m = re.search(r"struct VR_IVROverlay_FnTable\s*\{(.*?)\n\};", t, re.S)
                names = re.findall(r"\(OPENVR_FNTABLE_CALLTYPE \*(\w+)\)", m.group(1))
                self.assertEqual(names, vr.FN_ORDER_027)
                return
        self.skipTest("openvr_capi.h が無い(Frame の SteamVR にはある)")

    def test_event_layout(self):
        import ctypes
        self.assertEqual(ctypes.sizeof(vr.VREvent), 60)
        self.assertEqual(vr.VREvent.x.offset, 12)
        self.assertEqual(vr.VREvent.y.offset, 16)
        self.assertEqual(vr.VREvent.button.offset, 20)


class InputTest(unittest.TestCase):
    def test_mouse_y_is_flipped(self):
        self.assertEqual(ov.to_panel_xy(10, 0), (10, ov.H))
        self.assertEqual(ov.to_panel_xy(10, ov.H), (10, 0))


@unittest.skipUnless(HAVE_CAIRO, "libcairo が無い")
class PanelTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.core = FakeCore()
        self.ctrl = model.Controller(core_mod=self.core, clock=self.clock, run_async=False)
        self.ctrl.refresh_once()
        self.panel = ov.Panel(self.ctrl)
        self.panel.render()
        self.by_id = {b.id: b for b in self.panel.buttons}

    def center(self, ident):
        b = self.by_id[ident]
        return b.x + b.w / 2, b.y + b.h / 2

    def click(self, ident, hold=0.0):
        x, y = self.center(ident)
        self.panel.on_mouse("move", x, y)
        self.panel.on_mouse("down", x, y)
        self.clock.advance(hold)
        return self.panel.on_mouse("up", x, y)

    def test_buttons_exist_and_fit(self):
        self.assertIn("quit", self.by_id)
        self.assertIn("restart", self.by_id)
        for b in self.panel.buttons:
            self.assertTrue(0 <= b.x and b.x + b.w <= ov.W and 0 <= b.y and b.y + b.h <= ov.H, b.id)
        q, r = self.by_id["quit"], self.by_id["restart"]
        self.assertLess(q.x + q.w, r.x)  # 重ならない

    def test_two_clicks_needed(self):
        self.assertEqual(self.click("quit"), arm.ARMED)
        self.assertEqual(self.core.ops, [])
        self.panel.render()
        self.clock.advance(1.0)
        self.assertEqual(self.click("quit"), arm.FIRE)
        self.assertEqual(self.core.ops, ["quit"])

    def test_release_elsewhere_is_not_a_click(self):
        qx, qy = self.center("quit")
        rx, ry = self.center("restart")
        self.panel.on_mouse("down", qx, qy)
        self.assertIsNone(self.panel.on_mouse("up", rx, ry))
        self.assertIsNone(self.ctrl.arm.armed())

    def test_hover_alone_does_nothing(self):
        x, y = self.center("quit")
        self.panel.on_mouse("move", x, y)
        self.assertIsNone(self.ctrl.arm.armed())
        self.assertEqual(self.core.ops, [])

    def test_leave_cancels_press(self):
        x, y = self.center("quit")
        self.panel.on_mouse("down", x, y)
        self.panel.on_mouse("leave", -1, -1)
        self.assertIsNone(self.panel.on_mouse("up", x, y))
        self.assertIsNone(self.ctrl.arm.armed())

    def test_armed_label_is_drawn_and_disabled_buttons_are_not_clickable(self):
        self.click("quit")
        self.assertTrue(self.panel.needs_draw())
        self.panel.render()
        self.ctrl.status = None  # PC につながらなくなった
        self.panel.render()
        self.assertNotIn("quit", [b.id for b in self.panel.buttons if b.enabled])

    def test_keypad_enters_digits(self):
        self.ctrl.show_pair()
        self.panel.render()
        self.by_id = {b.id: b for b in self.panel.buttons}
        for k in ("1", "2", "3"):
            self.click("key:" + k)
        self.assertEqual(self.ctrl.code, "123")
        self.click("key:消す")
        self.assertEqual(self.ctrl.code, "12")
        self.click("key:全消し")
        self.assertEqual(self.ctrl.code, "")

    def test_preview_renders_all_screens(self):
        out = tempfile.mkdtemp(prefix="fsr-prev-")
        for name in ov.render_preview(out):
            self.assertGreater(os.path.getsize(os.path.join(out, name + ".png")), 10000)


@unittest.skipUnless(HAVE_CAIRO, "libcairo が無い")
class UploadGateTest(unittest.TestCase):
    """ちらつき対策: 見た目が変わらないかぎり、画像を OpenVR に送らない(SetOverlayRaw は呼ぶたびに前の画像がいったん消える)。"""

    def setUp(self):
        self.clock = Clock()
        self.core = FakeCore()
        self.ctrl = model.Controller(core_mod=self.core, clock=self.clock, run_async=False)
        self.ctrl.refresh_once()
        self.panel = ov.Panel(self.ctrl, clock=self.clock)
        self.frames = []
        self.assertIsNotNone(self.pump())  # 最初の 1 枚

    def pump(self):
        """描画ループの 1 回分(送る画像があれば記録して返す)。"""
        self.ctrl.tick()
        f = self.panel.next_frame()
        if f is not None:
            self.frames.append((self.clock(), f))
        return f

    def run_for(self, seconds, step=0.016):
        n = 0
        end = self.clock() + seconds
        while self.clock() < end:
            self.clock.advance(step)
            if self.pump() is not None:
                n += 1
        return n

    def center(self, ident):
        b = {b.id: b for b in self.panel.buttons}[ident]
        return b.x + b.w / 2, b.y + b.h / 2

    def test_first_frame_is_complete_and_sized(self):
        self.assertEqual(len(self.frames[0][1]), ov.W * ov.H * 4)
        self.assertEqual(self.panel.counts["upload"], 1)

    def test_idle_uploads_nothing(self):
        self.assertEqual(self.run_for(120), 0)
        self.assertEqual(self.panel.counts["upload"], 1)

    def test_idle_does_not_even_render(self):
        calls = []
        orig = self.panel.render
        self.panel.render = lambda *a, **k: calls.append(1) or orig(*a, **k)
        self.run_for(30)
        self.assertEqual(calls, [])

    def test_polling_same_status_does_not_bump_rev_or_upload(self):
        rev = self.ctrl.rev
        for _ in range(10):  # 4 秒ごとの状態確認(中身は同じ)
            self.clock.advance(4.0)
            self.ctrl.refresh_once()
            self.assertIsNone(self.pump())
        self.assertEqual(self.ctrl.rev, rev)
        self.assertEqual(self.panel.counts["upload"], 1)

    def test_status_change_uploads_once(self):
        self.core.vrchat = True
        self.clock.advance(4.0)
        self.ctrl.refresh_once()
        self.assertEqual(self.run_for(2), 1)
        self.assertEqual(self.run_for(10), 0)

    def test_rev_bump_with_identical_picture_is_skipped(self):
        self.ctrl._changed()
        self.clock.advance(1)
        self.assertIsNone(self.pump())
        self.assertEqual(self.panel.counts["same"], 1)
        self.assertFalse(self.panel.needs_draw())  # 見た目が同じだと分かったら、もう描き直さない
        self.assertEqual(self.run_for(5), 0)

    def test_hover_never_uploads_the_full_image(self):
        """境目をカーソルで行き来しても、全体の画像は 1 枚も送らない(色は SteamVR 側の重ね画像が受け持つ)。"""
        qx, qy = self.center("quit")
        rx, ry = self.center("restart")
        n = 0
        for i in range(400):
            self.clock.advance(0.016)
            x, y = [(qx, qy), (rx, ry), (-5, -5), (qx + 300, qy), (rx - 300, ry)][i % 5]
            self.panel.on_mouse("move", x, y)
            if self.pump() is not None:
                n += 1
        self.assertEqual(n, 0)
        self.assertEqual(self.panel.counts["upload"], 1)
        self.assertEqual(self.panel.counts["same"], 0)  # 描き直しすらしない

    def test_press_and_release_do_not_upload_in_layered_mode(self):
        qx, qy = self.center("quit")
        self.clock.advance(1)
        self.panel.on_mouse("move", qx, qy)
        self.panel.on_mouse("down", qx, qy)
        self.assertEqual(self.run_for(1), 0)
        self.panel.on_mouse("leave", -1, -1)  # 押したまま外れる
        self.assertEqual(self.run_for(1), 0)

    def test_uploads_are_at_least_min_interval_apart(self):
        # 本当に中身が変わるとき(PC の状態が行き来する)でも、送る間隔は 0.25 秒以上
        for i in range(60):
            self.clock.advance(0.02)
            self.ctrl.status = dict(self.ctrl.status, steamvr="stopped" if i % 2 else "running")
            self.ctrl._changed()
            self.pump()
        times = [t for t, _ in self.frames]
        self.assertGreater(len(times), 2)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= 0.25 - 1e-9 for g in gaps), min(gaps))

    def test_arm_countdown_does_not_upload_the_full_image_each_second(self):
        qx, qy = self.center("quit")
        self.clock.advance(1)
        self.panel.on_mouse("move", qx, qy)
        self.panel.on_mouse("down", qx, qy)
        self.assertEqual(self.panel.on_mouse("up", qx, qy), arm.ARMED)
        # 待機の見た目(橙色のボタンとメッセージ)に変わるときの 1 回だけ
        self.assertEqual(self.run_for(0.6), 1)
        t_arm = self.clock()
        n = self.run_for(6)   # 数字が 4,3,2,1 と進む間は送らない。5 秒たって待機が外れるとき(元の見た目に戻る)に 1 回
        self.assertEqual(n, 1)
        self.assertEqual(self.run_for(10), 0)  # そのあとは静か
        self.assertGreater(self.clock() - t_arm, 5)

    def test_stats_line(self):
        self.assertIsNone(self.panel.stats_line())
        self.clock.advance(61)
        line = self.panel.stats_line()
        self.assertIn("1 回", line)
        self.assertEqual(self.panel.total["upload"], 1)
        self.clock.advance(61)
        self.assertIsNone(self.panel.stats_line())  # 静かな 1 分は、ダッシュボードが見えていなければ何も出さない
        self.clock.advance(61)
        self.panel.vis_seconds = 30
        self.assertIn("0 回", self.panel.stats_line())  # 見えているのに 0 回、は書く(待機中の確認ができる)


class FakeOverlay:
    """OpenVR の代わり。呼ばれたことを記録する(SteamVR には何もしない)。"""

    version = "IVROverlay_028"
    has_alpha = True

    def __init__(self, subview=True, show_ok=True):
        self.has_subview = subview
        self.show_ok = show_ok
        self.next = 100
        self.raw = []           # (handle, 幅, 高さ)
        self.pos = {}
        self.visible = set()
        self.alpha = {}
        self.destroyed = []
        self.calls = {"show": 0, "hide": 0, "move": 0}

    def create_subview(self, parent, key, name):
        if not self.has_subview:
            raise vr.OpenVRError("CreateSubviewOverlay が使えません")
        self.next += 1
        return self.next

    def set_raw(self, h, buf, w, hh):
        assert len(buf) == w * hh * 4
        self.raw.append((h, w, hh))

    def set_subview_position(self, h, x, y):
        self.pos[h] = (x, y)
        self.calls["move"] += 1

    def show(self, h):
        if not self.show_ok:
            raise vr.OpenVRError("ShowOverlay: 失敗")
        self.visible.add(h)
        self.calls["show"] += 1

    def hide(self, h):
        if not self.show_ok:
            raise vr.OpenVRError("HideOverlay: 失敗")
        self.visible.discard(h)
        self.calls["hide"] += 1

    def set_alpha(self, h, a):
        self.alpha[h] = a
        (self.visible.add if a > 0 else self.visible.discard)(h)

    def destroy(self, h):
        self.destroyed.append(h)


@unittest.skipUnless(HAVE_CAIRO, "libcairo が無い")
class LayersTest(unittest.TestCase):
    """ちらつき対策の本体: 指す・押す・数字が減る、で全体の画像も重ね画像の中身も送らない。"""

    MAIN = 7

    def setUp(self):
        self.clock = Clock()
        self.core = FakeCore()
        self.ctrl = model.Controller(core_mod=self.core, clock=self.clock, run_async=False)
        self.ctrl.refresh_once()
        self.fake = FakeOverlay()
        self.layers = ov.Layers(self.fake, self.MAIN, clock=self.clock)
        self.assertIsNone(self.layers.probe())
        self.fake.raw.clear()
        self.panel = ov.Panel(self.ctrl, clock=self.clock)
        self.fulls = 0
        self.step(0.0)

    def step(self, dt=0.016):
        """run() の 1 周と同じ流れ。"""
        self.clock.advance(dt)
        self.ctrl.tick()
        frame = self.panel.next_frame()
        if frame is not None:
            self.fake.set_raw(self.MAIN, frame, ov.W, ov.H)
            self.fulls += 1
            self.layers.prepare(self.panel.buttons)
        self.layers.sync(self.panel, self.clock())

    def run_for(self, seconds, dt=0.016):
        end = self.clock() + seconds
        while self.clock() < end:
            self.step(dt)

    def center(self, ident):
        b = {b.id: b for b in self.panel.buttons}[ident]
        return b.x + b.w / 2, b.y + b.h / 2

    def small_images(self):
        return [r for r in self.fake.raw if r[0] != self.MAIN]

    def test_probe_leaves_nothing_visible(self):
        self.assertEqual(self.fake.visible, set())
        self.assertEqual(len(self.fake.destroyed), 1)

    def test_first_frame_prepares_highlights_hidden(self):
        self.assertEqual(self.fulls, 1)
        self.assertGreaterEqual(len(self.small_images()), 2)   # 指す色・押す色
        self.assertEqual(self.fake.visible, set())
        self.assertNotIn(self.MAIN, [h for h, _, _ in self.small_images()])

    def test_hover_crossing_borders_uploads_nothing(self):
        qx, qy = self.center("quit")
        rx, ry = self.center("restart")
        raw_before = len(self.fake.raw)
        for i in range(500):
            x, y = [(qx, qy), (rx, ry), (-5, -5), (qx + 300, qy), (rx - 300, ry)][i % 5]
            self.panel.on_mouse("move", x, y)
            self.step()
        self.assertEqual(self.fulls, 1)
        self.assertEqual(len(self.fake.raw) - raw_before, 0)   # 全体の画像も、重ね画像の中身も 0 枚
        self.assertGreater(self.fake.calls["show"], 100)          # 出し入れと位置の変更だけ

    def test_hover_shows_one_highlight_at_the_button(self):
        qx, qy = self.center("quit")
        b = {b.id: b for b in self.panel.buttons}["quit"]
        self.panel.on_mouse("move", qx, qy)
        self.step()
        self.assertEqual(len(self.fake.visible), 1)
        h = next(iter(self.fake.visible))
        self.assertEqual(self.fake.pos[h], (b.x, b.y))
        raw = {r[0]: r for r in self.fake.raw}[h]
        self.assertEqual(raw[1:], (b.w, b.h))
        rx, ry = self.center("restart")
        self.panel.on_mouse("move", rx, ry)
        self.step()
        self.assertEqual(len(self.fake.visible), 1)   # 前のは隠れて、新しいほうだけ
        self.assertEqual(self.fake.pos[next(iter(self.fake.visible))][0], {b.id: b for b in self.panel.buttons}["restart"].x)
        self.panel.on_mouse("move", -5, -5)
        self.step()
        self.assertEqual(self.fake.visible, set())

    def test_press_shows_press_color_and_release_hides_it(self):
        qx, qy = self.center("quit")
        self.panel.on_mouse("move", qx, qy)
        self.step()
        self.panel.on_mouse("down", qx, qy)
        self.step()
        self.assertEqual(len(self.fake.visible), 2)   # 指す色 + 押す色
        self.panel.on_mouse("up", qx, qy)
        self.run_for(0.6)
        self.assertEqual(self.fulls, 2)   # 全体の画像は、待機の見た目に変わった 1 回だけ

    def test_disabled_button_gets_no_highlight(self):
        self.ctrl.status = None
        self.ctrl.view = "search"
        self.ctrl._changed()
        self.run_for(1)
        raw = len(self.fake.raw)
        self.panel.on_mouse("move", 5, 5)
        self.step()
        qx, qy = self.center("retry")
        self.panel.on_mouse("move", qx, qy)
        self.step()
        self.assertEqual(len(self.fake.visible), 1)
        self.assertEqual(len(self.fake.raw), raw)

    def test_armed_label_countdown_swaps_buffers_without_a_full_upload(self):
        qx, qy = self.center("quit")
        self.panel.on_mouse("move", qx, qy)
        self.panel.on_mouse("down", qx, qy)
        self.assertEqual(self.panel.on_mouse("up", qx, qy), arm.ARMED)
        self.run_for(0.6)
        self.assertEqual(self.fulls, 2)
        self.assertIn("(5)", self.layers.visible_label("quit"))
        seen = [self.layers.visible_label("quit")]
        t_end = self.clock() + 4.3
        while self.clock() < t_end:
            self.step()
            lab = self.layers.labels["quit"]
            # 文字は途切れない: 出ているのが少なくとも 1 枚ある
            shown = [h for h in lab["bufs"] if h in self.fake.visible]
            self.assertGreaterEqual(len(shown), 1, self.clock())
            if self.layers.visible_label("quit") != seen[-1]:
                seen.append(self.layers.visible_label("quit"))
        self.assertEqual([t[-3:] for t in seen], ["(5)", "(4)", "(3)", "(2)", "(1)"])
        self.assertEqual(self.fulls, 2)   # 数字が進んでも全体の画像は送らない
        # 待機が外れたら、文字は消えて、全体の画像が 1 回だけ元に戻る
        self.run_for(2)
        self.assertEqual(self.fulls, 3)
        self.assertIsNone(self.layers.visible_label("quit"))
        self.assertTrue(all(h not in self.fake.visible for h in self.layers.labels["quit"]["bufs"] if h))

    def test_label_is_only_the_two_buffers_per_button(self):
        qx, qy = self.center("quit")
        self.panel.on_mouse("down", qx, qy)
        self.panel.on_mouse("up", qx, qy)
        self.run_for(6)
        self.assertEqual(len([h for h in self.layers.labels["quit"]["bufs"] if h]), 2)

    def test_show_hide_falls_back_to_alpha_when_unsupported(self):
        fake = FakeOverlay(show_ok=False)
        layers = ov.Layers(fake, self.MAIN, clock=self.clock)
        layers.prepare(self.panel.buttons)
        qx, qy = self.center("quit")
        self.panel.on_mouse("move", qx, qy)
        layers.sync(self.panel, self.clock())
        self.assertTrue(layers.by_alpha)
        self.assertEqual(len(fake.visible), 1)
        self.panel.on_mouse("move", -5, -5)
        layers.sync(self.panel, self.clock())
        self.assertEqual(fake.visible, set())

    def test_probe_reports_why_subviews_are_unusable(self):
        old = ov.Layers(FakeOverlay(subview=False), self.MAIN)
        self.assertIn("サブビュー", old.probe())
        bad = ov.Layers(FakeOverlay(), self.MAIN)
        bad.ov.create_subview = lambda *a: (_ for _ in ()).throw(vr.OpenVRError("CreateSubviewOverlay: 親が違う"))
        self.assertIn("CreateSubviewOverlay", bad.probe())

    def test_legacy_mode_draws_pressed_in_the_base_but_never_hover(self):
        panel = ov.Panel(self.ctrl, clock=self.clock, layered=False)
        self.clock.advance(1)
        self.assertIsNotNone(panel.next_frame())
        b = {b.id: b for b in panel.buttons}["quit"]
        panel.on_mouse("move", b.x + 5, b.y + 5)
        self.clock.advance(1)
        self.assertIsNone(panel.next_frame())      # 指すだけでは送らない
        panel.on_mouse("down", b.x + 5, b.y + 5)
        self.clock.advance(1)
        self.assertIsNotNone(panel.next_frame())   # 押している間だけは全体の画像に描く
        self.assertEqual(panel.highlights(), {})
        self.assertEqual(panel.label_specs(), {})

    def test_highlight_image_is_a_straight_alpha_single_colour(self):
        key = (640, 280, 44, "#ffffff", 0.18)
        raw = ov.highlight_rgba(key)
        self.assertEqual(len(raw), 640 * 280 * 4)
        mid = (140 * 640 + 320) * 4
        self.assertEqual(tuple(raw[mid:mid + 3]), (255, 255, 255))
        self.assertAlmostEqual(raw[mid + 3], 0.18 * 255, delta=2)
        self.assertEqual(raw[3], 0)   # 角は透明
        dark = ov.highlight_rgba((640, 280, 44, "#000000", 0.22))
        self.assertEqual(tuple(dark[mid:mid + 3]), (0, 0, 0))

    def test_label_image_has_text_pixels_only_in_the_fg_colour(self):
        spec = {"x": 0, "y": 0, "w": 640, "h": 100, "fg": "#2b1600", "size": 50, "baseline": 68}
        raw = ov.label_rgba(spec, "もう一度押すと終了(5)")
        self.assertEqual(len(raw), 640 * 100 * 4)
        self.assertEqual(raw[3], 0)
        self.assertTrue(any(raw[i + 3] > 200 for i in range(0, len(raw), 4)))
        self.assertTrue(all(tuple(raw[i:i + 3]) == (0x2b, 0x16, 0x00) for i in range(0, len(raw), 4)))


class InitErrorTest(unittest.TestCase):
    """0.2.1 までは、SteamVR の起動直後にパネルが SEGV で何度も落ちた(VR_GetVRInitErrorAsSymbol が悪いポインタを返す)。"""

    def test_init_error_names_never_touch_the_runtime(self):
        class Boom:
            def __getattr__(self, name):
                raise AssertionError("ランタイムの %s は呼ばない" % name)

        o = vr.Overlay.__new__(vr.Overlay)
        o.lib = Boom()
        self.assertIn("IPC_ConnectFailed", o._sym(301))
        self.assertEqual(o._sym(99999), "VRInitError 99999")

    def test_source_does_not_call_the_crashing_function(self):
        import inspect
        src = inspect.getsource(vr)
        for line in src.splitlines():
            if "VR_GetVRInitErrorAsSymbol" in line:
                self.assertTrue(line.strip().startswith(("#", "libopenvr_api の")), line)


if __name__ == "__main__":
    unittest.main()
