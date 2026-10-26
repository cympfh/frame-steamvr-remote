"""画面の状態と操作の流れ(OpenVR のパネル用)。描画にも OpenVR にも依存しないので、テストできる。

PC を探す・状態を取る・ペアリングする・終了/再起動する、を裏のスレッドで行い、
状態が変わるたびに rev を増やす(描画する側は rev が変わったときだけ描き直す)。
終了と再起動は fsr_arm.ArmState で 2 回押さないと実行しない。
"""
import threading
import time

import fsr_arm
import fsr_core as core

OP_LABEL = {"quit": "終了", "restart": "再起動"}
BTN_LABEL = {"quit": "SteamVR終了", "restart": "再起動"}
STATE_TEXT = {"running": "起動中", "stopped": "停止中"}


class Controller:
    def __init__(self, core_mod=core, clock=time.monotonic, run_async=True, arm=None):
        self.core = core_mod
        self.run_async = run_async
        self.arm = arm or fsr_arm.ArmState(clock=clock)
        self.lock = threading.RLock()
        self.rev = 0
        self.view = "search"      # search / main / pair
        self.pc = None            # 見つかった PC(ペアリング前など)
        self.status = None        # 直近の status の返事
        self.msg = ""
        self.msg_is_arm = False   # いまの msg が「もう一度押して」の案内か
        self.code = ""
        self.busy = False
        self.pairing = False
        self.repair = False       # 「ペアリングし直す」で開いたペアリング画面(状態の更新で勝手に戻さない)
        self.offline_text = ""
        self.offline_last = ""
        self._wake = threading.Event()
        self._stop = False
        self._thread = None
        self._last_arm_cd = 0

    # ------------------------------------------------------------ 内部
    def _changed(self):
        self.rev += 1

    def set_msg(self, text, is_arm=False):
        with self.lock:
            if self.msg != text or self.msg_is_arm != is_arm:
                self.msg, self.msg_is_arm = text, is_arm
                self._changed()

    def tick(self):
        """時間の経過を反映する(待機の期限切れ・カウントダウンの表示更新)。描画側が定期的に呼ぶ。"""
        with self.lock:
            cd = self.arm.countdown()
            if cd != self._last_arm_cd:
                self._last_arm_cd = cd
                self._changed()
                if cd == 0 and self.msg_is_arm:
                    self.msg, self.msg_is_arm = "", False

    def button_label(self, cmd):
        """ボタンに出す文字と、待機中かどうか。"""
        with self.lock:
            cd = self.arm.countdown(cmd)
            if cd:
                return "もう一度押すと%s(%d)" % (OP_LABEL[cmd], cd), True
            return BTN_LABEL[cmd], False

    def buttons_enabled(self):
        with self.lock:
            return bool(self.status) and not self.busy and self.view == "main"

    # ------------------------------------------------------------ 終了・再起動(2 回押し)
    def press_op(self, cmd):
        """終了/再起動ボタンが押された。戻り値は fsr_arm の ARMED / FIRE / IGNORED、押せない状態なら None。"""
        if cmd not in OP_LABEL:
            return None
        with self.lock:
            if not self.buttons_enabled():
                return None
            r = self.arm.press(cmd)
            self._changed()
            if r == fsr_arm.ARMED:
                note = ""
                if (self.status or {}).get("vrchat"):
                    note = "VRChat が起動中です。SteamVR を止めると VRChat も閉じることがあります。"
                self.set_msg("%s「%s」をもう一度押すと実行します(%d 秒以内)" % (
                    note, BTN_LABEL[cmd], int(self.arm.timeout)), is_arm=True)
                self._last_arm_cd = self.arm.countdown()
                return r
            if r == fsr_arm.IGNORED:
                return r
            self.busy = True
            self.msg_is_arm = False
        self.set_msg("SteamVR を%sしています…(最大 1 分ほどかかります)" % OP_LABEL[cmd])
        if self.run_async:
            threading.Thread(target=self._do_op, args=(cmd,), daemon=True).start()
        else:
            self._do_op(cmd)
        return r

    def _do_op(self, cmd):
        label = OP_LABEL[cmd]
        try:
            r, _pc = self.core.request_with_discovery(cmd, timeout=90)
            ok, detail, state = r.get("ok"), r.get("detail", ""), r.get("steamvr")
            err = None
        except self.core.RemoteError as e:
            self.core.log.error("%s に失敗: %s", cmd, e)
            ok, detail, state, err = False, "", None, str(e)
        except Exception as e:  # 想定外でも画面は固まらせない
            self.core.log.exception("操作で想定外のエラー")
            ok, detail, state, err = False, "", None, "想定外のエラー: %s" % e
        with self.lock:
            self.busy = False
            if err:
                self.set_msg(err)
            elif ok:
                self.set_msg("SteamVR を%sしました" % label if cmd == "quit" or state == "running" else
                             "SteamVR の起動を始めました。立ち上がるまで少し待ってください")
            else:
                self.set_msg("SteamVR の%sに失敗しました(%s)" % (label, detail))
            if state and self.status is not None:
                self.status["steamvr"] = state
            self._changed()
        self.kick()

    # ------------------------------------------------------------ ペアリング
    def show_pair(self):
        with self.lock:
            self.code = ""
            self.view = "pair"
            self.repair = True
            self.arm.disarm()
            self.set_msg("")
            self._changed()

    def back(self):
        """ペアリング画面から戻る(ペアリング済みのときだけ)。"""
        with self.lock:
            if self.core.is_paired():
                self.repair = False
                self.view = "search"
                self.code = ""
                self.set_msg("")
                self._changed()
        self.kick()

    def key(self, k):
        with self.lock:
            if self.pairing or self.view != "pair":
                return
            if k == "消す":
                self.code = self.code[:-1]
            elif k == "全消し":
                self.code = ""
            elif k.isdigit() and len(k) == 1 and len(self.code) < 6:
                self.code += k
            self._changed()
            if len(self.code) != 6:
                return
            code, pc = self.code, self.pc
            if not pc:
                self.code = ""
                self.set_msg("PC が見つかりません。PC のアプリが動いているか確認してください")
                return
            self.pairing = True
        self.set_msg("ペアリングしています…")
        if self.run_async:
            threading.Thread(target=self._do_pair, args=(pc, code), daemon=True).start()
        else:
            self._do_pair(pc, code)

    def _do_pair(self, pc, code):
        try:
            name = self.core.pair(pc["ip"], int(pc.get("port") or self.core.CONTROL_PORT_DEFAULT), code, pc.get("name", ""))
            err = None
        except self.core.RemoteError as e:
            self.core.log.error("ペアリング失敗: %s", e)
            name, err = None, str(e)
        except Exception as e:
            self.core.log.exception("ペアリングで想定外のエラー")
            name, err = None, "想定外のエラー: %s" % e
        with self.lock:
            self.pairing = False
            self.code = ""
            if not err:
                self.repair = False
            if err:
                self.set_msg(err)
            else:
                self.set_msg("ペアリングしました: %s" % name)
                self.view = "search"
            self._changed()
        self.kick()

    # ------------------------------------------------------------ 状態の更新(裏スレッド)
    def kick(self):
        self._wake.set()

    def start(self, interval_fn=lambda: 4.0):
        self._stop = False
        self._interval_fn = interval_fn
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        self._wake.set()

    def _loop(self):
        while not self._stop:
            if not self.busy and not self.pairing:
                try:
                    self.refresh_once()
                except Exception:
                    self.core.log.exception("状態の更新で想定外のエラー")
            self._wake.wait(self._interval_fn())
            self._wake.clear()

    def refresh_once(self):
        cfg = self.core.load_config()
        if not self.core.is_paired(cfg):
            pc, pcs = self.core.find_pc(timeout=1.5)
            if pc is None:
                opening = [p for p in pcs if p.get("pairing")]
                pc = opening[0] if len(opening) == 1 else (pcs[0] if len(pcs) == 1 else None)
            with self.lock:
                before = (self.pc, self.status, self.view, self.arm.armed())
                self.pc, self.status = pc, None
                self.arm.disarm()
                if self.view != "pair":
                    self.view = "pair"
                    self.code = ""
                if before != (self.pc, self.status, self.view, self.arm.armed()):
                    self._changed()  # 変わらなかったときは rev を増やさない(画面を描き直さない)
            return
        try:
            r, pc = self.core.request_with_discovery("status", timeout=6)
        except self.core.RemoteError as e:
            if e.code in ("auth", "unpaired"):
                with self.lock:
                    self.status = None
                    self.arm.disarm()
                    if self.view != "pair":
                        self.view, self.code = "pair", ""
                    self.set_msg(str(e))
                    self._changed()
            else:
                with self.lock:
                    before = (self.status, self.view, self.offline_text, self.offline_last, self.arm.armed())
                    self.status = None
                    self.arm.disarm()
                    if self.view != "pair":
                        self.view = "search"
                    self.offline_text = str(e)
                    self.offline_last = ("前につながった PC: %s (%s)" % (cfg.get("pc_name"), cfg.get("pc_ip", "?"))
                                         if cfg.get("pc_name") else "")
                    if before != (self.status, self.view, self.offline_text, self.offline_last, self.arm.armed()):
                        self._changed()
            return
        with self.lock:
            before = (self.pc, self.status, self.view)
            self.pc, self.status = pc, r
            if self.view != "main" and not (self.view == "pair" and self.repair):
                self.view = "main"
            if before != (self.pc, self.status, self.view):
                self._changed()  # 4 秒ごとの状態確認で、中身が同じなら描き直さない
