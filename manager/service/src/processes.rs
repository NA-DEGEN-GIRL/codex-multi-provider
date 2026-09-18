use crate::{
    protocol,
    windows::{self, Handle, wide},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    ffi::c_void,
    mem::{size_of, zeroed},
    path::{Path, PathBuf},
    ptr,
};
use windows_sys::Win32::{
    Foundation::*,
    System::{Diagnostics::ToolHelp::*, Threading::*},
    UI::WindowsAndMessaging::*,
};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Identity {
    pub process_id: u32,
    pub process_created: u64,
    pub executable_path: String,
}
pub struct Process {
    handle: Handle,
    termination_requested: std::cell::Cell<bool>,
    pub identity: Identity,
}
impl Process {
    fn open(pid: u32, terminate: bool) -> Option<Self> {
        unsafe {
            let raw = OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | 0x00100000 /* SYNCHRONIZE */ | if terminate { PROCESS_TERMINATE } else { 0 },
                0,
                pid,
            );
            if raw.is_null() {
                return None;
            }
            let handle = Handle(raw);
            if WaitForSingleObject(raw, 0) != WAIT_TIMEOUT {
                return None;
            }
            let mut times = [zeroed::<FILETIME>(); 4];
            if GetProcessTimes(
                raw,
                &mut times[0],
                &mut times[1],
                &mut times[2],
                &mut times[3],
            ) == 0
            {
                return None;
            }
            let mut buf = vec![0u16; 32768];
            let mut len = buf.len() as u32;
            if QueryFullProcessImageNameW(raw, 0, buf.as_mut_ptr(), &mut len) == 0 {
                return None;
            }
            Some(Self {
                handle,
                termination_requested: std::cell::Cell::new(false),
                identity: Identity {
                    process_id: pid,
                    process_created: ((times[0].dwHighDateTime as u64) << 32)
                        | times[0].dwLowDateTime as u64,
                    executable_path: String::from_utf16_lossy(&buf[..len as usize]),
                },
            })
        }
    }
    fn alive(&self) -> bool {
        unsafe { WaitForSingleObject(self.handle.0, 0) == WAIT_TIMEOUT }
    }
    fn terminate(&self) -> Result<(), String> {
        if self.termination_requested.get() {
            return Ok(());
        }
        if self.alive() && unsafe { TerminateProcess(self.handle.0, 1) } == 0 && self.alive() {
            return Err("프로필 프로세스를 종료하지 못했습니다.".into());
        }
        self.termination_requested.set(true);
        Ok(())
    }
    fn arguments(&self) -> Result<Vec<String>, String> {
        #[repr(C)]
        struct UnicodeString {
            length: u16,
            maximum: u16,
            buffer: *mut u16,
        }
        #[link(name = "ntdll")]
        unsafe extern "system" {
            fn NtQueryInformationProcess(
                handle: HANDLE,
                class: u32,
                buffer: *mut c_void,
                length: u32,
                returned: *mut u32,
            ) -> i32;
        }
        unsafe {
            let mut size = 0;
            NtQueryInformationProcess(self.handle.0, 60, ptr::null_mut(), 0, &mut size);
            if !(size_of::<UnicodeString>() as u32..=1024 * 1024).contains(&size) {
                return Err("프로필 실행 인수를 확인하지 못했습니다.".into());
            }
            let mut data = vec![0u64; (size as usize).div_ceil(8)];
            if NtQueryInformationProcess(
                self.handle.0,
                60,
                data.as_mut_ptr().cast(),
                size,
                &mut size,
            ) < 0
            {
                return Err("프로필 실행 인수 조회 실패".into());
            }
            let value = &*data.as_ptr().cast::<UnicodeString>();
            let start = data.as_ptr() as usize;
            let buffer = value.buffer as usize;
            if buffer < start
                || value.length % 2 != 0
                || buffer
                    .checked_add(value.length as usize)
                    .is_none_or(|end| end > start + data.len() * 8)
            {
                return Err("프로필 실행 인수 형식 오류".into());
            }
            let command = String::from_utf16_lossy(std::slice::from_raw_parts(
                value.buffer,
                value.length as usize / 2,
            ));
            let mut count = 0;
            let args = windows_sys::Win32::UI::Shell::CommandLineToArgvW(
                wide(&command).as_ptr(),
                &mut count,
            );
            if args.is_null() {
                return Err("프로필 실행 인수 해석 실패".into());
            }
            let result = (0..count as usize)
                .map(|index| {
                    let text = *args.add(index);
                    let mut len = 0;
                    while *text.add(len) != 0 {
                        len += 1;
                    }
                    String::from_utf16_lossy(std::slice::from_raw_parts(text, len))
                })
                .collect();
            LocalFree(args.cast());
            Ok(result)
        }
    }
}
pub fn identity(pid: u32) -> Option<Identity> {
    Process::open(pid, false).map(|p| p.identity.clone())
}
pub fn same_path(a: &str, b: &str) -> bool {
    a.replace('/', "\\")
        .trim_end_matches('\\')
        .eq_ignore_ascii_case(b.replace('/', "\\").trim_end_matches('\\'))
}
fn under(path: &str, root: &Path) -> bool {
    let path = PathBuf::from(path);
    path.is_absolute()
        && path
            .components()
            .all(|c| !matches!(c, std::path::Component::ParentDir))
        && path
            .to_string_lossy()
            .replace('/', "\\")
            .to_lowercase()
            .starts_with(&(root.to_string_lossy().replace('/', "\\").to_lowercase() + "\\"))
}

