//! 小さな設定ファイル settings.json(秘密は入れない)。
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Default, Clone, Debug)]
pub struct Settings {
    /// この PC を見分けるための ID(秘密ではない)
    #[serde(default)]
    pub pc_id: String,
    /// 初回だけ自動起動を ON にしたか(あとで OFF にしたら尊重する)
    #[serde(default)]
    pub autostart_initialized: bool,
}

fn path() -> std::path::PathBuf {
    crate::paths::data_dir().join("settings.json")
}

pub fn load() -> Settings {
    let mut s: Settings = std::fs::read_to_string(path())
        .ok()
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_default();
    if s.pc_id.len() != 16 {
        s.pc_id = crate::proto::random_hex(8);
        save(&s);
    }
    s
}

pub fn save(s: &Settings) {
    if let Ok(t) = serde_json::to_string_pretty(s) {
        let tmp = path().with_extension("json.tmp");
        if std::fs::write(&tmp, t).is_ok() {
            let _ = std::fs::rename(&tmp, path());
        }
    }
}
