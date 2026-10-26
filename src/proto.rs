//! 通信の取り決め(プロトコル)と暗号まわり。OS に依存しないのでテストできる。
//!
//! - 発見: UDP 47650 に `{"app":"fsr","t":"discover"}` をブロードキャスト → PC が名前と状態を返す
//! - 操作: TCP 47651。接続するとまず PC が `challenge`(使い捨ての乱数)を送る。
//!   Frame は `HMAC(共有の合言葉, 乱数|クライアントID|コマンド)` を付けて 1 行のリクエストを送る。
//! - ペアリング: X25519 で鍵交換し、PC に表示した数字コードを HMAC で確認する。
//!   合言葉(共有の秘密)は通信に流れず、両側が計算で作る。

use hmac::{Hmac, Mac};
use sha2::Sha256;
use x25519_dalek::{PublicKey, StaticSecret};

pub const APP_ID: &str = "fsr";
pub const PROTO_VERSION: u32 = 1;
pub const DISCOVERY_PORT: u16 = 47650;
pub const CONTROL_PORT: u16 = 47651;
pub const MAX_LINE: usize = 4096;
pub const PAIR_CODE_DIGITS: usize = 6;
pub const PAIR_VALID_SECS: u64 = 120;
pub const PAIR_MAX_ATTEMPTS: u8 = 5;

pub fn to_hex(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for x in b {
        s.push_str(&format!("{:02x}", x));
    }
    s
}

pub fn from_hex(s: &str) -> Option<Vec<u8>> {
    if s.len() % 2 != 0 || !s.is_ascii() {
        return None;
    }
    (0..s.len() / 2)
        .map(|i| u8::from_str_radix(&s[i * 2..i * 2 + 2], 16).ok())
        .collect()
}

pub fn random_bytes<const N: usize>() -> [u8; N] {
    let mut b = [0u8; N];
    getrandom::getrandom(&mut b).expect("OS の乱数が使えない");
    b
}

pub fn random_hex(n: usize) -> String {
    let mut v = vec![0u8; n];
    getrandom::getrandom(&mut v).expect("OS の乱数が使えない");
    to_hex(&v)
}

/// 数字コード(先頭が 0 でも 6 桁)。偏りが出ないよう、棄却法で作る。
pub fn random_code() -> String {
    loop {
        let b: [u8; 4] = random_bytes();
        let n = u32::from_le_bytes(b);
        if n < 4_000_000_000 {
            return format!("{:0width$}", n % 1_000_000, width = PAIR_CODE_DIGITS);
        }
    }
}

pub fn hmac_bytes(key: &[u8], msg: &str) -> Vec<u8> {
    let mut m = <Hmac<Sha256> as Mac>::new_from_slice(key).expect("HMAC は任意長の鍵を取れる");
    m.update(msg.as_bytes());
    m.finalize().into_bytes().to_vec()
}

pub fn hmac_hex(key: &[u8], msg: &str) -> String {
    to_hex(&hmac_bytes(key, msg))
}

/// 一定時間で比較する(タイミング攻撃対策)。
pub fn verify_hex(key: &[u8], msg: &str, mac_hex: &str) -> bool {
    let Some(given) = from_hex(mac_hex) else { return false };
    let mut m = <Hmac<Sha256> as Mac>::new_from_slice(key).expect("HMAC は任意長の鍵を取れる");
    m.update(msg.as_bytes());
    m.verify_slice(&given).is_ok()
}

/// 操作リクエストの署名の対象。
pub fn request_message(nonce_hex: &str, client: &str, cmd: &str) -> String {
    format!("req|{}|{}|{}", nonce_hex, client, cmd)
}

#[cfg(test)]
pub fn request_mac(secret: &[u8], nonce_hex: &str, client: &str, cmd: &str) -> String {
    hmac_hex(secret, &request_message(nonce_hex, client, cmd))
}

pub fn verify_request(secret: &[u8], nonce_hex: &str, client: &str, cmd: &str, mac_hex: &str) -> bool {
    verify_hex(secret, &request_message(nonce_hex, client, cmd), mac_hex)
}

pub struct Ephemeral {
    secret: StaticSecret,
    pub public_hex: String,
}

impl Ephemeral {
    pub fn new() -> Self {
        let secret = StaticSecret::from(random_bytes::<32>());
        let public_hex = to_hex(PublicKey::from(&secret).as_bytes());
        Ephemeral { secret, public_hex }
    }

    /// 相手の公開鍵から共有の鍵 K を作る。弱い点(全部 0 になる鍵)は拒否する。
    pub fn agree(&self, peer_pub_hex: &str) -> Option<[u8; 32]> {
        let v = from_hex(peer_pub_hex)?;
        let arr: [u8; 32] = v.try_into().ok()?;
        let shared = self.secret.diffie_hellman(&PublicKey::from(arr));
        if !shared.was_contributory() {
            return None;
        }
        Some(*shared.as_bytes())
    }
}

