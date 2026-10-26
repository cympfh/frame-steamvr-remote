#!/bin/bash
# SPDX-License-Identifier: MIT
# frame-steamvr-remote を Steam Frame に入れる・外す。Frame の Konsole で、ふつうのユーザーで実行する(sudo 不要)。
#
#   入れる:  curl -fsSL https://raw.githubusercontent.com/cympfh/frame-steamvr-remote/main/install.sh | bash
#   外す:    curl -fsSL https://raw.githubusercontent.com/cympfh/frame-steamvr-remote/main/install.sh | bash -s -- --uninstall
#
# 入れるもの(全部ユーザー領域。pacman も steamos-readonly も使わない):
#   ~/.local/opt/frame-steamvr-remote/     本体(Python)
#   ~/.local/bin/frame-steamvr-remote      コマンド
#   ~/.local/share/frame-steamvr-remote/   SteamVR のクイックランチ用の登録ファイルと画像
#   ~/.local/share/applications/frame-steamvr-remote.desktop と アイコン
#   ~/.config/openvr/config/appconfig.json に登録ファイルの場所を 1 行足す(足す前にバックアップを取る)
# 自動起動(systemd)は使わない。必要なときに SteamVR のクイックランチのタイル、またはメニューから開く。
# 外すとき(--uninstall)は消さずに ~/.local/share/frame-steamvr-remote-uninstalled/<日時>/ に移す。
# ペアリング情報(~/.config/frame-steamvr-remote/)とログ(~/.local/state/frame-steamvr-remote/)は残す。
set -euo pipefail

name=frame-steamvr-remote
repo=cympfh/$name
ref=${FRAME_TOOL_REF:-main}
app_key=dev.cympfh.frame-steamvr-remote
opt="$HOME/.local/opt/$name"
bin="$HOME/.local/bin/$name"
share="$HOME/.local/share/$name"
desktop="$HOME/.local/share/applications/$name.desktop"
icon="$HOME/.local/share/icons/hicolor/128x128/apps/$name.png"
appconfig="${FSR_APPCONFIG:-$HOME/.config/openvr/config/appconfig.json}"
manifest="$share/$name.vrmanifest"

[[ "$(id -u)" != 0 ]] || { echo "root では動かしません。sudo なしで実行してください。" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 が見つかりません。" >&2; exit 1; }

# appconfig.json の manifest_paths に、登録ファイルを足す(add)・外す(remove)。ほかの項目は変えない。
edit_appconfig() {
    python3 - "$1" "$appconfig" "$manifest" <<'PY'
import json, os, sys
mode, path, manifest = sys.argv[1:4]
try:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
except FileNotFoundError:
    cfg = {}
paths = cfg.get("manifest_paths") or []
if mode == "add" and manifest not in paths:
    paths.append(manifest)
elif mode == "remove":
    paths = [p for p in paths if p != manifest]
else:
    sys.exit(0)
cfg["manifest_paths"] = paths
os.makedirs(os.path.dirname(path), exist_ok=True)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=3)
    f.write("\n")
os.replace(tmp, path)
PY
}

backup_appconfig() {
    [[ -f "$appconfig" ]] || return 0
    mkdir -p "$share/backup"
    cp -p "$appconfig" "$share/backup/appconfig.json.$(date +%Y%m%d-%H%M%S)"
}

if [[ "${1:-}" == "--uninstall" ]]; then
    aside="$HOME/.local/share/$name-uninstalled/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$aside"
    if [[ -f "$appconfig" ]]; then
        backup_appconfig
        edit_appconfig remove
    fi
    for pair in "opt:$opt" "bin-link:$bin" "desktop-entry:$desktop" "icon:$icon" "share:$share"; do
        label="${pair%%:*}"; f="${pair#*:}"
        if [[ -e "$f" || -L "$f" ]]; then mv "$f" "$aside/$label"; fi
    done
    echo "$name を外しました。"
    echo "  外したファイルは $aside に移してあります(要らなければ消してOK)。"
    echo "  appconfig.json から登録を外しました(バックアップは $aside/share/backup/)。SteamVR を次に起動したときタイルが消えます。"
    echo "  ペアリング情報 ~/.config/$name とログ ~/.local/state/$name はそのまま残しています。"
    exit 0
fi

# ファイルの出どころ: このスクリプトの隣にあればそれ、無ければ GitHub から tar.gz を取ってくる(git は使わない)
here=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [[ -f "$here/frame/fsr_core.py" ]] || here=""
fi
if [[ -z "$here" ]]; then
    cache="${XDG_CACHE_HOME:-$HOME/.cache}"
    mkdir -p "$cache"
    tmp="$(mktemp -d "$cache/$name-install.XXXXXX")"
    trap 'rm -rf "$tmp"' EXIT
    if [[ -n "${FRAME_TOOL_TARBALL:-}" ]]; then
        cp "$FRAME_TOOL_TARBALL" "$tmp/src.tar.gz"
    else
        echo "ダウンロード中: https://github.com/$repo ($ref)"
        curl -fsSL "https://codeload.github.com/$repo/tar.gz/$ref" -o "$tmp/src.tar.gz"
    fi
    mkdir -p "$tmp/src"
    tar xzf "$tmp/src.tar.gz" -C "$tmp/src" --strip-components=1
    here="$tmp/src"
fi

mkdir -p "$opt" "$share" "$(dirname "$bin")" "$(dirname "$desktop")" "$(dirname "$icon")"
for f in fsr_core.py fsr_gui.py frame-steamvr-remote; do
    install -m755 "$here/frame/$f" "$opt/$f.new"
    mv -f "$opt/$f.new" "$opt/$f"
done
install -m644 "$here/assets/logo.png" "$opt/logo.png"
install -m644 "$here/assets/logo.png" "$icon"
install -m644 "$here/frame/assets/header.png" "$share/header.png"
install -m644 "$here/frame/assets/capsule.png" "$share/capsule.png"
ln -sfn "$opt/frame-steamvr-remote" "$bin"

cat > "$manifest" <<JSON
{
  "source": "user",
  "applications": [
    {
      "app_key": "$app_key",
      "launch_type": "binary",
      "binary_path_linux": "$bin",
      "binary_path_linux_arm": "$bin",
      "image_path": "header.png",
      "image_path_capsule": "capsule.png",
      "is_dashboard_overlay": false,
      "show_overlay_in_quicklaunch": true,
      "strings": { "en_us": { "name": "SteamVR Remote", "description": "Quit or restart SteamVR on your PC from the Frame" } }
    }
  ]
}
JSON

cat > "$desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=SteamVR リモート
Comment=PC の SteamVR を Frame から終了・再起動する
Exec="$bin"
Icon=$name
Terminal=false
Categories=Utility;
DESKTOP

backup_appconfig
edit_appconfig add
command -v update-desktop-database >/dev/null && update-desktop-database "$(dirname "$desktop")" 2>/dev/null || true

gi_note=""
python3 -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null || gi_note="  ※ 画面(GTK)が使えないようです。コマンド($name status など)は使えます。"

cat <<MSG

$name を入れました。
  開く:        デスクトップモードのメニュー「SteamVR リモート」、または SteamVR のクイックランチのタイル(SteamVR を次に起動したときから)
  最初に:      PC のトレイのアイコンを右クリック →「Frame とペアリングする」→ 出た 6 桁のコードを Frame の画面に入力
  コマンド:    $name status / quit / restart / pair 123456
  設定:        ~/.config/$name/   ログ: ~/.local/state/$name/app.log
  外す:        curl -fsSL https://raw.githubusercontent.com/$repo/main/install.sh | bash -s -- --uninstall
$gi_note
MSG
