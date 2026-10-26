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

    def test_hover_jitter_at_a_button_edge_uploads_nothing(self):
        qx, qy = self.center("quit")
        # 境目を 20 回/秒で行き来する(手ぶれ)。どこにも落ち着かない
        n = 0
        for i in range(60):
            self.clock.advance(0.05)
            self.panel.on_mouse("move", qx, qy) if i % 2 == 0 else self.panel.on_mouse("move", -5, -5)
            if self.pump() is not None:
                n += 1
        self.assertEqual(n, 0)

    def test_hover_that_settles_uploads_once(self):
        qx, qy = self.center("quit")
        self.clock.advance(1)
        self.panel.on_mouse("move", qx, qy)
        self.assertEqual(self.run_for(2), 1)
        self.assertEqual(self.run_for(5), 0)
        # マウスが動き続けても、同じボタンの上なら送らない
        for _ in range(100):
            self.clock.advance(0.016)
            self.panel.on_mouse("move", qx + 1, qy + 1)
            self.assertIsNone(self.pump())

    def test_hover_on_disabled_button_changes_nothing(self):
        self.ctrl.status = None  # PC が見えない(ボタンが押せない)
        self.ctrl.view = "search"
        self.ctrl._changed()
        self.clock.advance(1)
        self.assertEqual(self.run_for(1), 1)
        self.panel.on_mouse("move", 5, 5)
        self.panel.on_mouse("leave", -1, -1)
        self.assertEqual(self.run_for(3), 0)

    def test_uploads_are_at_least_min_interval_apart(self):
        qx, qy = self.center("quit")
        rx, ry = self.center("restart")
        t = 0
        for i in range(200):  # 押す・離すを速く繰り返す
            self.clock.advance(0.02)
            self.panel.on_mouse("move", qx if i % 2 else rx, qy if i % 2 else ry)
            self.panel.on_mouse("down", qx if i % 2 else rx, qy if i % 2 else ry)
            self.pump()
            self.panel.on_mouse("leave", -1, -1)
            self.pump()
            t += 1
        times = [t for t, _ in self.frames]
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(g >= 0.25 - 1e-9 for g in gaps), min(gaps))

    def test_arm_countdown_uploads_once_per_second(self):
        qx, qy = self.center("quit")
        self.clock.advance(1)
        self.panel.on_mouse("move", qx, qy)
        self.run_for(1)  # ホバーの画像
        self.panel.on_mouse("down", qx, qy)
        self.run_for(0.5)  # 押した画像
        self.assertEqual(self.panel.on_mouse("up", qx, qy), arm.ARMED)
        before = len(self.frames)
        self.run_for(0.5)
        self.assertEqual(len(self.frames) - before, 1)  # 「もう一度押すと終了(5)」の画像
        t_arm = self.clock()
        mark = len(self.frames)
        n = self.run_for(6)  # 5 秒たつと待機が外れる
        # 数字が 4,3,2,1 と進み、最後に待機が外れて元の文字に戻る = 5 枚。1 秒に 1 枚
        self.assertEqual(n, 5)
        times = [t for t, _ in self.frames[mark:]]
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(all(0.95 <= g <= 1.1 for g in gaps), gaps)
        self.assertLess(times[0] - t_arm, 1.1)
        self.assertEqual(self.run_for(10), 0)  # そのあとは静か

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


if __name__ == "__main__":
    unittest.main()
