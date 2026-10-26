//! Windows 専用: プロセス操作・DPAPI・自動起動・トレイ。
#![cfg(windows)]
use crate::log;
use crate::server::{Host, OpResult, Pairing, Server};
use crate::settings;
use crate::steamvr;
use std::ffi::OsStr;
use std::os::windows::ffi::OsStrExt;
use std::ptr::{null, null_mut};
use std::sync::atomic::{AtomicIsize, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};
use windows_sys::Win32::Foundation::*;
use windows_sys::Win32::Graphics::Gdi::HBRUSH;
use windows_sys::Win32::Security::Cryptography::*;
use windows_sys::Win32::System::Diagnostics::ToolHelp::*;
use windows_sys::Win32::System::LibraryLoader::GetModuleHandleW;
use windows_sys::Win32::System::Registry::*;
use windows_sys::Win32::System::Threading::*;
use windows_sys::Win32::UI::Shell::*;
use windows_sys::Win32::UI::WindowsAndMessaging::*;

pub fn w(s: &str) -> Vec<u16> {
    OsStr::new(s).encode_wide().chain(std::iter::once(0)).collect()
}

fn fill<const N: usize>(dst: &mut [u16; N], s: &str) {
    let v: Vec<u16> = OsStr::new(s).encode_wide().take(N - 1).collect();
    dst[..v.len()].copy_from_slice(&v);
    dst[v.len()] = 0;
}

// ---------------------------------------------------------------- プロセス

pub struct Proc {
    pub pid: u32,
    pub name: String,
    pub path: String,
}

fn exe_path(pid: u32) -> Option<String> {
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h.is_null() {
            return None;
        }
        let mut buf = [0u16; 1024];
        let mut len = buf.len() as u32;
        let ok = QueryFullProcessImageNameW(h, 0, buf.as_mut_ptr(), &mut len);
        CloseHandle(h);
        if ok == 0 {
            return None;
        }
        Some(String::from_utf16_lossy(&buf[..len as usize]))
    }
}

pub fn all_procs() -> Vec<Proc> {
    let mut out = Vec::new();
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snap == INVALID_HANDLE_VALUE {
            return out;
        }
        let mut pe: PROCESSENTRY32W = std::mem::zeroed();
        pe.dwSize = std::mem::size_of::<PROCESSENTRY32W>() as u32;
        let mut ok = Process32FirstW(snap, &mut pe);
        while ok != 0 {
            let n = pe.szExeFile.iter().position(|&c| c == 0).unwrap_or(pe.szExeFile.len());
            out.push(Proc {
                pid: pe.th32ProcessID,
                name: String::from_utf16_lossy(&pe.szExeFile[..n]),
                path: String::new(),
            });
            ok = Process32NextW(snap, &mut pe);
        }
        CloseHandle(snap);
    }
    out
}

pub fn steamvr_procs() -> Vec<Proc> {
    let mark = steamvr::mark();
    all_procs()
        .into_iter()
        .filter(|p| steamvr::NAMES.contains(&p.name.to_lowercase().as_str()))
        .filter_map(|mut p| {
            p.path = exe_path(p.pid)?;
            steamvr::is_steamvr_process(&p.name, &p.path, mark).then_some(p)
        })
        .collect()
}

pub fn vrchat_running() -> bool {
    all_procs().iter().any(|p| steamvr::is_vrchat(&p.name))
}

unsafe extern "system" fn enum_cb(hwnd: HWND, lp: LPARAM) -> BOOL {
    let pids = &*(lp as *const Vec<u32>);
    let mut pid = 0u32;
    GetWindowThreadProcessId(hwnd, &mut pid);
    if pids.contains(&pid) {
        PostMessageW(hwnd, WM_CLOSE, 0, 0);
    }
    1
}

/// 指定したプロセスのウィンドウに「閉じて」と頼む(普通に × を押したのと同じ)。
fn post_close(pids: &[u32]) {
    let v: Vec<u32> = pids.to_vec();
    unsafe {
        EnumWindows(Some(enum_cb), &v as *const Vec<u32> as LPARAM);
    }
}