fn profile(root: &Path, args: &Value) -> Result<Value, String> {
    let id = protocol::uuid(args, "profile_id")?;
    let state: Value = serde_json::from_slice(
        &std::fs::read(root.join("work/control-center/state.json"))
            .map_err(|_| "프로필 상태를 읽지 못했습니다.")?,
    )
    .map_err(|_| "프로필 상태 형식 오류")?;
    state["profiles"]
        .as_array()
        .and_then(|p| p.iter().find(|p| p["id"].as_str() == Some(&id)))
        .cloned()
        .ok_or("프로필을 찾지 못했습니다.".into())
}

fn verified(root: &Path, args: &Value) -> Result<(Value, Option<Process>), String> {
    let profile = profile(root, args)?;
    if profile["generation"] != args["generation"] || args["generation"].as_str().is_none() {
        return Err("프로필 실행이 변경되었습니다.".into());
    }
    let pid = profile["process_id"]
        .as_u64()
        .ok_or("실행 프로필이 없습니다.")? as u32;
    let process = Process::open(pid, true);
    if process.is_none() && snapshot()?.contains_key(&pid) {
        return Err("프로필 종료 여부를 확인하지 못했습니다.".into());
    }
    if let Some(p) = &process
        && (Some(p.identity.process_created) != profile["process_created"].as_u64()
            || !same_path(
                &p.identity.executable_path,
                profile["executable_path"].as_str().unwrap_or(""),
            )
            || !under(
                &p.identity.executable_path,
                &root.join("artifacts/managed-desktop"),
            ))
    {
        return Err("프로필 프로세스 식별이 달라 종료하지 않았습니다.".into());
    }
    if let Some(p) = &process {
        let expected = root
            .join("work/control-center/profiles")
            .join(protocol::uuid(args, "profile_id")?);
        let ui = profile["ui_home"].as_str().ok_or("프로필 경로 없음")?;
        let arguments = p.arguments()?;
        let paths: Vec<_> = arguments
            .iter()
            .filter_map(|a| a.strip_prefix("--user-data-dir="))
            .collect();
        if !under(ui, &expected) || paths.len() != 1 || !same_path(paths[0], ui) {
            return Err("관리 앱 전용 Codex 프로필을 확인하지 못했습니다.".into());
        }
    }
    Ok((profile, process))
}

fn snapshot() -> Result<HashMap<u32, u32>, String> {
    unsafe {
        let raw = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if raw == INVALID_HANDLE_VALUE {
            return Err("프로세스 목록을 확인하지 못했습니다.".into());
        }
        let handle = Handle(raw);
        let mut row = zeroed::<PROCESSENTRY32W>();
        row.dwSize = size_of::<PROCESSENTRY32W>() as u32;
        let mut rows = HashMap::new();
        let mut has = Process32FirstW(handle.0, &mut row);
        while has != 0 {
            rows.insert(row.th32ProcessID, row.th32ParentProcessID);
            has = Process32NextW(handle.0, &mut row);
        }
        Ok(rows)
    }
}

