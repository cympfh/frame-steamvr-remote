//! LAN 向けのサーバー: UDP の発見応答と、TCP の操作受付。
//! 受け付けるのは「ペアリング」「状態」「SteamVR 終了」「SteamVR 再起動」だけ。
use crate::log;
use crate::proto::*;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream, UdpSocket};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub struct OpResult {
    pub ok: bool,
    pub state: &'static str, // "running" | "stopped"
    pub detail: String,
}

pub struct Pairing {
    pub client_id: String,
    pub client_name: String,
    pub secret: Vec<u8>,
}

/// OS ごとに違う部分。Windows では本物、テストでは偽物を入れる。
pub trait Host: Send + Sync {
    fn pc_name(&self) -> String;
    fn pc_id(&self) -> String;
    fn steamvr_running(&self) -> bool;
    fn vrchat_running(&self) -> bool;
    fn quit_steamvr(&self) -> OpResult;
    fn restart_steamvr(&self) -> OpResult;
    fn load_pairing(&self) -> Option<Pairing>;
    fn save_pairing(&self, p: &Pairing) -> Result<(), String>;
    fn clear_pairing(&self);
    fn notify(&self, msg: &str);
    fn close_code_dialog(&self);
}

struct Session {
    code: String,
    expires: Instant,
    attempts: u8,
}

pub struct Server {
    host: Arc<dyn Host>,
    session: Mutex<Option<Session>>,
    op_lock: Mutex<()>,
    active: AtomicUsize,
    control_port: u16,
}

impl Server {
    pub fn new(host: Arc<dyn Host>) -> Arc<Server> {
        Server::with_port(host, CONTROL_PORT)
    }

    /// 発見の返事で知らせる操作用ポートを指定する(テスト用の別ポート向け)。
    pub fn with_port(host: Arc<dyn Host>, control_port: u16) -> Arc<Server> {
        Arc::new(Server {
            host,
            session: Mutex::new(None),
            op_lock: Mutex::new(()),
            active: AtomicUsize::new(0),
            control_port,
        })
    }

    /// ペアリングを受け付け始め、表示するコードを返す。
    pub fn open_pairing(&self) -> String {
        let code = random_code();
        *self.session.lock().unwrap_or_else(|e| e.into_inner()) = Some(Session {
            code: code.clone(),
            expires: Instant::now() + Duration::from_secs(PAIR_VALID_SECS),
            attempts: PAIR_MAX_ATTEMPTS,
        });
        log!("ペアリングの受付を開始 ({} 秒間有効、コードは画面にだけ出す)", PAIR_VALID_SECS);
        code
    }

    pub fn pairing_open(&self) -> bool {
        let mut g = self.session.lock().unwrap_or_else(|e| e.into_inner());
        match g.as_ref() {
            Some(s) if s.expires > Instant::now() => true,
            Some(_) => {
                *g = None;
                false
            }
            None => false,
        }
    }

