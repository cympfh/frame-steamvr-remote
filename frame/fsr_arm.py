"""「2 回押さないと実行しない」ボタンの仕組み(画面にも OpenVR のパネルにも依存しない)。

1 回目の押下でそのボタンを「待機」にして、ほかのボタンは待機から外す。
待機中(既定 5 秒以内)に同じボタンをもう一度押すと実行。時間が過ぎたら待機は自然に外れる。
押した直後の二重反応(コントローラーの手ぶれ・チャタリング)で、うっかり実行しないよう、
待機にしてから MIN_GAP 秒たつまでの 2 回目は無視する。
"""
import math
import time

ARMED = "armed"      # 1 回目: 待機にした(まだ実行しない)
FIRE = "fire"        # 2 回目: 実行してよい
IGNORED = "ignored"  # 待機にした直後の二重反応なので無視した

DEFAULT_TIMEOUT = 5.0
MIN_GAP = 0.3


class ArmState:
    def __init__(self, timeout=DEFAULT_TIMEOUT, min_gap=MIN_GAP, clock=time.monotonic):
        self.timeout = float(timeout)
        self.min_gap = float(min_gap)
        self._clock = clock
        self._cmd = None
        self._armed_at = 0.0

    def _expire(self):
        if self._cmd is not None and self._clock() - self._armed_at >= self.timeout:
            self._cmd = None

    def armed(self):
        """いま待機中の操作名(なければ None)。期限切れならここで外れる。"""
        self._expire()
        return self._cmd

    def remaining(self, cmd=None):
        """待機の残り秒数。待機中でなければ 0。cmd を渡すと、その操作が待機中のときだけ返す。"""
        self._expire()
        if self._cmd is None or (cmd is not None and cmd != self._cmd):
            return 0.0
        return max(0.0, self.timeout - (self._clock() - self._armed_at))

    def countdown(self, cmd=None):
        """画面に出す整数の残り秒(5,4,3,2,1)。待機中でなければ 0。"""
        r = self.remaining(cmd)
        return int(math.ceil(r)) if r > 0 else 0

    def press(self, cmd):
        """ボタンが押された。ARMED / FIRE / IGNORED のどれか。FIRE のときは待機を外す。"""
        self._expire()
        now = self._clock()
        if self._cmd == cmd:
            if now - self._armed_at < self.min_gap:
                return IGNORED
            self._cmd = None
            return FIRE
        self._cmd = cmd  # 別のボタンの待機はここで外れる
        self._armed_at = now
        return ARMED

    def disarm(self):
        self._cmd = None
