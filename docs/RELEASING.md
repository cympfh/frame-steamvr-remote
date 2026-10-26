# リリースの手順

1. `Cargo.toml` の `version` と `CHANGELOG.md` を新しい版にそろえて、main に入れる。
2. きれいな clone で、Windows 用の exe をビルドする(WSL などから)。
   ```sh
   git clone https://github.com/cympfh/frame-steamvr-remote && cd frame-steamvr-remote
   cargo build --release --target x86_64-pc-windows-gnu
   sha256sum target/x86_64-pc-windows-gnu/release/frame-steamvr-remote.exe
   ```
3. タグを付けて Release を作り、exe を **`frame-steamvr-remote.exe` という名前のまま**添付する(README のダウンロードリンクが `releases/latest/download/frame-steamvr-remote.exe` を指しているため)。
   ```sh
   gh release create vX.Y.Z --target main --title "frame-steamvr-remote X.Y.Z" --notes-file notes.md \
     target/x86_64-pc-windows-gnu/release/frame-steamvr-remote.exe
   ```
   ノートの最後に SHA-256 を書く。
4. `curl -L` で README のリンクをダウンロードして、SHA-256 が合うか確かめる。

Frame 側は `install.sh` が main を取りに行くので、別途のファイルは要らない。