/// ペアリングの確認用 HMAC。role は "frame" か "pc"。
pub fn pair_proof(k: &[u8], role: &str, code: &str, a_hex: &str, b_hex: &str) -> String {
    hmac_hex(k, &format!("pair-{}|{}|{}|{}", role, code, a_hex, b_hex))
}

/// ペアリング後に両側で持つ合言葉(通信には流れない)。
pub fn derive_secret(k: &[u8], code: &str, a_hex: &str, b_hex: &str) -> Vec<u8> {
    hmac_bytes(k, &format!("secret|{}|{}|{}", code, a_hex, b_hex))
}

pub fn valid_client_id(s: &str) -> bool {
    !s.is_empty() && s.len() <= 64 && s.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

pub fn clean_name(s: &str) -> String {
    s.chars().filter(|c| !c.is_control()).take(64).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_roundtrip() {
        let b = [0u8, 1, 0xab, 0xff];
        assert_eq!(to_hex(&b), "0001abff");
        assert_eq!(from_hex("0001abff").unwrap(), b);
        assert!(from_hex("0").is_none());
        assert!(from_hex("zz").is_none());
        assert!(from_hex("é").is_none());
    }

    #[test]
    fn hmac_known_vector() {
        // RFC 4231 test case 2
        let h = hmac_hex(b"Jefe", "what do ya want for nothing?");
        assert_eq!(h, "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
    }

    #[test]
    fn x25519_rfc7748_vector() {
        // RFC 7748 6.1 の Alice/Bob
        let a = StaticSecret::from(
            <[u8; 32]>::try_from(
                from_hex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a").unwrap(),
            )
            .unwrap(),
        );
        let b_pub = from_hex("de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f").unwrap();
        let s = a.diffie_hellman(&PublicKey::from(<[u8; 32]>::try_from(b_pub).unwrap()));
        assert_eq!(
            to_hex(s.as_bytes()),
            "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
        );
    }

    #[test]
    fn pairing_both_sides_agree() {
        let f = Ephemeral::new();
        let p = Ephemeral::new();
        let kf = f.agree(&p.public_hex).unwrap();
        let kp = p.agree(&f.public_hex).unwrap();
        assert_eq!(kf, kp);
        let sf = derive_secret(&kf, "123456", &f.public_hex, &p.public_hex);
        let sp = derive_secret(&kp, "123456", &f.public_hex, &p.public_hex);
        assert_eq!(sf, sp);
        // コードが違えば合言葉も確認用 HMAC も違う
        assert_ne!(sf, derive_secret(&kf, "654321", &f.public_hex, &p.public_hex));
        assert_ne!(
            pair_proof(&kf, "frame", "123456", &f.public_hex, &p.public_hex),
            pair_proof(&kf, "frame", "000000", &f.public_hex, &p.public_hex)
        );
        assert_ne!(
            pair_proof(&kf, "frame", "123456", &f.public_hex, &p.public_hex),
            pair_proof(&kf, "pc", "123456", &f.public_hex, &p.public_hex)
        );
    }

    #[test]
    fn weak_public_key_rejected() {
        let e = Ephemeral::new();
        assert!(e.agree(&"00".repeat(32)).is_none());
        assert!(e.agree("abcd").is_none());
        assert!(e.agree("not hex").is_none());
    }

    #[test]
    fn request_auth() {
        let secret = [7u8; 32];
        let nonce = random_hex(16);
        let mac = request_mac(&secret, &nonce, "frame1", "status");
        assert!(verify_request(&secret, &nonce, "frame1", "status", &mac));
        assert!(!verify_request(&secret, &nonce, "frame1", "quit", &mac));
        assert!(!verify_request(&secret, &nonce, "frame2", "status", &mac));
        assert!(!verify_request(&secret, &random_hex(16), "frame1", "status", &mac));
        assert!(!verify_request(&[8u8; 32], &nonce, "frame1", "status", &mac));
        assert!(!verify_request(&secret, &nonce, "frame1", "status", "zz"));
    }

    #[test]
    fn codes_are_six_digits() {
        for _ in 0..200 {
            let c = random_code();
            assert_eq!(c.len(), 6);
            assert!(c.chars().all(|x| x.is_ascii_digit()));
        }
    }

    #[test]
    fn client_id_rules() {
        assert!(valid_client_id("abc-123_X"));
        assert!(!valid_client_id(""));
        assert!(!valid_client_id("a b"));
        assert!(!valid_client_id(&"a".repeat(65)));
        assert_eq!(clean_name("Steam\nFrame"), "SteamFrame");
    }
}
