# 通信の取り決め (v1)

すべて同じ LAN の中だけで使う前提です。メッセージは 1 行の JSON(UTF-8、1 行 4096 バイトまで)です。

## 発見 (UDP 47650)

Frame がブロードキャスト(と、前回覚えた PC の IP への直接送信)で送ります。

```json
{"app":"fsr","v":1,"t":"discover"}
```

PC は送信元に返事します。

```json
{"app":"fsr","v":1,"t":"here","id":"<PC の ID>","name":"<PC 名>","port":47651,
 "steamvr":"running","pairing":false,"paired":true}
```

`id` は PC ごとの乱数(秘密ではない)。`pairing` はペアリング受付中かどうか。

## 操作 (TCP 47651)

接続すると PC が最初に `{"t":"challenge","nonce":"<16 バイトの乱数 hex>"}` を送ります。
Frame は 1 行だけ送ります。

### 操作リクエスト

```json
{"t":"req","client":"<Frame の ID>","cmd":"status|quit|restart","mac":"<hex>"}
```

`mac = HMAC-SHA256(secret, "req|" + nonce + "|" + client + "|" + cmd)` (hex)。
nonce は接続ごとの使い捨てなので、盗み見て再送しても通りません。

返事:

- `status`: `{"t":"status","name":..,"id":..,"steamvr":"running|stopped","vrchat":true|false}`
- `quit` / `restart`: `{"t":"result","cmd":..,"ok":true|false,"steamvr":"running|stopped","detail":".."}`
- 失敗: `{"t":"error","error":"auth|unknown_cmd|bad_request|..."}`

`quit` / `restart` は終わるまで返事を待たせます(数秒〜十数秒)。同時には 1 つだけ実行します。

### ペアリング

PC のトレイで「Frame とペアリングする」を選ぶと 6 桁のコードが画面に出ます(120 秒有効、間違いは 5 回まで)。
コードは通信にも、ログにも出ません。

1. Frame → PC: `{"t":"pair","client":"<Frame の ID>","name":"Steam Frame","pub":"<A: X25519 公開鍵 hex>"}`
2. PC → Frame: `{"t":"pair1","pub":"<B: X25519 公開鍵 hex>"}`
3. 両側で `K = X25519(自分の秘密鍵, 相手の公開鍵)` を計算。
4. Frame → PC: `{"t":"pair2","proof":"HMAC(K, "pair-frame|" + code + "|" + A + "|" + B)"}`
5. PC がコードを照合し、合っていれば `{"t":"paired","proof":"HMAC(K, "pair-pc|" + code + "|" + A + "|" + B)","pc_id":..,"pc_name":..}`。
   間違いなら `{"t":"error","error":"bad_code","left":<残り回数>}`。
6. 共有の合言葉 `secret = HMAC(K, "secret|" + code + "|" + A + "|" + B)` を両側で計算して保存します(通信には出ません)。

PC は小さい位数の公開鍵(K が全部 0 になるもの)を拒否します。

## 保存

- PC: `%APPDATA%\frame-steamvr-remote\paired.dat`(DPAPI で暗号化)
- Frame: `~/.config/frame-steamvr-remote/config.json`(権限 600)