pub fn execute(root: &Path, command: &str, args: &Value) -> Result<Value, String> {
    match command {
        "process.identity" => Ok(json!(identity(
            args["pid"].as_u64().ok_or("PID 없음")? as u32
        ))),
        "process.launch" => launch(root, args),
        "process.close" => {
            let (_, process) = verified(root, args)?;
            if let Some(process) = process {
                let hwnd = args["window_handle"]
                    .as_u64()
                    .ok_or("창을 확인하지 못했습니다.")? as HWND;
                let mut owner = 0;
                unsafe {
                    GetWindowThreadProcessId(hwnd, &mut owner);
                }
                if owner != process.identity.process_id || unsafe { IsWindow(hwnd) } == 0 {
                    return Err("프로필 창 소유자가 변경되었습니다.".into());
                }
                if unsafe { PostMessageW(hwnd, WM_CLOSE, 0, 0) } == 0 {
                    return Err("창 닫기 요청 실패".into());
                }
            }
            Ok(json!({"state":"close_requested"}))
        }
        "process.stop" => stop(root, args),
        _ => Err("지원하지 않는 프로세스 요청입니다.".into()),
    }
}

fn launch(root: &Path, args: &Value) -> Result<Value, String> {
    let profile = profile(root, args)?;
    let id = protocol::uuid(args, "profile_id")?;
    let generation = protocol::uuid(args, "generation")?;
    let expected = root.join("work/control-center/profiles").join(&id);
    let ui = profile["ui_home"].as_str().ok_or("프로필 경로 없음")?;
    let home = profile["home"].as_str().ok_or("프로필 경로 없음")?;
    if !under(ui, &expected) || !under(home, &expected) {
        return Err("관리 앱 전용 프로필이 아닙니다.".into());
    }
    let executable = args["executable"].as_str().ok_or("실행 파일 없음")?;
    if !under(executable, &root.join("artifacts/managed-desktop"))
        || !Path::new(executable).is_file()
        || executable.contains('"')
        || ui.contains('"')
    {
        return Err("관리 앱 실행 경로가 올바르지 않습니다.".into());
    }
    if let Some(pid) = profile["process_id"].as_u64()
        && let Some(active) = identity(pid as u32)
        && Some(active.process_created) == profile["process_created"].as_u64()
        && args["reopen"] != true
    {
        return Err("이미 실행 중인 프로필입니다.".into());
    }
    let mut env: BTreeMap<String, String> =
        serde_json::from_value(args["environment"].clone()).map_err(|_| "환경 형식 오류")?;
    if !env.get("CODEX_HOME").is_some_and(|v| same_path(v, home)) {
        return Err("실행할 프로필 경로가 다릅니다.".into());
    }
    env.remove("CODEX_MANAGER_BROKER_TOKEN");
    env.remove("CODEX_MANAGER_SERVICE_PIPE");
    let mut environment = Vec::<u16>::new();
    for (key, value) in env {
        if key.contains(['=', '\0']) || value.contains('\0') {
            return Err("환경 형식 오류".into());
        }
        environment.extend(format!("{key}={value}").encode_utf16());
        environment.push(0);
    }
    environment.push(0);
    let suffix = if args["reopen"] == true {
        ""
    } else {
        " codex://threads/new?mode=codex"
    };
    let mut command = wide(&format!(
        "\"{executable}\" \"--user-data-dir={ui}\"{suffix}"
    ));
    unsafe {
        let mut startup = zeroed::<STARTUPINFOW>();
        startup.cb = size_of::<STARTUPINFOW>() as u32;
        startup.dwFlags = STARTF_USESHOWWINDOW;
        startup.wShowWindow = if args["embed"] == true {
            SW_HIDE as u16
        } else {
            SW_SHOWNORMAL as u16
        };
        let mut info = zeroed::<PROCESS_INFORMATION>();
        if CreateProcessW(
            wide(executable).as_ptr(),
            command.as_mut_ptr(),
            ptr::null(),
            ptr::null(),
            0,
            CREATE_UNICODE_ENVIRONMENT | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            environment.as_ptr().cast::<c_void>(),
            wide(&root.to_string_lossy()).as_ptr(),
            &startup,
            &mut info,
        ) == 0
        {
            return Err("Codex 프로세스를 시작하지 못했습니다.".into());
        }
        let process = Handle(info.hProcess);
        let _thread = Handle(info.hThread);
        let mut times = [zeroed::<FILETIME>(); 4];
        if GetProcessTimes(
            process.0,
            &mut times[0],
            &mut times[1],
            &mut times[2],
            &mut times[3],
        ) == 0
        {
            return Err("실행 식별을 확인하지 못했습니다.".into());
        }
        let identity = Identity {
            process_id: info.dwProcessId,
            process_created: ((times[0].dwHighDateTime as u64) << 32)
                | times[0].dwLowDateTime as u64,
            executable_path: executable.to_owned(),
        };
        let state =
            json!({"state":"running","profile_id":id,"generation":generation,"identity":identity});
        if args["reopen"] != true {
            windows::atomic(
                &root
                    .join("work/control-center/instances")
                    .join(&id)
                    .join("process-owner.json"),
                &serde_json::to_vec(&state).unwrap(),
            )
            .map_err(|_| "실행 상태 기록 실패")?;
        }
        drop(process); // Close our handle, never terminate the independent GUI.
        Ok(json!(identity))
    }
}

