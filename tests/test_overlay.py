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


if __name__ == "__main__":
    unittest.main()
