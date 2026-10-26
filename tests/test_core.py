"""Frame 側の部品のテスト。 `python3 -m unittest discover -s tests` で動く(標準ライブラリだけ)。"""
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix="fsr-test-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_TMP, "config")
os.environ["XDG_STATE_HOME"] = os.path.join(_TMP, "state")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "frame"))

import fsr_core as core  # noqa: E402


class X25519Test(unittest.TestCase):
    def test_rfc7748_vector1(self):
        k = bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4")
        u = bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
        self.assertEqual(core.x25519(k, u).hex(), "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552")

    def test_rfc7748_exchange(self):
        a = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
        b = bytes.fromhex("5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")
        pa = core.x25519(a, core._BASE)
        pb = core.x25519(b, core._BASE)
        self.assertEqual(pa.hex(), "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
        self.assertEqual(pb.hex(), "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")
        shared = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
        self.assertEqual(core.x25519(a, pb).hex(), shared)
        self.assertEqual(core.x25519(b, pa).hex(), shared)


class HmacTest(unittest.TestCase):
    def test_rfc4231_case2(self):
        self.assertEqual(
            core.hmac_hex(b"Jefe", "what do ya want for nothing?"),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843",
        )


class ConfigTest(unittest.TestCase):
    def test_config_is_private_and_forget_keeps_client_id(self):
        cfg = core.load_config()
        cfg["secret"] = "ab" * 32
        cfg["pc_id"] = "x"
        core.save_config(cfg)
        mode = os.stat(core.CONFIG_PATH).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        cid = core.client_id()
        core.forget()
        self.assertFalse(core.is_paired())
        self.assertEqual(core.client_id(), cid)


if __name__ == "__main__":
    unittest.main()
