//! 動画の配信(frame-video-player 用)。
//!
//! - PC の「ビデオ」フォルダの動画を、LAN の HTTP(ポート 47652)で Frame に流す。Range 要求に対応(シーク用)。
//! - URL には使い捨ての合言葉(トークン)が入る。トークンは、ペアリング済みの Frame が TCP 47651 の
//!   `video` 要求(HMAC で認証済み)でだけ受け取れる。12 時間使われないと無効になる
//!   (使うたびに延びるので、再生中に切れることはない)。
//! - トレイから「YouTube の URL を Frame に送る」と、URL を預かり、次の `video` 要求で Frame に渡す。
use crate::log;
use crate::proto::random_hex;
use serde_json::json;
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const VIDEO_PORT: u16 = 47652;
const TOKEN_LIFE: Duration = Duration::from_secs(12 * 3600);
const MAX_TOKENS: usize = 8;
pub const VIDEO_EXTS: &[&str] = &["mp4", "mkv", "webm", "mov", "avi", "m4v", "ts", "m2ts", "wmv", "flv", "mpg", "mpeg"];
const MAX_FILES: usize = 5000;

pub struct VideoState {
    root: PathBuf,
    tokens: Mutex<Vec<(String, Instant)>>,
    pending_url: Mutex<Option<String>>,
}

impl VideoState {
    pub fn new(root: PathBuf) -> Arc<VideoState> {
        Arc::new(VideoState { root, tokens: Mutex::new(Vec::new()), pending_url: Mutex::new(None) })
    }

    /// 新しいトークンを作って返す(古いものは期限切れか、多すぎる分から捨てる)。
    pub fn new_token(&self) -> String {
        let t = random_hex(24);
        let mut g = self.tokens.lock().unwrap_or_else(|e| e.into_inner());
        g.retain(|(_, at)| at.elapsed() < TOKEN_LIFE);
        g.push((t.clone(), Instant::now()));
        while g.len() > MAX_TOKENS {
            g.remove(0);
        }
        t
    }

    /// トークンが有効か。使われたトークンは時刻を新しくして末尾へ回すので、再生中のものは消えない。
    pub fn token_ok(&self, t: &str) -> bool {
        let mut g = self.tokens.lock().unwrap_or_else(|e| e.into_inner());
        let hit = g.iter().position(|(x, at)| at.elapsed() < TOKEN_LIFE && crate::proto::ct_eq(x.as_bytes(), t.as_bytes()));
        match hit {
            Some(i) => {
                let (x, _) = g.remove(i);
                g.push((x, Instant::now()));
                true
            }
            None => false,
        }
    }

    pub fn set_url(&self, url: &str) {
        *self.pending_url.lock().unwrap_or_else(|e| e.into_inner()) = Some(url.to_string());
    }

    pub fn take_url(&self) -> Option<String> {
        self.pending_url.lock().unwrap_or_else(|e| e.into_inner()).take()
    }

    /// ルート以下の動画ファイルを、相対パス(/ 区切り)と大きさで返す。
    pub fn list(&self) -> Vec<(String, u64)> {
        let mut out = Vec::new();
        walk(&self.root, &self.root, &mut out, 0);
        out.sort_by(|a, b| a.0.to_lowercase().cmp(&b.0.to_lowercase()));
        out
    }

    /// 相対パスを、ルートの中の実在する動画ファイルに直す。外に出るもの・動画でないものは None。
    pub fn resolve(&self, rel: &str) -> Option<PathBuf> {
        let p = Path::new(rel);
        if rel.is_empty() || p.components().any(|c| !matches!(c, Component::Normal(_))) {
            return None;
        }
        if !is_video(p) {
            return None;
        }
        let full = self.root.join(p);
        let canon = full.canonicalize().ok()?;
        let root = self.root.canonicalize().ok()?;
        if !canon.starts_with(&root) || !canon.is_file() {
            return None;
        }
        Some(canon)
    }

    pub fn run_http(self: &Arc<Self>, port: u16) -> std::io::Result<()> {
        let l = TcpListener::bind(("0.0.0.0", port))?;
        log!("動画の配信を待ち受け開始: TCP {} フォルダ={}", port, self.root.display());
        for s in l.incoming() {
            let Ok(s) = s else { continue };
            if ACTIVE.fetch_add(1, Ordering::SeqCst) >= MAX_CONNS {
                ACTIVE.fetch_sub(1, Ordering::SeqCst);
                log!("動画の配信: 接続が多すぎるので断る ({:?})", s.peer_addr().ok());
                continue; // s を落として閉じる
            }
            let me = self.clone();
            std::thread::spawn(move || {
                let peer = s.peer_addr().ok();
                if let Err(e) = me.serve(s) {
                    log!("動画の配信でエラー ({:?}): {}", peer, e);
                }
                ACTIVE.fetch_sub(1, Ordering::SeqCst);
            });
        }
        Ok(())
    }