    fn state_str(&self) -> &'static str {
        if self.host.steamvr_running() { "running" } else { "stopped" }
    }

    pub fn discovery_reply(&self) -> Value {
        json!({
            "app": APP_ID, "v": PROTO_VERSION, "t": "here",
            "id": self.host.pc_id(), "name": self.host.pc_name(), "port": self.control_port,
            "steamvr": self.state_str(),
            "pairing": self.pairing_open(),
            "paired": self.host.load_pairing().is_some(),
        })
    }

    // ---------- UDP ----------
    pub fn run_discovery(self: &Arc<Self>, port: u16) -> std::io::Result<()> {
        let sock = UdpSocket::bind(("0.0.0.0", port))?;
        log!("発見の待ち受けを開始: UDP {}", port);
        let mut buf = [0u8; 512];
        let mut last_logged: Option<(std::net::IpAddr, Instant)> = None;
        loop {
            let (n, from) = match sock.recv_from(&mut buf) {
                Ok(x) => x,
                Err(e) => {
                    log!("UDP 受信エラー: {}", e);
                    std::thread::sleep(Duration::from_millis(500));
                    continue;
                }
            };
            let Ok(v) = serde_json::from_slice::<Value>(&buf[..n]) else { continue };
            if v["app"] == APP_ID && v["t"] == "discover" {
                let reply = self.discovery_reply().to_string();
                let _ = sock.send_to(reply.as_bytes(), from);
                // 探している間は 0.5 秒ごとに来るので、同じ相手からの問い合わせは 30 秒に 1 回だけ記録する
                let fresh = match last_logged {
                    Some((ip, t)) => ip != from.ip() || t.elapsed() > Duration::from_secs(30),
                    None => true,
                };
                if fresh {
                    log!("発見の問い合わせに返信: {}", from);
                    last_logged = Some((from.ip(), Instant::now()));
                }
            }
        }
    }

    // ---------- TCP ----------
    pub fn run_control(self: &Arc<Self>, port: u16) -> std::io::Result<()> {
        let l = TcpListener::bind(("0.0.0.0", port))?;
        log!("操作の待ち受けを開始: TCP {}", port);
        for conn in l.incoming() {
            let Ok(stream) = conn else { continue };
            if self.active.fetch_add(1, Ordering::SeqCst) >= 8 {
                self.active.fetch_sub(1, Ordering::SeqCst);
                log!("同時接続が多すぎるので切断");
                continue;
            }
            let me = Arc::clone(self);
            std::thread::spawn(move || {
                let peer = stream.peer_addr().ok();
                if let Err(e) = me.handle(stream, peer) {
                    log!("接続の処理でエラー ({:?}): {}", peer, e);
                }
                me.active.fetch_sub(1, Ordering::SeqCst);
            });
        }
        Ok(())
    }

    fn handle(&self, stream: TcpStream, peer: Option<SocketAddr>) -> Result<(), String> {
        let e = |x: std::io::Error| x.to_string();
        stream.set_read_timeout(Some(Duration::from_secs(10))).map_err(e)?;
        stream.set_write_timeout(Some(Duration::from_secs(10))).map_err(e)?;
        let mut w = stream.try_clone().map_err(e)?;
        let mut r = BufReader::new(stream);
        let nonce = random_hex(16);
        send(&mut w, &json!({"app": APP_ID, "v": PROTO_VERSION, "t": "challenge", "nonce": nonce}))?;
        let req = read_json(&mut r)?;
        let t = req["t"].as_str().unwrap_or("");
        match t {
            "pair" => self.handle_pair(&mut r, &mut w, &req, peer),
            "req" => self.handle_req(&mut w, &req, &nonce, peer),
            _ => send(&mut w, &json!({"t": "error", "error": "bad_request"})),
        }
    }

    fn handle_req(&self, w: &mut TcpStream, req: &Value, nonce: &str, peer: Option<SocketAddr>) -> Result<(), String> {
        let client = req["client"].as_str().unwrap_or("");
        let cmd = req["cmd"].as_str().unwrap_or("");
        let mac = req["mac"].as_str().unwrap_or("");
        let ok = match self.host.load_pairing() {
            Some(p) => p.client_id == client && verify_request(&p.secret, nonce, client, cmd, mac),
            None => false,
        };
        if !ok {
            log!("認証に失敗 ({:?}): client={} cmd={}", peer, clean_name(client), clean_name(cmd));
            std::thread::sleep(Duration::from_millis(500));
            return send(w, &json!({"t": "error", "error": "auth"}));
        }
        log!("要求を受信 ({:?}): {}", peer, cmd);
        match cmd {
            "status" => send(
                w,
                &json!({"t": "status", "name": self.host.pc_name(), "id": self.host.pc_id(),
                        "steamvr": self.state_str(), "vrchat": self.host.vrchat_running()}),
            ),
            "quit" | "restart" => {
                let _g = self.op_lock.lock().unwrap_or_else(|e| e.into_inner());
                let res = if cmd == "quit" { self.host.quit_steamvr() } else { self.host.restart_steamvr() };
                log!("{} の結果: ok={} state={} detail={}", cmd, res.ok, res.state, res.detail);
                send(w, &json!({"t": "result", "cmd": cmd, "ok": res.ok, "steamvr": res.state, "detail": res.detail}))
            }
            _ => send(w, &json!({"t": "error", "error": "unknown_cmd"})),
        }
    }

    fn handle_pair(
        &self,
        r: &mut BufReader<TcpStream>,
        w: &mut TcpStream,
        req: &Value,
        peer: Option<SocketAddr>,
    ) -> Result<(), String> {
        if !self.pairing_open() {
            log!("ペアリング要求を拒否 ({:?}): 受付中ではない", peer);
            return send(w, &json!({"t": "error", "error": "not_pairing"}));
        }
        let client = req["client"].as_str().unwrap_or("");
        let name = clean_name(req["name"].as_str().unwrap_or("Steam Frame"));
        let a_hex = req["pub"].as_str().unwrap_or("").to_string();
        if !valid_client_id(client) {
            return send(w, &json!({"t": "error", "error": "bad_request"}));
        }
        let eph = Ephemeral::new();
        let Some(k) = eph.agree(&a_hex) else {
            return send(w, &json!({"t": "error", "error": "bad_request"}));
        };
        let b_hex = eph.public_hex.clone();
        send(w, &json!({"t": "pair1", "pub": b_hex}))?;
        let req2 = read_json(r)?;
        if req2["t"] != "pair2" {
            return send(w, &json!({"t": "error", "error": "bad_request"}));
        }
        let proof = req2["proof"].as_str().unwrap_or("");
        // コードの確認は 1 回ずつ直列に行う(試行回数を数えるため)
        let (code, success) = {
            let mut g = self.session.lock().unwrap_or_else(|e| e.into_inner());
            let Some(s) = g.as_mut() else {
                drop(g);
                return send(w, &json!({"t": "error", "error": "not_pairing"}));
            };
            if s.expires <= Instant::now() {
                *g = None;
                drop(g);
                return send(w, &json!({"t": "error", "error": "not_pairing"}));
            }
            let code = s.code.clone();
            if verify_hex(&k, &format!("pair-frame|{}|{}|{}", code, a_hex, b_hex), proof) {
                *g = None;
                (code, true)
            } else {
                s.attempts = s.attempts.saturating_sub(1);
                let left = s.attempts;
                if left == 0 {
                    *g = None;
                }
                drop(g);
                log!("ペアリングのコードが違う ({:?}) 残り {} 回", peer, left);
                std::thread::sleep(Duration::from_millis(500));
                return send(w, &json!({"t": "error", "error": "bad_code", "left": left}));
            }
        };
        debug_assert!(success);
        let secret = derive_secret(&k, &code, &a_hex, &b_hex);
        let p = Pairing { client_id: client.to_string(), client_name: name.clone(), secret };
        if let Err(msg) = self.host.save_pairing(&p) {
            log!("合言葉の保存に失敗: {}", msg);
            return send(w, &json!({"t": "error", "error": "save_failed"}));
        }
        let pc_proof = pair_proof(&k, "pc", &code, &a_hex, &b_hex);
        log!("ペアリング成功: client={} name={}", client, name);
        self.host.close_code_dialog();
        self.host.notify(&format!("「{}」とペアリングしました", name));
        send(w, &json!({"t": "paired", "proof": pc_proof, "pc_id": self.host.pc_id(), "pc_name": self.host.pc_name()}))
    }
}

