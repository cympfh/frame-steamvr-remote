#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]
//! frame-steamvr-remote (PC 側)
//! Steam Frame から、この PC の SteamVR を終了・再起動できるようにするトレイ常駐アプリ。

#[cfg(not(windows))]
mod devhost;
mod applog;
mod paths;
mod proto;
mod server;
mod settings;
mod steamvr;
#[cfg(windows)]
mod win;

#[cfg(windows)]
fn main() {
    use std::sync::Arc;
    applog::init();
    std::panic::set_hook(Box::new(|info| {
        log!("パニック: {}", info);
    }));
    let args: Vec<String> = std::env::args().collect();
    log!("起動 v{} 引数={:?}", env!("CARGO_PKG_VERSION"), &args[1..]);

    // 開発用: 偽の SteamVR プロセス(目印のフォルダ名つき)だけを対象に終了処理を試す。本物には触れない。
    if args.len() >= 3 && args[1] == "--selftest-quit" {
        selftest_quit(&args[2]);
        return;
    }
    if !win::single_instance() {
        log!("すでに起動しているので終了する");
        return;
    }
    let s = settings::load();
    win::first_run_autostart();
    let host = Arc::new(win::WinHost::new(s.pc_id.clone()));
    let srv = server::Server::new(host.clone());
    {
        let srv = srv.clone();
        std::thread::spawn(move || loop {
            if let Err(e) = srv.run_discovery(proto::DISCOVERY_PORT) {
                log!("発見の待ち受けに失敗: {} (10 秒後に再試行)", e);
                std::thread::sleep(std::time::Duration::from_secs(10));
            }
        });
    }
    {
        let srv = srv.clone();
        std::thread::spawn(move || loop {
            if let Err(e) = srv.run_control(proto::CONTROL_PORT) {
                log!("操作の待ち受けに失敗: {} (10 秒後に再試行)", e);
                std::thread::sleep(std::time::Duration::from_secs(10));
            }
        });
    }
    win::run(srv, host);
    log!("終了");
}

#[cfg(windows)]
fn selftest_quit(mark: &str) {
    // 目印は「steamvr」を含む本物のパスと混ざらない、専用の名前でなければ拒否する
    if !mark.to_lowercase().contains("fsr-selftest") || mark.to_lowercase().contains("steamapps") {
        log!("selftest: 目印が安全でないので中止: {}", mark);
        return;
    }
    steamvr::set_test_mark(mark);
    let before = win::steamvr_procs();
    log!("selftest: 対象の偽プロセス {} 個", before.len());
    let r = win::quit_steamvr();
    log!("selftest: 結果 ok={} state={} detail={}", r.ok, r.state, r.detail);
}

#[cfg(not(windows))]
fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() >= 2 && args[1] == "--dev-server" {
        devhost::run(&args[2..]);
        return;
    }
    applog::init();
    eprintln!("frame-steamvr-remote の PC 側は Windows 専用です。(テストは `cargo test`)");
}