    fn serve(&self, s: TcpStream) -> Result<(), String> {
        s.set_read_timeout(Some(Duration::from_secs(30))).ok();
        let peer = s.peer_addr().ok();
        let mut r = BufReader::new(s.try_clone().map_err(|e| e.to_string())?);
        let mut w = s;
        loop {
            let mut line = String::new();
            if read_line_limited(&mut r, &mut line)? == 0 {
                return Ok(());
            }
            let parts: Vec<&str> = line.trim_end().split(' ').collect();
            if parts.len() < 2 {
                return Ok(());
            }
            let (method, target) = (parts[0], parts[1]);
            let mut range: Option<String> = None;
            let mut close = false;
            let mut nh = 0;
            loop {
                nh += 1;
                if nh > MAX_HEADERS {
                    return Err("ヘッダーが多すぎる".into());
                }
                let mut h = String::new();
                if read_line_limited(&mut r, &mut h)? == 0 {
                    return Ok(());
                }
                let h = h.trim_end();
                if h.is_empty() {
                    break;
                }
                if let Some((k, v)) = h.split_once(':') {
                    let k = k.trim().to_ascii_lowercase();
                    if k == "range" {
                        range = Some(v.trim().to_string());
                    } else if k == "connection" && v.trim().eq_ignore_ascii_case("close") {
                        close = true;
                    }
                }
            }
            let head = method == "HEAD";
            if method != "GET" && !head {
                respond(&mut w, "405 Method Not Allowed", "text/plain", b"method", head)?;
                return Ok(());
            }
            // /list/<token> または /v/<token>/<相対パス(URL エンコード)>
            let path = target.split('?').next().unwrap_or("");
            let segs: Vec<&str> = path.trim_start_matches('/').splitn(3, '/').collect();
            let token = segs.get(1).copied().unwrap_or("");
            if !self.token_ok(token) {
                log!("動画の配信: トークンが違う ({:?}) {}", peer, segs.first().unwrap_or(&""));
                std::thread::sleep(Duration::from_millis(300));
                respond(&mut w, "403 Forbidden", "text/plain", b"forbidden", head)?;
                return Ok(());
            }
            match segs.first().copied() {
                Some("list") => {
                    let files: Vec<_> = self.list().into_iter().map(|(p, n)| json!({"path": p, "size": n})).collect();
                    log!("動画の一覧を送信 ({:?}): {} 件", peer, files.len());
                    let body = json!({"files": files}).to_string();
                    respond(&mut w, "200 OK", "application/json; charset=utf-8", body.as_bytes(), head)?;
                }
                Some("v") => {
                    let rel = percent_decode(segs.get(2).copied().unwrap_or(""));
                    match rel.as_deref().and_then(|r| self.resolve(r)) {
                        Some(f) => self.send_file(&mut w, &f, range.as_deref(), head, peer)?,
                        None => {
                            log!("動画の配信: 見つからない ({:?}) {:?}", peer, rel);
                            respond(&mut w, "404 Not Found", "text/plain", b"not found", head)?;
                        }
                    }
                }
                _ => respond(&mut w, "404 Not Found", "text/plain", b"not found", head)?,
            }
            if close {
                return Ok(());
            }
        }
    }

    fn send_file(&self, w: &mut TcpStream, f: &Path, range: Option<&str>, head: bool, peer: Option<std::net::SocketAddr>) -> Result<(), String> {
        let mut file = std::fs::File::open(f).map_err(|e| e.to_string())?;
        let size = file.metadata().map_err(|e| e.to_string())?.len();
        // bytes= 以外の単位(items= など)は無視して全体を返す
        let range = range.filter(|r| r.trim_start().to_ascii_lowercase().starts_with("bytes="));
        let (start, end, partial) = match range.and_then(|r| parse_range(r, size)) {
            Some((a, b)) => (a, b, true),
            None if range.is_some() => {
                let h = format!("HTTP/1.1 416 Range Not Satisfiable\r\nContent-Range: bytes */{}\r\nContent-Length: 0\r\n\r\n", size);
                return w.write_all(h.as_bytes()).map_err(|e| e.to_string());
            }
            None => (0, size.saturating_sub(1), false),
        };
        let len = if size == 0 { 0 } else { end - start + 1 };
        log!("動画を送信 ({:?}): {} bytes={}-{}/{}", peer, f.display(), start, end, size);
        let mut h = String::new();
        h.push_str(if partial { "HTTP/1.1 206 Partial Content\r\n" } else { "HTTP/1.1 200 OK\r\n" });
        h.push_str(&format!("Content-Type: {}\r\nAccept-Ranges: bytes\r\nContent-Length: {}\r\n", mime(f), len));
        if partial {
            h.push_str(&format!("Content-Range: bytes {}-{}/{}\r\n", start, end, size));
        }
        h.push_str("\r\n");
        w.write_all(h.as_bytes()).map_err(|e| e.to_string())?;
        if head || len == 0 {
            return Ok(());
        }
        file.seek(SeekFrom::Start(start)).map_err(|e| e.to_string())?;
        let mut left = len;
        let mut buf = vec![0u8; 256 * 1024];
        while left > 0 {
            let cap = left.min(buf.len() as u64) as usize;
            let n = file.read(&mut buf[..cap]).map_err(|e| e.to_string())?;
            if n == 0 {
                break;
            }
            if w.write_all(&buf[..n]).is_err() {
                return Ok(()); // 相手が切った(シークなど)。普通のこと
            }
            left -= n as u64;
        }
        Ok(())
    }
}