fn terminate(pid: u32) -> bool {
    unsafe {
        let h = OpenProcess(PROCESS_TERMINATE, 0, pid);
        if h.is_null() {
            return false;
        }
        let ok = TerminateProcess(h, 1);
        CloseHandle(h);
        ok != 0
    }
}

fn wait_until<F: Fn() -> bool>(timeout: Duration, f: F) -> bool {
    let t = Instant::now();
    while t.elapsed() < timeout {
        if f() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    f()
}

pub fn steamvr_running() -> bool {
    steamvr_procs().iter().any(|p| {
        let n = p.name.to_lowercase();
        n == "vrserver.exe" || n == "vrmonitor.exe"
    })
}

pub fn quit_steamvr() -> OpResult {
    let procs = steamvr_procs();
    if procs.is_empty() {
        log!("SteamVR は起動していない(終了の要求は何もしない)");
        return OpResult { ok: true, state: "stopped", detail: "already_stopped".into() };
    }
    let desc: Vec<String> = procs.iter().map(|p| format!("{}({})", p.name, p.pid)).collect();
    log!("SteamVR を終了する。対象: {}", desc.join(", "));
    let pids: Vec<u32> = procs.iter().map(|p| p.pid).collect();
    post_close(&pids);
    log!("まず SteamVR のウィンドウに「閉じて」と依頼した。最大 20 秒待つ");
    if wait_until(Duration::from_secs(20), || steamvr_procs().is_empty()) {
        log!("SteamVR は自分で終了した");
        return OpResult { ok: true, state: "stopped", detail: "graceful".into() };
    }
    let left = steamvr_procs();
    let d2: Vec<String> = left.iter().map(|p| format!("{}({})", p.name, p.pid)).collect();
    log!("まだ残っているので SteamVR のプロセスだけ強制終了する: {}", d2.join(", "));
    for p in &left {
        let ok = terminate(p.pid);
        log!("強制終了 {}({}): {}", p.name, p.pid, if ok { "成功" } else { "失敗" });
    }
    let gone = wait_until(Duration::from_secs(8), || steamvr_procs().is_empty());
    OpResult {
        ok: gone,
        state: if steamvr_running() { "running" } else { "stopped" },
        detail: if gone { format!("killed:{}", left.len()) } else { "still_running".into() },
    }
}

pub fn launch_steamvr() -> bool {
    let r = unsafe {
        ShellExecuteW(null_mut(), w("open").as_ptr(), w("steam://run/250820").as_ptr(), null(), null(), SW_SHOWNORMAL)
    };
    (r as isize) > 32
}

pub fn restart_steamvr() -> OpResult {
    let q = quit_steamvr();
    if !q.ok {
        return OpResult { ok: false, state: q.state, detail: format!("quit_failed:{}", q.detail) };
    }
    std::thread::sleep(Duration::from_secs(3));
    log!("steam://run/250820 で SteamVR を起動する");
    if !launch_steamvr() {
        log!("SteamVR の起動要求に失敗");
        return OpResult { ok: false, state: "stopped", detail: "launch_failed".into() };
    }
    let up = wait_until(Duration::from_secs(40), steamvr_running);
    log!("SteamVR の起動確認: {}", if up { "起動した" } else { "40 秒待ったがまだ(起動途中かもしれない)" });
    OpResult { ok: true, state: if up { "running" } else { "stopped" }, detail: if up { "restarted".into() } else { "starting".into() } }
}

// ---------------------------------------------------------------- DPAPI(合言葉の保管)

fn dpapi_protect(data: &[u8]) -> Option<Vec<u8>> {
    unsafe {
        let inb = CRYPT_INTEGER_BLOB { cbData: data.len() as u32, pbData: data.as_ptr() as *mut u8 };
        let mut out = CRYPT_INTEGER_BLOB { cbData: 0, pbData: null_mut() };
        if CryptProtectData(&inb, null(), null(), null(), null(), CRYPTPROTECT_UI_FORBIDDEN, &mut out) == 0 {
            return None;
        }
        let v = std::slice::from_raw_parts(out.pbData, out.cbData as usize).to_vec();
        LocalFree(out.pbData as _);
        Some(v)
    }
}

fn dpapi_unprotect(data: &[u8]) -> Option<Vec<u8>> {
    unsafe {
        let inb = CRYPT_INTEGER_BLOB { cbData: data.len() as u32, pbData: data.as_ptr() as *mut u8 };
        let mut out = CRYPT_INTEGER_BLOB { cbData: 0, pbData: null_mut() };
        if CryptUnprotectData(&inb, null_mut(), null(), null(), null(), CRYPTPROTECT_UI_FORBIDDEN, &mut out) == 0 {
            return None;
        }
        let v = std::slice::from_raw_parts(out.pbData, out.cbData as usize).to_vec();
        LocalFree(out.pbData as _);
        Some(v)
    }
}

fn pair_file() -> std::path::PathBuf {
    crate::paths::data_dir().join("paired.dat")
}

// ---------------------------------------------------------------- 自動起動(HKCU Run)

const RUN_KEY: &str = "Software\\Microsoft\\Windows\\CurrentVersion\\Run";
const RUN_NAME: &str = "frame-steamvr-remote";

pub fn autostart_enabled() -> bool {
    unsafe {
        let mut hk: HKEY = null_mut();
        if RegOpenKeyExW(HKEY_CURRENT_USER, w(RUN_KEY).as_ptr(), 0, KEY_READ, &mut hk) != 0 {
            return false;
        }
        let r = RegQueryValueExW(hk, w(RUN_NAME).as_ptr(), null(), null_mut(), null_mut(), null_mut());
        RegCloseKey(hk);
        r == 0
    }
}

pub fn set_autostart(on: bool) -> bool {
    unsafe {
        let mut hk: HKEY = null_mut();
        if RegOpenKeyExW(HKEY_CURRENT_USER, w(RUN_KEY).as_ptr(), 0, KEY_READ | KEY_WRITE, &mut hk) != 0 {
            return false;
        }
        let ok = if on {
            let exe = std::env::current_exe().map(|p| p.display().to_string()).unwrap_or_default();
            let val = w(&format!("\"{}\"", exe));
            RegSetValueExW(hk, w(RUN_NAME).as_ptr(), 0, REG_SZ, val.as_ptr() as *const u8, (val.len() * 2) as u32) == 0
        } else {
            let r = RegDeleteValueW(hk, w(RUN_NAME).as_ptr());
            r == 0 || r == ERROR_FILE_NOT_FOUND
        };
        RegCloseKey(hk);
        log!("Windows 起動時の自動起動を {} にした: {}", if on { "ON" } else { "OFF" }, if ok { "成功" } else { "失敗" });
        ok
    }
}

// ---------------------------------------------------------------- Host の実装

static TRAY_HWND: AtomicIsize = AtomicIsize::new(0);
static NOTES: Mutex<Vec<String>> = Mutex::new(Vec::new());
const WM_TRAY: u32 = WM_APP + 1;
const WM_NOTE: u32 = WM_APP + 2;
const TIMER_TIP: usize = 1;
const DIALOG_TITLE: &str = "frame-steamvr-remote ペアリング";

pub struct WinHost {
    pc_id: String,
}

impl WinHost {
    pub fn new(pc_id: String) -> Self {
        WinHost { pc_id }
    }
}

impl Host for WinHost {
    fn pc_name(&self) -> String {
        std::env::var("COMPUTERNAME").unwrap_or_else(|_| "Windows PC".into())
    }
    fn pc_id(&self) -> String {
        self.pc_id.clone()
    }
    fn steamvr_running(&self) -> bool {
        steamvr_running()
    }
    fn vrchat_running(&self) -> bool {
        vrchat_running()
    }
    fn quit_steamvr(&self) -> OpResult {
        notify_note("SteamVR を終了します(Frame からの操作)");
        quit_steamvr()
    }
    fn restart_steamvr(&self) -> OpResult {
        notify_note("SteamVR を再起動します(Frame からの操作)");
        restart_steamvr()
    }
    fn load_pairing(&self) -> Option<Pairing> {
        let raw = std::fs::read(pair_file()).ok()?;
        let plain = dpapi_unprotect(&raw)?;
        let v: serde_json::Value = serde_json::from_slice(&plain).ok()?;
        Some(Pairing {
            client_id: v["client_id"].as_str()?.to_string(),
            client_name: v["client_name"].as_str().unwrap_or("").to_string(),
            secret: crate::proto::from_hex(v["secret"].as_str()?)?,
        })
    }
    fn save_pairing(&self, p: &Pairing) -> Result<(), String> {
        let j = serde_json::json!({
            "client_id": p.client_id, "client_name": p.client_name,
            "secret": crate::proto::to_hex(&p.secret)
        });
        let enc = dpapi_protect(j.to_string().as_bytes()).ok_or("DPAPI で暗号化できなかった")?;
        let tmp = pair_file().with_extension("dat.tmp");
        std::fs::write(&tmp, enc).map_err(|e| e.to_string())?;
        std::fs::rename(&tmp, pair_file()).map_err(|e| e.to_string())
    }
    fn clear_pairing(&self) {
        let f = pair_file();
        if f.exists() {
            // 消さずに脇へ寄せる
            let to = f.with_extension(format!("dat.unpaired-{}", crate::proto::random_hex(3)));
            let _ = std::fs::rename(&f, to);
        }
    }
    fn notify(&self, msg: &str) {
        notify_note(msg);
    }
    fn close_code_dialog(&self) {
        unsafe {
            let h = FindWindowW(null(), w(DIALOG_TITLE).as_ptr());
            if !h.is_null() {
                PostMessageW(h, WM_CLOSE, 0, 0);
            }
        }
    }
}

fn notify_note(msg: &str) {
    NOTES.lock().unwrap_or_else(|e| e.into_inner()).push(msg.to_string());
    let h = TRAY_HWND.load(Ordering::SeqCst) as HWND;
    if !h.is_null() {
        unsafe { PostMessageW(h, WM_NOTE, 0, 0) };
    }
}

// ---------------------------------------------------------------- トレイ

static SERVER: OnceLock<Arc<Server>> = OnceLock::new();
static HOST: OnceLock<Arc<WinHost>> = OnceLock::new();
static TASKBAR_CREATED: OnceLock<u32> = OnceLock::new();

const ID_PAIR: usize = 1002;
const ID_UNPAIR: usize = 1003;
const ID_QUIT_VR: usize = 1004;
const ID_RESTART_VR: usize = 1005;
const ID_AUTOSTART: usize = 1006;
const ID_OPEN_LOG: usize = 1007;
const ID_EXIT: usize = 1008;

fn tip_text() -> String {
    let host = HOST.get();
    let vr = if steamvr_running() { "起動中" } else { "停止中" };
    let paired = host.map(|h| h.load_pairing().is_some()).unwrap_or(false);
    format!("frame-steamvr-remote\nSteamVR: {} / Frame: {}", vr, if paired { "ペアリング済み" } else { "未ペアリング" })
}

unsafe fn nid(hwnd: HWND) -> NOTIFYICONDATAW {
    let mut n: NOTIFYICONDATAW = std::mem::zeroed();
    n.cbSize = std::mem::size_of::<NOTIFYICONDATAW>() as u32;
    n.hWnd = hwnd;
    n.uID = 1;
    n
}

unsafe fn add_icon(hwnd: HWND) {
    let mut n = nid(hwnd);
    n.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
    n.uCallbackMessage = WM_TRAY;
    n.hIcon = LoadIconW(GetModuleHandleW(null()), 1 as *const u16);
    fill(&mut n.szTip, &tip_text());
    if Shell_NotifyIconW(NIM_ADD, &n) == 0 {
        log!("トレイアイコンの追加に失敗");
    }
}

unsafe fn update_tip(hwnd: HWND) {
    let mut n = nid(hwnd);
    n.uFlags = NIF_TIP;
    fill(&mut n.szTip, &tip_text());
    Shell_NotifyIconW(NIM_MODIFY, &n);
}

unsafe fn balloon(hwnd: HWND, text: &str) {
    let mut n = nid(hwnd);
    n.uFlags = NIF_INFO;
    n.dwInfoFlags = NIIF_INFO;
    fill(&mut n.szInfoTitle, "frame-steamvr-remote");
    fill(&mut n.szInfo, text);
    Shell_NotifyIconW(NIM_MODIFY, &n);
}

unsafe fn show_menu(hwnd: HWND) {
    let menu = CreatePopupMenu();
    let host = HOST.get().unwrap();
    let paired = host.load_pairing();
    let vr = if steamvr_running() { "SteamVR: 起動中" } else { "SteamVR: 停止中" };
    let pr = match &paired {
        Some(p) => format!("Frame: ペアリング済み({})", p.client_name),
        None => "Frame: 未ペアリング".to_string(),
    };
    let add = |flags: u32, id: usize, text: &str| {
        AppendMenuW(menu, flags, id, w(text).as_ptr());
    };
    add(MF_STRING | MF_GRAYED, 0, vr);
    add(MF_STRING | MF_GRAYED, 0, &pr);
    AppendMenuW(menu, MF_SEPARATOR, 0, null());
    add(MF_STRING, ID_PAIR, "Frame とペアリングする(コードを表示)");
    add(MF_STRING | if paired.is_some() { 0 } else { MF_GRAYED }, ID_UNPAIR, "ペアリングを解除する");
    AppendMenuW(menu, MF_SEPARATOR, 0, null());
    add(MF_STRING, ID_QUIT_VR, "SteamVR を終了する");
    add(MF_STRING, ID_RESTART_VR, "SteamVR を再起動する");
    AppendMenuW(menu, MF_SEPARATOR, 0, null());
    add(MF_STRING | if autostart_enabled() { MF_CHECKED } else { 0 }, ID_AUTOSTART, "Windows の起動時に自動で起動する");
    add(MF_STRING, ID_OPEN_LOG, "ログのフォルダを開く");
    add(MF_STRING, ID_EXIT, "終了");
    let mut pt = POINT { x: 0, y: 0 };
    GetCursorPos(&mut pt);
    SetForegroundWindow(hwnd);
    TrackPopupMenu(menu, TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, null());
    PostMessageW(hwnd, WM_NULL, 0, 0);
    DestroyMenu(menu);
}

fn spawn_code_dialog(code: String) {
    std::thread::spawn(move || unsafe {
        let spaced = format!("{} {}", &code[..3], &code[3..]);
        let text = format!(
            "Steam Frame のアプリ(frame-steamvr-remote)に、次のコードを入力してください。\n\n        {}\n\n{} 秒間だけ有効です。入力が済むとこの画面は自動で閉じます。",
            spaced,
            crate::proto::PAIR_VALID_SECS
        );
        MessageBoxW(null_mut(), w(&text).as_ptr(), w(DIALOG_TITLE).as_ptr(), MB_OK | MB_ICONINFORMATION | MB_TOPMOST | MB_SETFOREGROUND);
    });
}

unsafe extern "system" fn wndproc(hwnd: HWND, msg: u32, wp: WPARAM, lp: LPARAM) -> LRESULT {
    match msg {
        WM_CREATE => {
            TRAY_HWND.store(hwnd as isize, Ordering::SeqCst);
            add_icon(hwnd);
            SetTimer(hwnd, TIMER_TIP, 5000, None);
            0
        }
        WM_TIMER => {
            update_tip(hwnd);
            0
        }
        WM_TRAY => {
            let ev = (lp & 0xFFFF) as u32;
            if ev == WM_RBUTTONUP || ev == WM_LBUTTONUP || ev == WM_CONTEXTMENU {
                show_menu(hwnd);
            }
            0
        }
        WM_NOTE => {
            let notes: Vec<String> = std::mem::take(&mut *NOTES.lock().unwrap_or_else(|e| e.into_inner()));
            if let Some(last) = notes.last() {
                balloon(hwnd, last);
            }
            0
        }
        WM_COMMAND => {
            let id = wp & 0xFFFF;
            let srv = SERVER.get().unwrap().clone();
            let host = HOST.get().unwrap().clone();
            match id {
                ID_PAIR => {
                    let code = srv.open_pairing();
                    spawn_code_dialog(code);
                }
                ID_UNPAIR => {
                    host.clear_pairing();
                    log!("ペアリングを解除した(合言葉のファイルは脇に寄せた)");
                    notify_note("ペアリングを解除しました");
                }
                ID_QUIT_VR => {
                    std::thread::spawn(move || {
                        log!("トレイのメニューから SteamVR を終了");
                        let r = quit_steamvr();
                        notify_note(&format!("SteamVR の終了: {}", r.detail));
                    });
                }
                ID_RESTART_VR => {
                    std::thread::spawn(move || {
                        log!("トレイのメニューから SteamVR を再起動");
                        let r = restart_steamvr();
                        notify_note(&format!("SteamVR の再起動: {}", r.detail));
                    });
                }
                ID_AUTOSTART => {
                    set_autostart(!autostart_enabled());
                }
                ID_OPEN_LOG => {
                    let dir = crate::paths::data_dir();
                    ShellExecuteW(null_mut(), w("open").as_ptr(), w(&dir.display().to_string()).as_ptr(), null(), null(), SW_SHOWNORMAL);
                }
                ID_EXIT => {
                    DestroyWindow(hwnd);
                }
                _ => {}
            }
            0
        }
        WM_DESTROY => {
            let n = nid(hwnd);
            Shell_NotifyIconW(NIM_DELETE, &n);
            PostQuitMessage(0);
            0
        }
        m if Some(&m) == TASKBAR_CREATED.get() => {
            add_icon(hwnd);
            0
        }
        _ => DefWindowProcW(hwnd, msg, wp, lp),
    }
}

/// 二重起動を防ぐ。すでに動いていたら false。
pub fn single_instance() -> bool {
    unsafe {
        let h = CreateMutexW(null(), 0, w("Local\\frame-steamvr-remote-single").as_ptr());
        if h.is_null() {
            return true;
        }
        GetLastError() != ERROR_ALREADY_EXISTS
    }
}

pub fn run(server: Arc<Server>, host: Arc<WinHost>) {
    let _ = SERVER.set(server);
    let _ = HOST.set(host);
    unsafe {
        let hinst = GetModuleHandleW(null());
        let _ = TASKBAR_CREATED.set(RegisterWindowMessageW(w("TaskbarCreated").as_ptr()));
        let class = w("FrameSteamVrRemoteTray");
        let mut wc: WNDCLASSW = std::mem::zeroed();
        wc.lpfnWndProc = Some(wndproc);
        wc.hInstance = hinst;
        wc.lpszClassName = class.as_ptr();
        wc.hbrBackground = 0 as HBRUSH;
        if RegisterClassW(&wc) == 0 {
            log!("ウィンドウクラスの登録に失敗");
            return;
        }
        let hwnd = CreateWindowExW(0, class.as_ptr(), w("frame-steamvr-remote").as_ptr(), 0, 0, 0, 0, 0, null_mut(), null_mut(), hinst, null());
        if hwnd.is_null() {
            log!("トレイ用ウィンドウの作成に失敗");
            return;
        }
        let mut msg: MSG = std::mem::zeroed();
        while GetMessageW(&mut msg, null_mut(), 0, 0) > 0 {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
}

pub fn first_run_autostart() {
    let mut s = settings::load();
    if !s.autostart_initialized {
        s.autostart_initialized = true;
        settings::save(&s);
        set_autostart(true);
    }
}
