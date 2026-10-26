"""Frame 側(Python)と PC 側(Rust)を実際につないで確かめるテスト。
PC 側は偽の PC(--dev-server)なので、本物の SteamVR には触れない。
先に `cargo build` しておくこと。環境変数 FSR_DEV_SERVER でバイナリを指定できる。
"""
import os
import subprocess
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="fsr-interop-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")
os.environ["XDG_STATE_HOME"] = os.path.join(_TMP, "state")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "frame"))

import fsr_core as core  # noqa: E402

BIN = os.environ.get("FSR_DEV_SERVER") or os.path.join(ROOT, "target", "debug", "frame-steamvr-remote")
DISC, CTRL = 47750, 47751


@unittest.skipUnless(os.path.exists(BIN), "cargo build が先に必要")
class InteropTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ, FSR_DATA_DIR=os.path.join(_TMP, "pc"))
        cls.proc = subprocess.Popen([BIN, "--dev-server", str(DISC), str(CTRL)], stdout=subprocess.PIPE, text=True, env=env)
        line = cls.proc.stdout.readline().split()
        assert line[0] == "READY", line
        cls.code = line[1]

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()

    def test_flow(self):
        pcs = core.discover(timeout=1.0, broadcast=["127.0.0.1"], port=DISC)
        self.assertEqual(len(pcs), 1)
        self.assertEqual(pcs[0]["name"], "DEV-PC")
        self.assertEqual(pcs[0]["port"], CTRL)
        self.assertTrue(pcs[0]["pairing"])
        # ペアリング前は操作できない
        with self.assertRaises(core.RemoteError):
            core.request("127.0.0.1", CTRL, "status")
        # 間違ったコードは弾かれる
        wrong = "000000" if self.code != "000000" else "111111"
        with self.assertRaises(core.RemoteError) as cm:
            core.pair("127.0.0.1", CTRL, wrong)
        self.assertEqual(cm.exception.code, "bad_code")
        self.assertFalse(core.is_paired())
        # 正しいコードで成功し、操作できる
        self.assertEqual(core.pair("127.0.0.1", CTRL, self.code), "DEV-PC")
        self.assertTrue(core.is_paired())
        self.assertEqual(core.request("127.0.0.1", CTRL, "status")["steamvr"], "running")
        self.assertEqual(core.request("127.0.0.1", CTRL, "quit")["steamvr"], "stopped")
        self.assertEqual(core.request("127.0.0.1", CTRL, "status")["steamvr"], "stopped")
        self.assertEqual(core.request("127.0.0.1", CTRL, "restart")["steamvr"], "running")
        # 合言葉が違うと拒否される
        cfg = core.load_config()
        cfg["secret"] = "00" * 32
        core.save_config(cfg)
        with self.assertRaises(core.RemoteError) as cm:
            core.request("127.0.0.1", CTRL, "status")
        self.assertEqual(cm.exception.code, "auth")


if __name__ == "__main__":
    unittest.main()
