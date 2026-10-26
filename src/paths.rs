//! 設定・ログの置き場所。Windows は %APPDATA%\frame-steamvr-remote。
use std::path::PathBuf;

pub fn data_dir() -> PathBuf {
    let base = std::env::var_os("FSR_DATA_DIR")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("APPDATA").map(|a| PathBuf::from(a).join("frame-steamvr-remote")))
        .or_else(|| {
            std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local/state/frame-steamvr-remote-pc"))
        })
        .unwrap_or_else(|| PathBuf::from("."));
    let _ = std::fs::create_dir_all(&base);
    base
}