static ACTIVE: AtomicUsize = AtomicUsize::new(0);
const MAX_CONNS: usize = 16;
const MAX_LINE: usize = 8192;
const MAX_HEADERS: usize = 64;

/// 1 行を MAX_LINE バイトまで読む。改行が来ないまま上限に達したらエラー。
fn read_line_limited<R: BufRead>(r: &mut R, out: &mut String) -> Result<usize, String> {
    let n = r.by_ref().take(MAX_LINE as u64).read_line(out).map_err(|e| e.to_string())?;
    if n == MAX_LINE && !out.ends_with('\n') {
        return Err("行が長すぎる".into());
    }
    Ok(n)
}

fn walk(root: &Path, dir: &Path, out: &mut Vec<(String, u64)>, depth: usize) {
    if depth > 8 || out.len() >= MAX_FILES {
        return;
    }
    let Ok(rd) = std::fs::read_dir(dir) else { return };
    for e in rd.flatten() {
        let Ok(ft) = e.file_type() else { continue };
        let p = e.path();
        if ft.is_dir() {
            walk(root, &p, out, depth + 1);
        } else if ft.is_file() && is_video(&p) {
            if let Ok(rel) = p.strip_prefix(root) {
                let rel = rel.components().map(|c| c.as_os_str().to_string_lossy().into_owned()).collect::<Vec<_>>().join("/");
                out.push((rel, e.metadata().map(|m| m.len()).unwrap_or(0)));
            }
        }
        if out.len() >= MAX_FILES {
            return;
        }
    }
}

pub fn is_video(p: &Path) -> bool {
    p.extension().and_then(|e| e.to_str()).map(|e| VIDEO_EXTS.contains(&e.to_ascii_lowercase().as_str())).unwrap_or(false)
}

fn mime(p: &Path) -> &'static str {
    match p.extension().and_then(|e| e.to_str()).map(|e| e.to_ascii_lowercase()).as_deref() {
        Some("mp4") | Some("m4v") => "video/mp4",
        Some("webm") => "video/webm",
        Some("mkv") => "video/x-matroska",
        _ => "application/octet-stream",
    }
}

fn respond(w: &mut TcpStream, status: &str, ctype: &str, body: &[u8], head: bool) -> Result<(), String> {
    let h = format!("HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\n\r\n", status, ctype, body.len());
    w.write_all(h.as_bytes()).map_err(|e| e.to_string())?;
    if !head {
        w.write_all(body).map_err(|e| e.to_string())?;
    }
    Ok(())
}

/// "bytes=a-b" / "bytes=a-" / "bytes=-n" を (開始, 終わり[含む]) に。範囲外は None。
pub fn parse_range(r: &str, size: u64) -> Option<(u64, u64)> {
    let spec = r.strip_prefix("bytes=")?.split(',').next()?.trim();
    let (a, b) = spec.split_once('-')?;
    if size == 0 {
        return None;
    }
    if a.is_empty() {
        let n: u64 = b.parse().ok()?;
        if n == 0 {
            return None;
        }
        return Some((size.saturating_sub(n), size - 1));
    }
    let start: u64 = a.parse().ok()?;
    if start >= size {
        return None;
    }
    let end = if b.is_empty() { size - 1 } else { b.parse::<u64>().ok()?.min(size - 1) };
    if end < start {
        return None;
    }
    Some((start, end))
}