fn send(w: &mut TcpStream, v: &Value) -> Result<(), String> {
    let mut s = v.to_string();
    s.push('\n');
    w.write_all(s.as_bytes()).map_err(|e| e.to_string())
}

fn read_json(r: &mut BufReader<TcpStream>) -> Result<Value, String> {
    let mut line = String::new();
    let n = r.by_ref().take(MAX_LINE as u64).read_line(&mut line).map_err(|e| e.to_string())?;
    if n == 0 {
        return Err("相手が先に切断した".into());
    }
    serde_json::from_str(line.trim()).map_err(|e| format!("JSON が読めない: {}", e))
}

#[cfg(test)]
pub mod tests {
    use super::*;
    use std::sync::atomic::AtomicBool;

    /// 偽の PC。SteamVR を触らず、呼ばれた操作だけ記録する。
    pub struct Fake {
        pub running: AtomicBool,
        pub quits: AtomicUsize,
        pub restarts: AtomicUsize,
        pub pairing: Mutex<Option<Pairing>>,
    }
    impl Fake {
        pub fn new() -> Arc<Fake> {
            Arc::new(Fake {
                running: AtomicBool::new(true),
                quits: AtomicUsize::new(0),
                restarts: AtomicUsize::new(0),
                pairing: Mutex::new(None),
            })
        }
    }
    impl Host for Fake {
        fn pc_name(&self) -> String { "TESTPC".into() }
        fn pc_id(&self) -> String { "pcid0123".into() }
        fn steamvr_running(&self) -> bool { self.running.load(Ordering::SeqCst) }
        fn vrchat_running(&self) -> bool { false }
        fn quit_steamvr(&self) -> OpResult {
            self.quits.fetch_add(1, Ordering::SeqCst);
            self.running.store(false, Ordering::SeqCst);
            OpResult { ok: true, state: "stopped", detail: "fake".into() }
        }
        fn restart_steamvr(&self) -> OpResult {
            self.restarts.fetch_add(1, Ordering::SeqCst);
            self.running.store(true, Ordering::SeqCst);
            OpResult { ok: true, state: "running", detail: "fake".into() }
        }
        fn load_pairing(&self) -> Option<Pairing> {
            self.pairing.lock().unwrap().as_ref().map(|p| Pairing {
                client_id: p.client_id.clone(),
                client_name: p.client_name.clone(),
                secret: p.secret.clone(),
            })
        }
        fn save_pairing(&self, p: &Pairing) -> Result<(), String> {
            *self.pairing.lock().unwrap() = Some(Pairing {
                client_id: p.client_id.clone(),
                client_name: p.client_name.clone(),
                secret: p.secret.clone(),
            });
            Ok(())
        }
        fn clear_pairing(&self) { *self.pairing.lock().unwrap() = None; }
        fn notify(&self, _: &str) {}
        fn close_code_dialog(&self) {}
    }

