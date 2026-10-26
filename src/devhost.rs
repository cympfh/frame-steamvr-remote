//! 開発・テスト用の偽の PC(Windows 以外で `--dev-server` のときだけ使う)。
//! 本物のプロセスには一切触れず、SteamVR の状態をメモリ上で切り替えるだけ。
use crate::server::{Host, OpResult, Pairing};
use std::sync::Mutex;

pub struct DevHost {
    pub running: Mutex<bool>,
    pub paired: Mutex<Option<Pairing>>,
}

impl DevHost {
    pub fn new() -> Self {
        DevHost { running: Mutex::new(true), paired: Mutex::new(None) }
    }
}

impl Host for DevHost {
    fn pc_name(&self) -> String {
        "DEV-PC".into()
    }
    fn pc_id(&self) -> String {
        "0123456789abcdef".into()
    }
    fn steamvr_running(&self) -> bool {
        *self.running.lock().unwrap()
    }
    fn vrchat_running(&self) -> bool {
        false
    }
    fn quit_steamvr(&self) -> OpResult {
        *self.running.lock().unwrap() = false;
        OpResult { ok: true, state: "stopped", detail: "dev: 偽の終了".into() }
    }
    fn restart_steamvr(&self) -> OpResult {
        *self.running.lock().unwrap() = true;
        OpResult { ok: true, state: "running", detail: "dev: 偽の再起動".into() }
    }
    fn load_pairing(&self) -> Option<Pairing> {
        self.paired.lock().unwrap().as_ref().map(|p| Pairing {
            client_id: p.client_id.clone(),
            client_name: p.client_name.clone(),
            secret: p.secret.clone(),
        })
    }
    fn save_pairing(&self, p: &Pairing) -> Result<(), String> {
        *self.paired.lock().unwrap() =
            Some(Pairing { client_id: p.client_id.clone(), client_name: p.client_name.clone(), secret: p.secret.clone() });
        Ok(())
    }
    fn clear_pairing(&self) {
        *self.paired.lock().unwrap() = None;
    }
    fn notify(&self, _msg: &str) {}
    fn close_code_dialog(&self) {}
}

/// `frame-steamvr-remote --dev-server 発見ポート 操作ポート`
/// 準備ができたら標準出力に `READY <ペアリングコード>` を出す(テスト専用。本物のアプリはコードを出さない)。
pub fn run(args: &[String]) {
    use crate::server::Server;
    use std::sync::Arc;
    let disc: u16 = args.get(0).and_then(|s| s.parse().ok()).unwrap_or(47650);
    let ctrl: u16 = args.get(1).and_then(|s| s.parse().ok()).unwrap_or(47651);
    let srv = Server::with_port(Arc::new(DevHost::new()), ctrl);
    let code = srv.open_pairing();
    {
        let srv = srv.clone();
        std::thread::spawn(move || {
            let _ = srv.run_discovery(disc);
        });
    }
    {
        let srv = srv.clone();
        std::thread::spawn(move || {
            let _ = srv.run_control(ctrl);
        });
    }
    std::thread::sleep(std::time::Duration::from_millis(300));
    println!("READY {}", code);
    use std::io::Write;
    let _ = std::io::stdout().flush();
    loop {
        std::thread::sleep(std::time::Duration::from_secs(3600));
    }
}
