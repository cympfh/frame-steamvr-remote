#!/usr/bin/env python3
"""frame-steamvr-remote (Frame 側) の中身: 通信・発見・設定・ログ。標準ライブラリだけで動く。

PC 側(Windows のトレイアプリ)との取り決めは docs/PROTOCOL.md を参照。
"""
import hmac
import hashlib
import json
import logging
import logging.handlers
import os
import re
import socket
import subprocess
import time

APP_ID = "fsr"
DISCOVERY_PORT = int(os.environ.get("FSR_DISCOVERY_PORT", "47650"))
CONTROL_PORT_DEFAULT = int(os.environ.get("FSR_CONTROL_PORT", "47651"))
NAME = "frame-steamvr-remote"

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), NAME)
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(STATE_DIR, "app.log")

log = logging.getLogger(NAME)


class RemoteError(Exception):
    """ユーザーに見せてよい日本語のメッセージを持つエラー。"""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def setup_logging(also_stderr=False):
    os.makedirs(STATE_DIR, exist_ok=True)
    if log.handlers:
        return
    log.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if also_stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)
        log.addHandler(sh)


# ---------------------------------------------------------------- X25519 (RFC 7748)
_P = 2 ** 255 - 19


def x25519(k: bytes, u: bytes) -> bytes:
    b = bytearray(k)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    kk = int.from_bytes(b, "little")
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        kt = (kk >> t) & 1
        swap ^= kt
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = kt
        a = (x2 + z2) % _P
        aa = a * a % _P
        bb_ = (x2 - z2) % _P
        bb = bb_ * bb_ % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * bb_ % _P
        x3 = (da + cb) ** 2 % _P
        z3 = x1 * (da - cb) ** 2 % _P
        x2 = aa * bb % _P
        z2 = e * (aa + 121665 * e) % _P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


_BASE = (9).to_bytes(32, "little")


def hmac_hex(key: bytes, msg: str) -> str:
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()