    fn start(host: Arc<Fake>) -> (Arc<Server>, u16) {
        let srv = Server::new(host);
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        drop(l);
        let s2 = Arc::clone(&srv);
        std::thread::spawn(move || { let _ = s2.run_control(port); });
        std::thread::sleep(Duration::from_millis(200));
        (srv, port)
    }

    fn connect(port: u16) -> (BufReader<TcpStream>, TcpStream, String) {
        let s = TcpStream::connect(("127.0.0.1", port)).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        let mut r = BufReader::new(s.try_clone().unwrap());
        let ch = read_json(&mut r).unwrap();
        let nonce = ch["nonce"].as_str().unwrap().to_string();
        (r, s, nonce)
    }

    /// Frame 役でペアリングし、合言葉を返す。
    fn do_pair(port: u16, client: &str, code: &str) -> Result<Vec<u8>, String> {
        let (mut r, mut w, _) = connect(port);
        let me = Ephemeral::new();
        send(&mut w, &json!({"t":"pair","client":client,"name":"Test Frame","pub":me.public_hex}))?;
        let m1 = read_json(&mut r)?;
        if m1["t"] != "pair1" { return Err(m1["error"].as_str().unwrap_or("?").to_string()); }
        let b = m1["pub"].as_str().unwrap().to_string();
        let k = me.agree(&b).unwrap();
        send(&mut w, &json!({"t":"pair2","proof":pair_proof(&k,"frame",code,&me.public_hex,&b)}))?;
        let m2 = read_json(&mut r)?;
        if m2["t"] != "paired" { return Err(m2["error"].as_str().unwrap_or("?").to_string()); }
        assert!(verify_hex(&k, &format!("pair-pc|{}|{}|{}", code, me.public_hex, b), m2["proof"].as_str().unwrap()));
        Ok(derive_secret(&k, code, &me.public_hex, &b))
    }

