//! ファイルへのログ。大事なことは全部書く(合言葉・ペアリングコードだけは書かない)。
//! 1 MiB を超えたら app.log.1 … app.log.5 に回す。
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::PathBuf;
use std::sync::Mutex;

const MAX_BYTES: u64 = 1024 * 1024;
const KEEP: u32 = 5;

struct Inner {
    path: PathBuf,
    file: Option<File>,
    size: u64,
}

static LOG: Mutex<Option<Inner>> = Mutex::new(None);

pub fn init() {
    let path = crate::paths::data_dir().join("app.log");
    let size = fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
    let file = OpenOptions::new().create(true).append(true).open(&path).ok();
    *LOG.lock().unwrap_or_else(|e| e.into_inner()) = Some(Inner { path, file, size });
}

/// 日本時間ではなく、PC のローカル時刻が欲しいが標準ライブラリだけでは出せないので UTC の秒で書く。
fn stamp() -> String {
    let d = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let secs = d.as_secs();
    let (days, rem) = (secs / 86400, secs % 86400);
    let (y, m, dd) = civil_from_days(days as i64);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        y,
        m,
        dd,
        rem / 3600,
        rem % 3600 / 60,
        rem % 60,
        d.subsec_millis()
    )
}

/// 1970-01-01 からの日数 → 年月日(Howard Hinnant の方式)
pub fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719468;
    let era = z.div_euclid(146097);
    let doe = z.rem_euclid(146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

pub fn write(msg: &str) {
    let line = format!("{} {}\n", stamp(), msg);
    eprint!("{}", line);
    let mut g = LOG.lock().unwrap_or_else(|e| e.into_inner());
    let Some(inner) = g.as_mut() else { return };
    if inner.size + line.len() as u64 > MAX_BYTES {
        inner.file = None;
        for i in (1..KEEP).rev() {
            let from = numbered(&inner.path, i);
            if from.exists() {
                let _ = fs::rename(&from, numbered(&inner.path, i + 1));
            }
        }
        let _ = fs::rename(&inner.path, numbered(&inner.path, 1));
        inner.file = OpenOptions::new().create(true).append(true).open(&inner.path).ok();
        inner.size = 0;
    }
    if let Some(f) = inner.file.as_mut() {
        if f.write_all(line.as_bytes()).is_ok() {
            inner.size += line.len() as u64;
        }
    }
}

fn numbered(p: &PathBuf, n: u32) -> PathBuf {
    let mut s = p.clone().into_os_string();
    s.push(format!(".{}", n));
    PathBuf::from(s)
}

#[macro_export]
macro_rules! log {
    ($($arg:tt)*) => { $crate::applog::write(&format!($($arg)*)) };
}

#[cfg(test)]
mod tests {
    #[test]
    fn civil_dates() {
        assert_eq!(super::civil_from_days(0), (1970, 1, 1));
        assert_eq!(super::civil_from_days(20_752), (2026, 10, 26));
        assert_eq!(super::civil_from_days(11_016), (2000, 2, 29));
    }
}