def hmac_bytes(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


# ---------------------------------------------------------------- 設定(合言葉は 0600)
def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


def client_id(cfg=None):
    cfg = load_config() if cfg is None else cfg
    cid = cfg.get("client_id")
    if not cid:
        cid = os.urandom(8).hex()
        cfg["client_id"] = cid
        save_config(cfg)
    return cid


def is_paired(cfg=None):
    cfg = load_config() if cfg is None else cfg
    return bool(cfg.get("secret") and cfg.get("client_id"))


def forget():
    """ペアリング情報を消す(合言葉だけ。ファイル自体は残す)。"""
    cfg = load_config()
    for k in ("secret", "pc_id", "pc_name", "pc_ip"):
        cfg.pop(k, None)
    save_config(cfg)
    log.info("ペアリング情報を消した(Frame 側)")


# ---------------------------------------------------------------- 発見
def broadcast_addresses():
    addrs = ["255.255.255.255"]
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
        for m in re.finditer(r"inet \S+ brd (\d+\.\d+\.\d+\.\d+)", out):
            if m.group(1) not in addrs:
                addrs.append(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return addrs


def discover(timeout=1.5, extra_hosts=(), broadcast=None, port=None):
    """同じ LAN の PC を探す。[{ip,name,id,port,steamvr,pairing,paired}, ...]"""
    port = port or DISCOVERY_PORT
    targets = list(broadcast_addresses() if broadcast is None else broadcast) + list(extra_hosts)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.25)
    found = {}
    msg = json.dumps({"app": APP_ID, "v": 1, "t": "discover"}).encode()
    deadline = time.time() + timeout
    last_send = 0
    try:
        while time.time() < deadline:
            if time.time() - last_send > 0.5:
                for t in targets:
                    try:
                        s.sendto(msg, (t, port))
                    except OSError as e:
                        log.debug("発見の送信に失敗 %s: %s", t, e)
                last_send = time.time()
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                v = json.loads(data.decode("utf-8", "replace"))
            except ValueError:
                continue
            if v.get("app") == APP_ID and v.get("t") == "here":
                v["ip"] = addr[0]
                if addr[0] not in found:
                    log.info("PC を発見: %s", json.dumps(v, ensure_ascii=False))
                found[addr[0]] = v
    finally:
        s.close()
    return list(found.values())


def pick_pc(pcs, cfg=None):
    """設定に覚えている PC を優先して選ぶ。"""
    cfg = load_config() if cfg is None else cfg
    if not pcs:
        return None
    pid = cfg.get("pc_id")
    if pid:
        for p in pcs:
            if p.get("id") == pid:
                return p
        return None
    return pcs[0] if len(pcs) == 1 else None


def find_pc(timeout=1.5):
    """覚えている PC があればその PC、なければ見つかった PC を返す。見つからなければ None。"""
    cfg = load_config()
    extra = [cfg["pc_ip"]] if cfg.get("pc_ip") else []
    pcs = discover(timeout=timeout, extra_hosts=extra)
    return pick_pc(pcs, cfg), pcs


# ---------------------------------------------------------------- TCP
def _read_json(f):
    line = f.readline(4096)
    if not line:
        raise RemoteError("PC との接続が切れました")
    try:
        return json.loads(line.decode("utf-8"))
    except ValueError:
        raise RemoteError("PC からの返事が読めません")


def _send(sock, obj):
    sock.sendall((json.dumps(obj) + "\n").encode())


def _connect(ip, port, timeout):
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
    except OSError as e:
        raise RemoteError("PC につながりません(PC のアプリが動いているか、Windows のファイアウォールを確認してください)") from e
    s.settimeout(timeout)
    f = s.makefile("rb")
    ch = _read_json(f)
    if ch.get("t") != "challenge":
        s.close()
        raise RemoteError("PC の応答が想定と違います")
    return s, f, ch["nonce"]


ERR_TEXT = {
    "auth": "この Frame は PC とペアリングされていません。ペアリングし直してください",
    "not_pairing": "PC がペアリングを受け付けていません。PC のトレイのメニューから「Frame とペアリングする」を選んでください",
    "bad_code": "コードが違います",
    "unknown_cmd": "PC が知らない操作です",
    "bad_request": "PC が要求を受け付けませんでした",
    "save_failed": "PC が合言葉を保存できませんでした",
}


def _raise_if_error(resp):
    if resp.get("t") == "error":
        code = resp.get("error", "")
        msg = ERR_TEXT.get(code, "PC がエラーを返しました: " + code)
        if code == "bad_code" and "left" in resp:
            msg += "(あと %s 回)" % resp["left"]
        raise RemoteError(msg, code)


def pair(ip, port, code, pc_name_hint=""):
    """PC に表示された数字コードでペアリングする。成功したら設定に保存する。"""
    cfg = load_config()
    cid = client_id(cfg)
    log.info("ペアリングを開始: %s:%s", ip, port)
    s, f, _ = _connect(ip, port, 10)
    try:
        priv = os.urandom(32)
        a = x25519(priv, _BASE).hex()
        _send(s, {"t": "pair", "client": cid, "name": "Steam Frame", "pub": a})
        m1 = _read_json(f)
        _raise_if_error(m1)
        b = m1.get("pub", "")
        try:
            peer = bytes.fromhex(b)
            k = x25519(priv, peer)
        except ValueError:
            raise RemoteError("PC の鍵が読めません")
        if len(peer) != 32 or k == bytes(32):
            raise RemoteError("PC の鍵が正しくありません")
        _send(s, {"t": "pair2", "proof": hmac_hex(k, "pair-frame|%s|%s|%s" % (code, a, b))})
        m2 = _read_json(f)
        _raise_if_error(m2)
        expected = hmac_hex(k, "pair-pc|%s|%s|%s" % (code, a, b))
        if m2.get("t") != "paired" or not hmac.compare_digest(expected, str(m2.get("proof", ""))):
            raise RemoteError("PC の確認が合いません。ペアリングを中止しました")
        secret = hmac_bytes(k, "secret|%s|%s|%s" % (code, a, b))
    finally:
        s.close()
    cfg = load_config()
    cfg.update({"client_id": cid, "secret": secret.hex(), "pc_id": m2.get("pc_id", ""),
                "pc_name": m2.get("pc_name", pc_name_hint), "pc_ip": ip})
    save_config(cfg)
    log.info("ペアリング成功: PC=%s id=%s", cfg["pc_name"], cfg["pc_id"])
    return cfg["pc_name"]


def request(ip, port, cmd, timeout=10):
    """ペアリング済みの Frame として操作を送る(status / quit / restart)。結果の dict を返す。"""
    cfg = load_config()
    if not is_paired(cfg):
        raise RemoteError("まだペアリングしていません", "unpaired")
    cid = cfg["client_id"]
    secret = bytes.fromhex(cfg["secret"])
    s, f, nonce = _connect(ip, port, timeout)
    try:
        mac = hmac_hex(secret, "req|%s|%s|%s" % (nonce, cid, cmd))
        _send(s, {"t": "req", "client": cid, "cmd": cmd, "mac": mac})
        resp = _read_json(f)
    finally:
        s.close()
    log.info("%s(%s) の結果: %s", cmd, ip, json.dumps(resp, ensure_ascii=False))
    _raise_if_error(resp)
    return resp


def request_with_discovery(cmd, timeout=10):
    """PC を(必要なら探して)操作を送る。(結果, PC情報) を返す。

    環境変数 FSR_DRY_RUN=1 のときは、quit / restart を PC に送らず、成功したことにする(テストと動作確認用)。
    """
    if os.environ.get("FSR_DRY_RUN") == "1" and cmd in ("quit", "restart"):
        log.warning("FSR_DRY_RUN=1 なので %s は PC に送らない", cmd)
        return {"t": "result", "ok": True, "detail": "dry-run", "steamvr": "running"}, {"ip": "dry-run"}
    cfg = load_config()
    pc = None
    if cfg.get("pc_ip"):
        try:
            return request(cfg["pc_ip"], CONTROL_PORT_DEFAULT, cmd, timeout), {"ip": cfg["pc_ip"]}
        except RemoteError as e:
            if e.code in ("auth", "unpaired"):
                raise
            log.info("前回の IP(%s) で失敗したので探し直す: %s", cfg["pc_ip"], e)
    pc, pcs = find_pc()
    if pc is None:
        if pcs:
            raise RemoteError("PC が複数見つかりました。ペアリングした PC が見つかりません")
        raise RemoteError("PC が見つかりません。PC が起動していて、トレイのアプリが動いているか確認してください")
    port = int(pc.get("port") or CONTROL_PORT_DEFAULT)
    resp = request(pc["ip"], port, cmd, timeout)
    cfg = load_config()
    if cfg.get("pc_ip") != pc["ip"]:
        cfg["pc_ip"] = pc["ip"]
        save_config(cfg)
    return resp, pc
