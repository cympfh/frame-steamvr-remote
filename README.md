<p align="center"><img src="assets/logo.png" width="120" alt="logo"></p>

# frame-steamvr-remote

**Steam Frame から、PC の SteamVR を終了・再起動するアプリ**です。

PC で先に SteamVR が動いていると、あとから Frame をつなげないことがあります。
そんなとき、Frame の画面からボタン 1 つで PC の SteamVR を止めて、つなぎ直せます。

- Frame が **同じ Wi-Fi/LAN の PC を自動で見つけます**(IP アドレスの入力は不要)
- PC 側はタスクトレイに常駐する小さなアプリ(Windows のログイン時に自動で起動)
- 最初に 1 回だけ、PC の画面に出る **6 桁のコード** を Frame に入力してペアリング。以降はペアリングした Frame だけが操作できます
- 操作できるのは「SteamVR の終了」「SteamVR の再起動」「状態の確認」の 3 つだけ。VRChat には触りません

> ⚠️ SteamVR を止めると、VRChat など SteamVR を使っているアプリは自分で終了することがあります。

## 入れかた

### 1. PC(Windows)

1. [Releases](https://github.com/cympfh/frame-steamvr-remote/releases) から `frame-steamvr-remote.exe` をダウンロードします。
2. 好きなフォルダに置いてダブルクリック。タスクトレイ(右下の ^ の中)にアイコンが出ます。
3. 初回の Windows ファイアウォールの確認が出たら **「プライベート ネットワーク」を許可** してください。

次回からは Windows にログインすると自動で起動します(トレイのメニューでオフにできます)。

### 2. Frame

デスクトップモードの Konsole で、次を実行します(sudo は不要です)。

```sh
curl -fsSL https://raw.githubusercontent.com/cympfh/frame-steamvr-remote/main/install.sh | bash
```

「SteamVR リモート」がメニューと、SteamVR のクイックランチ(次に SteamVR を起動したとき)に出ます。

## 使いかた

### 最初の 1 回(ペアリング)

1. PC のトレイのアイコンを右クリックして **「Frame とペアリングする(コードを表示)」** を選ぶと、6 桁のコードが出ます(2 分間有効)。
2. Frame で「SteamVR リモート」を開き、そのコードを入力します。「ペアリングしました」と出れば完了です。

### ふだん

「SteamVR リモート」を開くと PC の名前と SteamVR の状態が出ます。

- **SteamVR終了**: PC の SteamVR をていねいに閉じ、閉じなければ SteamVR 関連のプロセスだけを終了します
- **再起動**: 終了してから SteamVR を起動し直します

PC でトレイのメニューからも同じ操作ができます。

### コマンド(上級者向け)

```sh
frame-steamvr-remote status     # PC と SteamVR の状態
frame-steamvr-remote quit       # SteamVR を終了
frame-steamvr-remote restart    # SteamVR を再起動
frame-steamvr-remote pair 123456
frame-steamvr-remote discover   # LAN の PC を探す
frame-steamvr-remote forget     # Frame 側のペアリングを忘れる
```

## 安全について

- 同じ LAN の中だけで動きます。インターネットには出ません(ルーターのポート開放はしないでください)。
- ペアリングで作った合言葉は通信路に流れません。PC では Windows の DPAPI で暗号化して保存、Frame では自分だけが読めるファイル(権限 600)に保存します。
- 毎回の操作は使い捨ての確認値つきなので、通信を盗み見て再送しても通りません。
- 終了の対象は、Steam の SteamVR フォルダにある SteamVR のプログラムだけです。VRChat は対象外です。

## ログと設定

| | 場所 |
|---|---|
| PC | `%APPDATA%\frame-steamvr-remote\app.log`(トレイのメニュー「ログのフォルダを開く」) |
| Frame | `~/.local/state/frame-steamvr-remote/app.log` |
| Frame の設定 | `~/.config/frame-steamvr-remote/config.json`(合言葉入り。人に見せないでください) |

ログは大きくなると自動で入れ替わります。合言葉とペアリングコードはログに書きません。

## 外しかた

- **PC**: トレイのメニューで「Windows の起動時に自動で起動する」をオフにして「終了」し、exe を消します。
- **Frame**: 
  ```sh
  curl -fsSL https://raw.githubusercontent.com/cympfh/frame-steamvr-remote/main/install.sh | bash -s -- --uninstall
  ```
  ファイルは消さずに `~/.local/share/frame-steamvr-remote-uninstalled/` へ移します。

## うまくいかないとき

- **PC が見つからない**: PC のトレイにアイコンがあるか、Frame と PC が同じネットワークか確認してください。Windows のネットワークの種類が「パブリック」だとファイアウォールで止まります。「プライベート」にしてください。
- **ペアリングできない**: コードは 2 分で切れ、5 回間違えると使えなくなります。もう一度トレイから「Frame とペアリングする」を選んでください。
- **ペアリングをやり直したい**: Frame の画面の「ペアリングし直す」、または PC のトレイの「ペアリングを解除する」。

## 自分でビルドする

PC 側は Rust です(Windows で `cargo build --release`、WSL からは `--target x86_64-pc-windows-gnu`)。

```sh
cargo test                                        # PC 側の単体テスト
cargo build && python3 -m unittest discover -s tests   # Frame 側のテストと、Python↔Rust の結合テスト(偽の PC を使う)
```

通信の取り決めは [docs/PROTOCOL.md](docs/PROTOCOL.md) にあります。

## ライセンス

MIT
