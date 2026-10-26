#!/bin/bash
# SPDX-License-Identifier: MIT
# frame-steamvr-remote を Steam Frame に入れる・外す。Frame の Konsole で、ふつうのユーザーで実行する(sudo 不要)。
#
#   入れる:  curl -fsSL https://raw.githubusercontent.com/cympfh/frame-steamvr-remote/main/install.sh | bash
#   外す:    curl -fsSL https://raw.githubusercontent.com/cympfh/frame-steamvr-remote/main/install.sh | bash -s -- --uninstall
#
# 入れるもの(全部ユーザー領域。pacman も steamos-readonly も使わない):
#   ~/.local/opt/frame-steamvr-remote/                          本体(Python)
#   ~/.local/bin/frame-steamvr-remote                           コマンド
#   ~/.local/share/frame-steamvr-remote/                        画像など
#   ~/.local/share/applications/frame-steamvr-remote.desktop と アイコン(デスクトップモード用)
#   ~/.config/systemd/user/frame-steamvr-remote-overlay.service SteamVR のダッシュボードのパネル。SteamVR と一緒に自動で起動する
#       (frame-mic-tuner などと同じ仕組み: After/PartOf=steamvr.service、WantedBy=steamvr.service)
# SteamVR が動いているときは、そのパネルをその場で起動する(SteamVR 自体は再起動しない)。
# 0.1.x で入れた、クイックランチのタイル(登録ファイルと appconfig.json の 1 行)は、バックアップを取って外す。
# Steam のライブラリの「SteamVR リモート」(0.1.x が足した非 Steam ショートカット)には触らない(Steam が動いている間は
# shortcuts.vdf を書き換えられないため)。要らなければライブラリから手で外す。
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
unit_name=$name-overlay.service
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit="$unit_dir/$unit_name"
appconfig="${FSR_APPCONFIG:-$HOME/.config/openvr/config/appconfig.json}"
old_manifest="$share/$name.vrmanifest"   # 0.1.x のクイックランチ用。もう使わない

[[ "$(id -u)" != 0 ]] || { echo "root では動かしません。sudo なしで実行してください。" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 が見つかりません。" >&2; exit 1; }

# appconfig.json の manifest_paths から、0.1.x の登録ファイルを外す。ほかの項目は変えない。外したら "removed" と出す。
remove_old_manifest() {
    python3 - "$appconfig" "$old_manifest" <<'PY'
import json, os, sys
path, manifest = sys.argv[1:3]
try:
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
except (FileNotFoundError, ValueError):
    sys.exit(0)
paths = cfg.get("manifest_paths") or []
if manifest not in paths:
    sys.exit(0)
cfg["manifest_paths"] = [p for p in paths if p != manifest]
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=3)
    f.write("\n")
os.replace(tmp, path)
print("removed")
PY
}

backup_appconfig() {
    [[ -f "$appconfig" ]] || return 0
    mkdir -p "$share/backup"
    cp -p "$appconfig" "$share/backup/appconfig.json.$(date +%Y%m%d-%H%M%S)"
}

# systemctl --user を呼ぶ(テスト用に FSR_NO_SYSTEMCTL=1 で何もしない)。失敗しても先へ進む。
sc() {
    [[ "${FSR_NO_SYSTEMCTL:-}" == 1 ]] && return 0
    command -v systemctl >/dev/null || return 1
    systemctl --user "$@"
}

if [[ "${1:-}" == "--uninstall" ]]; then
    aside="$HOME/.local/share/$name-uninstalled/$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$aside"
    # パネルを止めて、自動起動を外す(SteamVR 自体には触らない)
    sc disable --now "$unit_name" >/dev/null 2>&1 || true
    if [[ -e "$unit" || -L "$unit" ]]; then mv "$unit" "$aside/systemd-unit"; fi
    sc daemon-reload >/dev/null 2>&1 || true
    if [[ -f "$appconfig" ]]; then
        backup_appconfig
        remove_old_manifest >/dev/null
    fi
    for pair in "opt:$opt" "bin-link:$bin" "desktop-entry:$desktop" "icon:$icon" "share:$share"; do
        label="${pair%%:*}"; f="${pair#*:}"
        if [[ -e "$f" || -L "$f" ]]; then mv "$f" "$aside/$label"; fi
    done
    echo "$name を外しました。"
    echo "  パネルを止めて、SteamVR と一緒の自動起動を外しました。SteamVR のダッシュボードのパネルは、次に SteamVR を起動したときから出ません(いま動いている SteamVR からは、止めた時点で消えます)。"
    echo "  外したファイルは $aside に移してあります(要らなければ消してOK)。"
    echo "  ペアリング情報 ~/.config/$name とログ ~/.local/state/$name はそのまま残しています。"
    echo "  Steam のライブラリに 0.1.x で足した「SteamVR リモート」が残っていれば、ライブラリから手で外してください(このスクリプトは Steam の設定ファイルを書き換えません)。"
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

