#!/bin/bash
# SPDX-License-Identifier: MIT
# frame-steamvr-remote を Steam Frame に入れる・外す。Frame の Konsole で、ふつうのユーザーで実行する(sudo 不要)。
#
#   入れる:  curl -fsSL https://raw.githubusercontent.com/cympfh/frame-steamvr-remote/main/install.sh | bash
#   外す:    curl -fsSL https://raw.githubusercontent.com/cympfh/frame-steamvr-remote/main/install.sh | bash -s -- --uninstall
#   Steam ライブラリへの追加をしない: ... | bash -s -- --no-steam-shortcut
#
# 入れるもの(全部ユーザー領域。pacman も steamos-readonly も使わない):
#   ~/.local/opt/frame-steamvr-remote/     本体(Python)
#   ~/.local/bin/frame-steamvr-remote      コマンド
#   ~/.local/share/frame-steamvr-remote/   SteamVR のクイックランチ用の登録ファイルと画像
#   ~/.local/share/applications/frame-steamvr-remote.desktop と アイコン
#   ~/.config/openvr/config/appconfig.json に登録ファイルの場所を 1 行足す(足す前にバックアップを取る)
#   SteamVR が動いていれば、再起動しなくても出るよう、その場で SteamVR にも登録する(vrcmd --appmanifest)
#   Steam が動いていれば、ライブラリに「SteamVR リモート」を追加する(steamos-add-to-steam。すでにあれば何もしない)
# 自動起動(systemd)は使わない。必要なときに SteamVR のクイックランチのタイル、またはメニューから開く。
# PC の画面が見えない(Frame と PC の SteamVR がつながっていない)ときでも、Frame の中だけで開けるようにするのが目的。
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
steam_shortcut=1
[[ "${1:-}" == "--no-steam-shortcut" ]] && steam_shortcut=0
vrbin=/opt/steamvr/bin/linuxarm64
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

# いま動いている SteamVR に登録ファイルを渡す(SteamVR を再起動しなくてもクイックランチにタイルが出る)。失敗しても先へ進む。
register_live() {
    [[ -x "$vrbin/vrcmd" ]] || return 1
    pgrep -x vrserver >/dev/null 2>&1 || return 1
    LD_LIBRARY_PATH="$vrbin" timeout 20 "$vrbin/vrcmd" --appmanifest "$manifest" >/dev/null 2>&1 || return 1
    LD_LIBRARY_PATH="$vrbin" timeout 20 "$vrbin/vrcmd" --dumpapps 2>/dev/null | grep -q "$app_key"
}

# shortcuts.vdf(Steam のライブラリの非 Steam ショートカット)に、表示名のエントリがあるか
steam_has_shortcut() {
    python3 - "$HOME/.local/share/Steam/userdata" "$1" <<'PY'
import glob, sys
root, label = sys.argv[1:3]
needle = b"AppName\x00" + label.encode() + b"\x00"
for f in glob.glob(root + "/*/config/shortcuts.vdf"):
    try:
        if needle in open(f, "rb").read():
            sys.exit(0)
    except OSError:
        pass
sys.exit(1)
PY
}

# Steam のライブラリに追加した直後、Steam が付けた appid を読んで、表紙画像を置く(Steam の grid 画像の決まり)
put_steam_art() {
    python3 - "$HOME/.local/share/Steam/userdata" "$1" "$share" <<'PY'
import glob, os, shutil, struct, sys
root, label, share = sys.argv[1:4]
needle = b"AppName\x00" + label.encode() + b"\x00"
for f in glob.glob(root + "/*/config/shortcuts.vdf"):
    data = open(f, "rb").read()
    i = data.find(needle)
    if i < 0:
        continue
    j = data.rfind(b"\x02appid\x00", 0, i + len(needle) + 1)
    if j < 0:
        j = data.find(b"\x02appid\x00", i)
    if j < 0:
        continue
    appid = struct.unpack("<I", data[j + 7:j + 11])[0]
    grid = os.path.join(os.path.dirname(f), "grid")
    os.makedirs(grid, exist_ok=True)
    for src, dst in (("capsule.png", "%dp.png" % appid), ("header.png", "%d.png" % appid)):
        shutil.copyfile(os.path.join(share, src), os.path.join(grid, dst))
    print(appid)
    sys.exit(0)
sys.exit(1)
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
      "strings": { "en_us": { "name": "SteamVR Remote", "description": "Quit or restart SteamVR on your PC from the Frame" }, "ja_jp": { "name": "SteamVR リモート", "description": "PC の SteamVR を Frame から終了・再起動する" } }
    }
  ]
}
JSON