    fn request(port: u16, client: &str, secret: &[u8], cmd: &str) -> Value {
        let (mut r, mut w, nonce) = connect(port);
        let mac = request_mac(secret, &nonce, client, cmd);
        send(&mut w, &json!({"t":"req","client":client,"cmd":cmd,"mac":mac})).unwrap();
        read_json(&mut r).unwrap()
    }

    #[test]
    fn pair_then_operate() {
        let host = Fake::new();
        let (srv, port) = start(host.clone());
        // 受付前は拒否
        assert_eq!(do_pair(port, "frame1", "123456").unwrap_err(), "not_pairing");
        let code = srv.open_pairing();
        let secret = do_pair(port, "frame1", &code).unwrap();
        assert!(!srv.pairing_open(), "成功したら受付は閉じる");
        // 状態
        let st = request(port, "frame1", &secret, "status");
        assert_eq!(st["t"], "status");
        assert_eq!(st["steamvr"], "running");
        // 操作
        let q = request(port, "frame1", &secret, "quit");
        assert_eq!(q["ok"], true);
        assert_eq!(host.quits.load(Ordering::SeqCst), 1);
        let rs = request(port, "frame1", &secret, "restart");
        assert_eq!(rs["steamvr"], "running");
        assert_eq!(host.restarts.load(Ordering::SeqCst), 1);
        // 知らないコマンドは通らない(署名が正しくても)
        assert_eq!(request(port, "frame1", &secret, "shutdown")["error"], "unknown_cmd");
        // 別の Frame や間違った合言葉は通らない
        assert_eq!(request(port, "frame2", &secret, "quit")["error"], "auth");
        assert_eq!(request(port, "frame1", &[1u8; 32], "quit")["error"], "auth");
        assert_eq!(host.quits.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn wrong_code_limits_attempts() {
        let host = Fake::new();
        let (srv, port) = start(host.clone());
        let code = srv.open_pairing();
        let wrong = if code == "000000" { "000001" } else { "000000" };
        for i in 0..PAIR_MAX_ATTEMPTS {
            assert_eq!(do_pair(port, "frame1", wrong).unwrap_err(), "bad_code", "attempt {}", i);
        }
        // 使い切ったら正しいコードでも通らない
        assert_eq!(do_pair(port, "frame1", &code).unwrap_err(), "not_pairing");
        assert!(host.load_pairing().is_none());
    }

    #[test]
    fn unpaired_cannot_operate_and_replay_fails() {
        let host = Fake::new();
        let (srv, port) = start(host.clone());
        let secret = [3u8; 32];
        assert_eq!(request(port, "frame1", &secret, "quit")["error"], "auth");
        let code = srv.open_pairing();
        let secret = do_pair(port, "frame1", &code).unwrap();
        // 前の接続の署名を使い回す(リプレイ)
        let (mut r, mut w, nonce) = connect(port);
        let mac = request_mac(&secret, &nonce, "frame1", "quit");
        drop((r, w));
        let (mut r2, mut w2, _n2) = connect(port);
        send(&mut w2, &json!({"t":"req","client":"frame1","cmd":"quit","mac":mac})).unwrap();
        assert_eq!(read_json(&mut r2).unwrap()["error"], "auth");
        assert_eq!(host.quits.load(Ordering::SeqCst), 0);
    }

    #[test]
    fn discovery_reply_shape() {
        let host = Fake::new();
        let srv = Server::new(host);
        let v = srv.discovery_reply();
        assert_eq!(v["app"], "fsr");
        assert_eq!(v["name"], "TESTPC");
        assert_eq!(v["steamvr"], "running");
        assert_eq!(v["port"], CONTROL_PORT);
        assert_eq!(v["pairing"], false);
        assert!(v.get("secret").is_none());
    }
}
