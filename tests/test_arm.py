"""終了・再起動の「2 回押し」のテスト。PC には何も送らない(偽の core を使う)。
`python3 -m unittest discover -s tests` で動く(標準ライブラリだけ)。
"""
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="fsr-test-arm-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")
os.environ["XDG_STATE_HOME"] = os.path.join(_TMP, "state")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "frame"))

import fsr_arm as arm  # noqa: E402
import fsr_core  # noqa: E402
import fsr_model as model  # noqa: E402


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class ArmStateTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.a = arm.ArmState(timeout=5.0, min_gap=0.3, clock=self.clock)

    def test_first_press_arms_second_fires(self):
        self.assertEqual(self.a.press("quit"), arm.ARMED)
        self.assertEqual(self.a.armed(), "quit")
        self.clock.advance(1.0)
        self.assertEqual(self.a.press("quit"), arm.FIRE)
        self.assertIsNone(self.a.armed())  # 実行したら待機は外れる

    def test_fire_needs_a_fresh_arm_again(self):
        self.a.press("quit")
        self.clock.advance(1)
        self.a.press("quit")
        self.clock.advance(1)
        self.assertEqual(self.a.press("quit"), arm.ARMED)

    def test_timeout_disarms(self):
        self.a.press("quit")
        self.clock.advance(4.9)
        self.assertEqual(self.a.armed(), "quit")
        self.clock.advance(0.2)
        self.assertIsNone(self.a.armed())
        self.assertEqual(self.a.press("quit"), arm.ARMED)  # 期限切れのあとの 1 回目は、また待機にするだけ

    def test_press_exactly_at_timeout_does_not_fire(self):
        self.a.press("restart")
        self.clock.advance(5.0)
        self.assertEqual(self.a.press("restart"), arm.ARMED)

    def test_arming_other_disarms_first(self):
        self.a.press("quit")
        self.clock.advance(1)
        self.assertEqual(self.a.press("restart"), arm.ARMED)
        self.assertEqual(self.a.armed(), "restart")
        self.assertEqual(self.a.remaining("quit"), 0)
        self.clock.advance(1)
        # 最初のボタンはもう待機ではないので、押しても実行されない
        self.assertEqual(self.a.press("quit"), arm.ARMED)
        self.assertEqual(self.a.armed(), "quit")

    def test_double_click_bounce_is_ignored(self):
        self.a.press("quit")
        self.clock.advance(0.1)
        self.assertEqual(self.a.press("quit"), arm.IGNORED)
        self.assertEqual(self.a.armed(), "quit")  # 無視しても待機はそのまま
        self.clock.advance(0.3)
        self.assertEqual(self.a.press("quit"), arm.FIRE)

    def test_countdown_and_remaining(self):
        self.assertEqual(self.a.countdown("quit"), 0)
        self.a.press("quit")
        self.assertEqual(self.a.countdown("quit"), 5)
        self.assertEqual(self.a.countdown("restart"), 0)
        self.clock.advance(1.2)
        self.assertEqual(self.a.countdown(), 4)
        self.clock.advance(3.7)
        self.assertEqual(self.a.countdown(), 1)
        self.clock.advance(0.2)
        self.assertEqual(self.a.countdown(), 0)

    def test_disarm(self):
        self.a.press("quit")
        self.a.disarm()
        self.assertIsNone(self.a.armed())
        self.assertEqual(self.a.press("quit"), arm.ARMED)


