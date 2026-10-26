//! 「どのプロセスが SteamVR のものか」の判定。VRChat は絶対に含めない。
use std::sync::OnceLock;

/// SteamVR が起動するプロセス名(小文字)。
pub const NAMES: &[&str] = &[
    "vrserver.exe",
    "vrmonitor.exe",
    "vrcompositor.exe",
    "vrdashboard.exe",
    "vrstartup.exe",
    "vrwebhelper.exe",
    "vrservice.exe",
];

pub const DEFAULT_MARK: &str = "\\steamapps\\common\\steamvr\\";

static MARK: OnceLock<String> = OnceLock::new();

/// 実際の判定に使う「パスに含まれる目印」。テスト用の偽プロセス検証でだけ変えられる。
pub fn mark() -> &'static str {
    MARK.get().map(|s| s.as_str()).unwrap_or(DEFAULT_MARK)
}

pub fn set_test_mark(m: &str) {
    let _ = MARK.set(m.to_lowercase());
}

/// 名前が SteamVR のもので、かつ実行ファイルが SteamVR のフォルダにあるときだけ true。
pub fn is_steamvr_process(name: &str, path: &str, mark: &str) -> bool {
    let n = name.to_lowercase();
    let p = path.to_lowercase().replace('/', "\\");
    if n.contains("vrchat") || p.contains("vrchat") {
        return false;
    }
    NAMES.contains(&n.as_str()) && p.contains(mark)
}

pub fn is_vrchat(name: &str) -> bool {
    name.eq_ignore_ascii_case("vrchat.exe")
}

#[cfg(test)]
mod tests {
    use super::*;
    const M: &str = DEFAULT_MARK;

    #[test]
    fn matches_real_steamvr() {
        let d = "C:\\Program Files (x86)\\Steam\\steamapps\\common\\SteamVR\\bin\\win64\\";
        for n in ["vrserver.exe", "VRMonitor.exe", "vrcompositor.exe", "vrdashboard.exe", "vrwebhelper.exe"] {
            assert!(is_steamvr_process(n, &format!("{}{}", d, n), M), "{}", n);
        }
        let lib = "D:\\Games\\SteamLibrary\\steamapps\\common\\SteamVR\\bin\\vrwebhelper\\win64\\vrwebhelper.exe";
        assert!(is_steamvr_process("vrwebhelper.exe", lib, M));
    }

    #[test]
    fn never_vrchat_or_others() {
        let vc = "C:\\Program Files (x86)\\Steam\\steamapps\\common\\VRChat\\VRChat.exe";
        assert!(!is_steamvr_process("VRChat.exe", vc, M));
        assert!(!is_steamvr_process("vrserver.exe", vc, M));
        // 名前は合っていても SteamVR のフォルダ以外なら対象外
        assert!(!is_steamvr_process("vrserver.exe", "C:\\tools\\vrserver.exe", M));
        // 同じフォルダでも名前が一覧にない(オーバーレイ等)なら対象外
        let d = "C:\\Program Files (x86)\\Steam\\steamapps\\common\\SteamVR\\tools\\x\\other.exe";
        assert!(!is_steamvr_process("other.exe", d, M));
        assert!(!is_steamvr_process("steam.exe", "C:\\Program Files (x86)\\Steam\\steam.exe", M));
        assert!(!is_steamvr_process("steamwebhelper.exe", "C:\\Program Files (x86)\\Steam\\bin\\cef\\steamwebhelper.exe", M));
        // 偽装: パスに vrchat を含むものは拒否
        assert!(!is_steamvr_process("vrserver.exe", "C:\\steamapps\\common\\steamvr\\vrchat\\vrserver.exe", M));
    }

    #[test]
    fn vrchat_detect() {
        assert!(is_vrchat("VRChat.exe"));
        assert!(!is_vrchat("vrserver.exe"));
    }
}