fn stop(root: &Path, args: &Value) -> Result<Value, String> {
    let (profile, main) = verified(root, args)?;
    let Some(main) = main else {
        return Ok(json!({"state":"already_stopped","all_selected_processes_exited":true}));
    };
    let id = profile["id"].as_str().unwrap();
    let state: Value = serde_json::from_slice(
        &std::fs::read(root.join("work/control-center/state.json"))
            .map_err(|_| "프로필 상태 확인 실패")?,
    )
    .map_err(|_| "프로필 상태 확인 실패")?;
    let other_profiles: HashSet<u32> = state["profiles"]
        .as_array()
        .ok_or("프로필 상태 확인 실패")?
        .iter()
        .filter(|p| p["id"] != profile["id"])
        .filter_map(|p| p["process_id"].as_u64().map(|id| id as u32))
        .collect();
    if other_profiles.contains(&main.identity.process_id) {
        return Err("다른 프로필 실행이 중복 지정되어 종료하지 않았습니다.".into());
    }
    let mut held = HashMap::from([(main.identity.process_id, main)]);
    let mut preserved = HashSet::new();
    let start = std::time::Instant::now();
    let mut terminated = false;
    let mut stable = 0;
    loop {
        let rows = snapshot()?;
        loop {
            let ready: Vec<_> = rows
                .iter()
                .filter(|(pid, parent)| !held.contains_key(pid) && held.contains_key(parent))
                .map(|(pid, parent)| (*pid, *parent))
                .collect();
            if ready.is_empty() {
                break;
            }
            let mut added = false;
            for (pid, parent) in ready {
                if pid == std::process::id() || other_profiles.contains(&pid) || held.len() >= 512 {
                    return Err("종료 범위가 올바르지 않습니다.".into());
                }
                if let Some(child) = Process::open(pid, true) {
                    if child.identity.process_created < held[&parent].identity.process_created {
                        return Err("하위 프로세스 식별이 변경되었습니다.".into());
                    }
                    let name = Path::new(&child.identity.executable_path)
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy()
                        .to_lowercase();
                    if preserved.contains(&parent)
                        || [
                            "wslhost.exe",
                            "wslservice.exe",
                            "vmmemwsl.exe",
                            "svchost.exe",
                        ]
                        .contains(&name.as_str())
                    {
                        preserved.insert(pid);
                    }
                    held.insert(pid, child);
                    added = true;
                }
            }
            if !added {
                break;
            }
        }
        if !terminated {
            held[&(profile["process_id"].as_u64().unwrap() as u32)].terminate()?;
            terminated = true;
        }
        for (pid, p) in &held {
            if !preserved.contains(pid) {
                p.terminate()?;
            }
        }
        if held
            .iter()
            .all(|(pid, p)| preserved.contains(pid) || !p.alive())
        {
            stable += 1;
        } else {
            stable = 0;
        }
        if stable >= 2 {
            break;
        }
        if start.elapsed().as_secs() > 12 {
            return Err("프로필 종료 확인 시간이 초과되었습니다.".into());
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    let result = json!({"state":"stopped","profile_id":id,"generation":profile["generation"],"all_selected_processes_exited":true,
        "processes":held.values().map(|p|p.identity.clone()).collect::<Vec<_>>(),"preserved_infrastructure":preserved});
    windows::atomic(
        &root
            .join("work/control-center/instances")
            .join(id)
            .join("last-recovery.json"),
        &serde_json::to_vec(&result).unwrap(),
    )
    .map_err(|_| "종료 기록 저장 실패")?;
    Ok(result)
}