class FakeCore:
    """fsr_core の代わり。ネットワークには出ない。quit / restart が何回呼ばれたかを数える。"""
    CONTROL_PORT_DEFAULT = 47651
    RemoteError = fsr_core.RemoteError

    class _Log:
        def __getattr__(self, _n):
            return lambda *a, **k: None

    log = _Log()

    def __init__(self):
        self.ops = []
        self.fail = None
        self.vrchat = False

    def load_config(self):
        return {"secret": "x", "client_id": "c", "pc_name": "TESTPC", "pc_ip": "192.0.2.1"}

    def is_paired(self, cfg=None):
        return True

    def find_pc(self, timeout=1.5):
        return None, []

    def pair(self, *a, **k):
        raise AssertionError("ペアリングは呼ばれない")

    def request_with_discovery(self, cmd, timeout=10):
        if cmd == "status":
            return {"name": "TESTPC", "steamvr": "running", "vrchat": self.vrchat}, {"ip": "192.0.2.1"}
        self.ops.append(cmd)
        if self.fail:
            raise self.fail
        return {"ok": True, "detail": "", "steamvr": "stopped" if cmd == "quit" else "running"}, {"ip": "192.0.2.1"}


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.core = FakeCore()
        self.c = model.Controller(core_mod=self.core, clock=self.clock, run_async=False)
        self.c.refresh_once()  # status を取る(quit/restart ではない)

    def test_status_makes_buttons_available(self):
        self.assertEqual(self.c.view, "main")
        self.assertTrue(self.c.buttons_enabled())
        self.assertEqual(self.core.ops, [])

    def test_one_press_never_sends(self):
        self.assertEqual(self.c.press_op("quit"), arm.ARMED)
        self.assertEqual(self.core.ops, [])
        label, armed = self.c.button_label("quit")
        self.assertTrue(armed)
        self.assertIn("もう一度押す", label)
        self.assertIn("(5)", label)
        self.assertEqual(self.c.button_label("restart"), ("再起動", False))

    def test_two_presses_send_once(self):
        self.c.press_op("quit")
        self.clock.advance(1.0)
        self.assertEqual(self.c.press_op("quit"), arm.FIRE)
        self.assertEqual(self.core.ops, ["quit"])
        self.assertIn("終了しました", self.c.msg)
        # 3 回目は、また 1 回目の扱い
        self.clock.advance(1.0)
        self.assertEqual(self.c.press_op("quit"), arm.ARMED)
        self.assertEqual(self.core.ops, ["quit"])

    def test_timeout_then_press_does_not_send(self):
        self.c.press_op("restart")
        self.clock.advance(6.0)
        self.c.tick()
        self.assertEqual(self.c.msg, "")  # 案内も消える
        self.assertEqual(self.c.button_label("restart"), ("再起動", False))
        self.assertEqual(self.c.press_op("restart"), arm.ARMED)
        self.assertEqual(self.core.ops, [])

    def test_other_button_disarms_first(self):
        self.c.press_op("quit")
        self.clock.advance(1.0)
        self.c.press_op("restart")
        self.assertEqual(self.c.button_label("quit"), ("SteamVR終了", False))
        self.assertTrue(self.c.button_label("restart")[1])
        self.clock.advance(1.0)
        self.assertEqual(self.c.press_op("quit"), arm.ARMED)  # 終了は待機が外れていたので、実行されない
        self.assertEqual(self.core.ops, [])

    def test_bounce_does_not_fire(self):
        self.c.press_op("quit")
        self.clock.advance(0.1)
        self.assertEqual(self.c.press_op("quit"), arm.IGNORED)
        self.assertEqual(self.core.ops, [])

    def test_vrchat_note_in_message(self):
        self.core.vrchat = True
        self.c.refresh_once()
        self.c.press_op("quit")
        self.assertIn("VRChat", self.c.msg)
        self.assertIn("もう一度押すと実行します", self.c.msg)

    def test_buttons_do_nothing_without_status_or_while_busy(self):
        self.c.status = None
        self.assertIsNone(self.c.press_op("quit"))
        self.c.status = {"name": "x", "steamvr": "running"}
        self.c.busy = True
        self.assertIsNone(self.c.press_op("quit"))
        self.assertEqual(self.core.ops, [])

    def test_failure_is_shown_and_nothing_stays_armed(self):
        self.core.fail = fsr_core.RemoteError("PC につながりません")
        self.c.press_op("restart")
        self.clock.advance(1)
        self.c.press_op("restart")
        self.assertEqual(self.c.msg, "PC につながりません")
        self.assertFalse(self.c.busy)
        self.assertIsNone(self.c.arm.armed())

    def test_unknown_command_is_rejected(self):
        self.assertIsNone(self.c.press_op("format"))
        self.assertEqual(self.core.ops, [])

    def test_pair_screen_disarms(self):
        self.c.press_op("quit")
        self.c.show_pair()
        self.assertIsNone(self.c.arm.armed())
        # 「ペアリングし直す」で開いたペアリング画面は、状態の更新で勝手に戻らない
        self.c.refresh_once()
        self.assertEqual(self.c.view, "pair")


class DryRunTest(unittest.TestCase):
    def test_dry_run_never_touches_the_network(self):
        def boom(*a, **k):
            raise AssertionError("ネットワークに出てはいけない")
        with mock.patch.dict(os.environ, {"FSR_DRY_RUN": "1"}), \
                mock.patch.object(socket, "create_connection", boom), \
                mock.patch.object(socket, "socket", boom):
            for cmd in ("quit", "restart"):
                r, _pc = fsr_core.request_with_discovery(cmd)
                self.assertTrue(r["ok"])
                self.assertEqual(r["detail"], "dry-run")


if __name__ == "__main__":
    unittest.main()