pub fn percent_decode(s: &str) -> Option<String> {
    let b = s.as_bytes();
    let mut out = Vec::with_capacity(b.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'%' {
            let h = std::str::from_utf8(b.get(i + 1..i + 3)?).ok()?;
            out.push(u8::from_str_radix(h, 16).ok()?);
            i += 3;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8(out).ok()
}

/// http(s) の URL らしいか(トレイからクリップボードを送るときの確認)。
pub fn looks_like_url(s: &str) -> bool {
    let s = s.trim();
    (s.starts_with("https://") || s.starts_with("http://")) && s.len() < 2048 && !s.chars().any(|c| c.is_whitespace() || c.is_control())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ranges() {
        assert_eq!(parse_range("bytes=0-99", 1000), Some((0, 99)));
        assert_eq!(parse_range("bytes=900-", 1000), Some((900, 999)));
        assert_eq!(parse_range("bytes=-100", 1000), Some((900, 999)));
        assert_eq!(parse_range("bytes=990-2000", 1000), Some((990, 999)));
        assert_eq!(parse_range("bytes=1000-", 1000), None);
        assert_eq!(parse_range("bytes=5-1", 1000), None);
    }

    #[test]
    fn decode_and_resolve() {
        assert_eq!(percent_decode("a%20b%E3%81%82.mp4").as_deref(), Some("a bあ.mp4"));
        let dir = std::env::temp_dir().join(format!("fsr-video-test-{}", random_hex(4)));
        std::fs::create_dir_all(dir.join("sub")).unwrap();
        std::fs::write(dir.join("sub/x.mp4"), b"0123456789").unwrap();
        std::fs::write(dir.join("y.txt"), b"no").unwrap();
        let v = VideoState::new(dir.clone());
        assert!(v.resolve("sub/x.mp4").is_some());
        assert!(v.resolve("y.txt").is_none());
        assert!(v.resolve("../x.mp4").is_none());
        assert!(v.resolve("/etc/x.mp4").is_none());
        assert_eq!(v.list(), vec![("sub/x.mp4".to_string(), 10)]);
        let t = v.new_token();
        assert!(v.token_ok(&t));
        assert!(!v.token_ok("nope"));
        v.set_url("https://youtu.be/x");
        assert_eq!(v.take_url().as_deref(), Some("https://youtu.be/x"));
        assert_eq!(v.take_url(), None);
        assert!(looks_like_url("https://www.youtube.com/watch?v=abc"));
        assert!(!looks_like_url("hello world"));
    }

    #[test]
    fn http_range_roundtrip() {
        let dir = std::env::temp_dir().join(format!("fsr-video-http-{}", random_hex(4)));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("a b.mp4"), b"0123456789").unwrap();
        let v = VideoState::new(dir);
        let t = v.new_token();
        let l = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = l.local_addr().unwrap().port();
        drop(l);
        let v2 = v.clone();
        std::thread::spawn(move || v2.run_http(port));
        std::thread::sleep(Duration::from_millis(200));
        let get = |p: &str, extra: &str| {
            let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
            write!(s, "GET {} HTTP/1.1\r\nHost: x\r\n{}Connection: close\r\n\r\n", p, extra).unwrap();
            let mut out = String::new();
            s.read_to_string(&mut out).unwrap();
            out
        };
        let r = get(&format!("/v/{}/a%20b.mp4", t), "Range: bytes=2-4\r\n");
        assert!(r.starts_with("HTTP/1.1 206"), "{}", r);
        assert!(r.ends_with("234"));
        assert!(r.contains("Content-Range: bytes 2-4/10"));
        assert!(get("/v/bad/a%20b.mp4", "").starts_with("HTTP/1.1 403"));
        let list = get(&format!("/list/{}", t), "");
        assert!(list.contains("a b.mp4"));
        assert!(!list.contains("root"), "{}", list);
        // bytes 以外の単位は無視して全体
        let r = get(&format!("/v/{}/a%20b.mp4", t), "Range: items=0-1\r\n");
        assert!(r.starts_with("HTTP/1.1 200"), "{}", r);
        assert!(r.ends_with("0123456789"));
        // 改行のない長い行は読み続けずに切る
        let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let junk = vec![b'A'; MAX_LINE * 4];
        let _ = s.write_all(&junk);
        let mut out = Vec::new();
        let _ = s.read_to_end(&mut out);
        assert!(out.is_empty());
        // ヘッダーが多すぎても切る
        let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let mut req = format!("GET /list/{} HTTP/1.1\r\n", t);
        for i in 0..(MAX_HEADERS + 5) {
            req.push_str(&format!("X-{}: y\r\n", i));
        }
        req.push_str("\r\n");
        let _ = s.write_all(req.as_bytes());
        let mut out = Vec::new();
        let _ = s.read_to_end(&mut out);
        assert!(out.is_empty());
    }

    #[test]
    fn used_token_survives_newer_ones() {
        let v = VideoState::new(std::env::temp_dir());
        let t = v.new_token();
        for _ in 0..MAX_TOKENS - 1 {
            v.new_token();
        }
        assert!(v.token_ok(&t)); // 使われたので末尾へ
        for _ in 0..MAX_TOKENS - 1 {
            v.new_token();
        }
        assert!(v.token_ok(&t));
        for _ in 0..MAX_TOKENS {
            v.new_token();
        }
        assert!(!v.token_ok(&t));
    }
}