cat > "$desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=SteamVR リモート
Comment=PC の SteamVR を Frame から終了・再起動する
Exec=$bin
Icon=$name
Terminal=false
Categories=Utility;
DESKTOP

backup_appconfig
edit_appconfig add
command -v update-desktop-database >/dev/null && update-desktop-database "$(dirname "$desktop")" 2>/dev/null || true

# SteamVR が動いていれば、その場で登録する(動いていなければ、次に起動したとき appconfig.json から読まれる)
if register_live; then
    live_note="SteamVR に登録しました(クイックランチに出ます)"
else
    live_note="SteamVR には登録できませんでした(動いていない、または失敗)。SteamVR を次に起動したときにタイルが出ます"
fi

# Steam のライブラリ(非 Steam ショートカット)に追加する。Steam が動いていてまだ無いときだけ。
label="SteamVR リモート"
shortcut_note="Steam のライブラリへの追加はしていません"
if [[ "$steam_shortcut" == 1 ]]; then
    if steam_has_shortcut "$label"; then
        put_steam_art "$label" >/dev/null 2>&1 || true
        shortcut_note="Steam のライブラリにすでにあります"
    elif pgrep -x steam >/dev/null 2>&1 && command -v steamos-add-to-steam >/dev/null; then
        steamos-add-to-steam "$desktop" >/dev/null 2>&1 || true
        for _ in $(seq 1 20); do steam_has_shortcut "$label" && break; sleep 1; done
        if steam_has_shortcut "$label"; then
            put_steam_art "$label" >/dev/null 2>&1 || true
            shortcut_note="Steam のライブラリに追加しました(「非 Steam」のゲームとして出ます。表紙の絵は次に Steam のライブラリを開いたとき反映されます)"
        else
            shortcut_note="Steam のライブラリへは追加できませんでした(Steam を次に再起動したあと、もう一度このスクリプトを実行してください)"
        fi
    else
        shortcut_note="Steam が動いていないので、ライブラリへの追加は見送りました(Steam が動いているときにもう一度このスクリプトを実行してください)"
    fi
fi

gi_note=""
python3 -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null || gi_note="  ※ 画面(GTK)が使えないようです。コマンド($name status など)は使えます。"

cat <<MSG

$name を入れました。
  開く(Frame だけで。PC の画面が見えなくてOK):
               ・Steam のライブラリ →「SteamVR リモート」(非 Steam のゲーム)
               ・SteamVR のダッシュボード → クイックランチのタイル「SteamVR Remote」(一覧の一番後ろにあることがあります。一度開くと前に出ます)
               ・デスクトップモードのアプリメニュー「SteamVR リモート」
  登録:        $live_note
               $shortcut_note
  最初に:      PC のトレイのアイコンを右クリック →「Frame とペアリングする」→ 出た 6 桁のコードを Frame の画面に入力
  コマンド:    $name status / quit / restart / pair 123456
  設定:        ~/.config/$name/   ログ: ~/.local/state/$name/app.log
  外す:        curl -fsSL https://raw.githubusercontent.com/$repo/main/install.sh | bash -s -- --uninstall
$gi_note
MSG