mkdir -p "$opt" "$share" "$(dirname "$bin")" "$(dirname "$desktop")" "$(dirname "$icon")" "$unit_dir"
for f in fsr_core.py fsr_arm.py fsr_model.py fsr_openvr.py fsr_cairo.py fsr_overlay.py fsr_gui.py frame-steamvr-remote; do
    install -m755 "$here/frame/$f" "$opt/$f.new"
    mv -f "$opt/$f.new" "$opt/$f"
done
install -m644 "$here/assets/logo.png" "$opt/logo.png"
install -m644 "$here/assets/logo.png" "$icon"
ln -sfn "$opt/frame-steamvr-remote" "$bin"

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
command -v update-desktop-database >/dev/null && update-desktop-database "$(dirname "$desktop")" 2>/dev/null || true

# 0.1.x のクイックランチのタイルを外す(appconfig.json はバックアップを取ってから。登録ファイルは消さず脇へ移す)
old_note=""
if [[ -f "$appconfig" ]] && grep -qF "$old_manifest" "$appconfig" 2>/dev/null; then
    backup_appconfig
    [[ "$(remove_old_manifest)" == removed ]] && old_note="0.1.x のクイックランチのタイルを外しました(いま動いている SteamVR からは、次に SteamVR を起動したときに消えます)"
fi
if [[ -f "$old_manifest" ]]; then
    mkdir -p "$share/old"
    mv "$old_manifest" "$share/old/$name.vrmanifest.$(date +%Y%m%d-%H%M%S)"
fi
for f in header.png capsule.png; do   # 0.1.x のタイル用の画像。消さずに脇へ移す
    if [[ -f "$share/$f" ]]; then mkdir -p "$share/old"; mv "$share/$f" "$share/old/$f"; fi
done

# SteamVR のダッシュボードのパネルの常駐(systemd のユーザーユニット。ほかの Frame 用ツールと同じ作り)
cat > "$unit" <<UNIT
[Unit]
Description=SteamVR リモート(SteamVR のダッシュボードのパネル)
After=steamvr.service
PartOf=steamvr.service

[Service]
ExecStart=%h/.local/bin/$name overlay
Restart=always
RestartSec=5
RestartPreventExitStatus=3
SuccessExitStatus=3

[Install]
WantedBy=steamvr.service
UNIT
sc daemon-reload || true
unit_note=""
if sc enable "$unit_name" >/dev/null 2>&1; then
    unit_note="SteamVR と一緒に自動で起動するようにしました"
else
    unit_note="自動起動の設定に失敗しました(systemctl --user enable $unit_name を手で実行してください)"
fi
# SteamVR が動いているときだけ、いまパネルを起動する(この操作で SteamVR は再起動しない)。すでに動いていれば新しい版で入れ替える。
if [[ "${FSR_NO_SYSTEMCTL:-}" == 1 ]]; then
    live_note="(systemctl は使いませんでした)"
elif systemctl --user is-active --quiet steamvr.service 2>/dev/null; then
    if sc restart "$unit_name" >/dev/null 2>&1; then
        live_note="いま動いている SteamVR のダッシュボードにパネルを出しました"
    else
        live_note="パネルの起動に失敗しました(journalctl --user -u $unit_name を見てください)"
    fi
else
    live_note="SteamVR が動いていないので、パネルは SteamVR を次に起動したときに出ます"
fi

gi_note=""
python3 -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null || gi_note="  ※ デスクトップ用の画面(GTK)が使えないようです。ヘッドセットのパネルとコマンドには関係ありません。"

cat <<MSG

$name を入れました。
  開く(Frame だけで。PC の画面が見えなくてOK):
               SteamVR のダッシュボードを開くと「SteamVR リモート」のパネルがあります(コントローラーのレーザーで押します)
               デスクトップモードのアプリメニュー「SteamVR リモート」でも開けます
  自動起動:    $unit_note
  いま:        $live_note
  ${old_note:+旧版:        $old_note}
  最初に:      PC のトレイのアイコンを右クリック →「Frame とペアリングする」→ 出た 6 桁のコードをパネルのテンキーで入力
  安全のため:  終了と再起動は、2 回押さないと実行されません(1 回目で「もう一度押すと…」に変わり、5 秒で元に戻ります)
  コマンド:    $name status / quit / restart / pair 123456 / overlay
  自動起動を止める: systemctl --user disable --now $unit_name
  設定:        ~/.config/$name/   ログ: ~/.local/state/$name/app.log
  外す:        curl -fsSL https://raw.githubusercontent.com/$repo/main/install.sh | bash -s -- --uninstall
$gi_note
MSG
