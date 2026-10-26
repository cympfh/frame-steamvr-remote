fn main() {
    // Windows の exe にロゴのアイコンを埋め込む(エクスプローラー・トレイ用)
    println!("cargo:rerun-if-changed=assets/app.rc");
    println!("cargo:rerun-if-changed=assets/logo.ico");
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        embed_resource::compile("assets/app.rc", embed_resource::NONE)
            .manifest_optional()
            .expect("アイコンを exe に埋め込めない: x86_64-w64-mingw32-windres か rc.exe が要る");
    }
}
