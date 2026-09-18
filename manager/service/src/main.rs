#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod backend;
mod notes;
mod processes;
mod protocol;
mod windows;

use protocol::{Request, error, ok};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncWriteExt, BufReader},
    sync::{Mutex, Notify, Semaphore},
};

struct Service {
    root: PathBuf,
    backend: backend::Backend,
    token: String,
    revision: String,
    notes: Arc<Mutex<()>>,
    gates: Mutex<HashMap<String, Arc<Mutex<()>>>>,
    operations: std::sync::Mutex<HashMap<String, Value>>,
    clients: AtomicUsize,
    stopping: AtomicBool,
    retire: Mutex<()>,
    stopped: Notify,
    admission: tokio::sync::RwLock<()>,
}
impl Service {
    async fn dispatch(self: &Arc<Self>, request: Request) -> Value {
        let id = &request.id;
        let command = request.command.as_str();
        if id.is_empty() || id.len() > 128 || !request.args.is_object() {
            return error(id, "invalid_request", "관리 요청 형식이 올바르지 않습니다.");
        }
        if command == "supervisor.status" {
            let mut status = self.backend.status().await;
            status["version"] = json!(protocol::VERSION);
            status["supervisor_pid"] = json!(std::process::id());
            status["service_revision"] = json!(self.revision);
            status["engine"] = json!("rust");
            status["clients"] = json!(self.clients.load(Ordering::Relaxed));
            status["operations"] = json!(
                self.operations
                    .lock()
                    .unwrap()
                    .values()
                    .cloned()
                    .collect::<Vec<_>>()
            );
            return ok(id, status);
        }
        if command == "supervisor.retire" {
            let _guard = self.retire.lock().await;
            let Ok(_admission) = self.admission.try_write() else {
                return error(id, "backend_busy", "진행 중인 관리 요청이 있습니다.");
            };
            if self.clients.load(Ordering::Relaxed) > 1
                || self.backend.pending() > 0
                || !self.operations.lock().unwrap().is_empty()
            {
                return error(id, "backend_busy", "진행 중인 관리 작업이 있습니다.");
            }
            if self.backend.status().await["backend_status"] == "faulted"
                && !self.backend.reconnect().await
            {
                return error(
                    id,
                    "backend_busy",
                    "기존 관리 어댑터의 종료를 확인하고 있습니다.",
                );
            }
            if self.backend.status().await["backend_status"] != "not_started" {
                let state = self.backend.request("state", json!({})).await;
                if !can_stop(&state) {
                    return error(
                        id,
                        "backend_busy",
                        "설정 적용이 끝난 뒤 관리 서비스를 바꿀 수 있습니다.",
                    );
                }
            }
            self.stopping.store(true, Ordering::SeqCst);
            self.stopped.notify_one();
            return ok(id, json!({"retiring":true}));
        }
        if request.version != protocol::VERSION {
            return error(
                id,
                "protocol_version",
                "관리 프로그램 버전이 맞지 않습니다.",
            );
        }
        if self.stopping.load(Ordering::SeqCst) {
            return error(id, "service_updating", "관리 서비스가 종료 중입니다.");
        }
        if command.starts_with("process.") {
            if request.args["broker_token"].as_str() != Some(&self.token) {
                return error(id, "unauthorized", "내부 프로세스 요청만 허용합니다.");
            }
            let root = self.root.clone();
            let args = request.args;
            let command = command.to_string();
            return match tokio::task::spawn_blocking(move || {
                processes::execute(&root, &command, &args)
            })
            .await
            {
                Ok(Ok(value)) => ok(id, value),
                Ok(Err(e)) => error(id, "process_failed", &e),
                Err(_) => error(id, "process_failed", "프로세스 관리 오류"),
            };
        }
        let _admission = self.admission.read().await;
        if self.stopping.load(Ordering::SeqCst) {
            return error(id, "service_updating", "관리 서비스가 종료 중입니다.");
        }
        if command == "supervisor.reconnect" {
            return if self.backend.reconnect().await {
                ok(id, json!({"reconnected":true}))
            } else {
                error(id, "backend_busy", "기존 관리 요청이 아직 처리 중입니다.")
            };
        }
        if command.starts_with("notes.") {
            let root = self.root.clone();
            let args = request.args;
            let command = command.to_string();
            let guard = self.notes.clone().lock_owned().await;
            return match tokio::task::spawn_blocking(move || {
                let _guard = guard;
                notes::execute(&root, &command, &args)
            })
            .await
            {
                Ok(Ok(value)) => ok(id, value),
                Ok(Err(e)) => error(id, "notes_failed", &e),
                Err(_) => error(id, "notes_failed", "메모 저장 오류"),
            };
        }
        if !allowed(command) {
            return error(id, "unknown_command", "지원하지 않는 관리 명령입니다.");
        }
        let group = request.args["profile_id"]
            .as_str()
            .map(|p| format!("profile:{p}"));
        let gate = if let Some(group) = group {
            Some(
                self.gates
                    .lock()
                    .await
                    .entry(group)
                    .or_insert_with(|| Arc::new(Mutex::new(())))
                    .clone(),
            )
        } else {
            None
        };
        let _guard = match gate {
            Some(g) => Some(g.lock_owned().await),
            None => None,
        };
        let op = uuid::Uuid::new_v4().to_string();
        let started = Instant::now();
        self.operations.lock().unwrap().insert(op.clone(),json!({"id":op,"command":command,"profile_id":request.args["profile_id"],"phase":"running"}));
        let mut result = self.backend.request(command, request.args).await;
        self.operations.lock().unwrap().remove(&op);
        result["id"] = json!(id);
        // IDs, timing and status only: never log request arguments, notes, keys or tokens.
        let event = json!({"id":op,"command":command,"elapsed_ms":started.elapsed().as_millis(),"ok":result["ok"],"code":result["error"]["code"]});
        let path = self
            .root
            .join("work/control-center/logs/rust-service.jsonl");
        let _ = tokio::task::spawn_blocking(move || {
            use std::io::Write;
            let _ = std::fs::create_dir_all(path.parent().unwrap());
            if std::fs::metadata(&path).is_ok_and(|m| m.len() > 2 * 1024 * 1024) {
                let _ = std::fs::rename(&path, path.with_extension("previous.jsonl"));
            }
            if let Ok(mut f) = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .open(path)
            {
                let _ = writeln!(f, "{event}");
            }
        })
        .await;
        result
    }
}

fn allowed(command: &str) -> bool {
    [
        "manager.startup",
        "manager.recover_legacy",
        "state",
        "accounts.refresh",
        "profile.add",
        "profile.register_current",
        "profile.remove",
        "profile.restore",
        "profile.bind",
        "profile.rename",
        "profile.move",
        "skills.personal.list",
        "skills.personal.set",
        "skills.personal.delete",
        "skills.personal.restore",
        "profile.show",
        "profile.prepare",
        "profile.login",
        "profile.login_status",
        "shortcut.add",
        "shortcut.move",
        "shortcut.rename",
        "shortcut.delete",
        "shortcut.undo",
        "conversation.open",
        "conversation.continue",
        "conversation.navigate",
        "handoff.preview",
        "catalog.list",
        "catalog.show",
        "catalog.resolve",
        "policy.set",
        "profile.model_settings",
        "profile.restart",
        "profile.recover",
        "providers.list",
        "providers.save",
        "providers.key",
        "providers.verify",
        "updates.check",
        "updates.prepare",
        "updates.apply",
        "remote.list",
        "remote.inspect",
        "remote.prepare",
    ]
    .contains(&command)
}
fn can_stop(response: &Value) -> bool {
    let state = &response["result"];
    response["ok"] == true
        && state["profiles"].is_array()
        && ["updates", "startup_updates"].iter().all(|key| {
            state[key]
                .get("worker_active")
                .is_none_or(|active| active == false)
        })
        && state.get("profile_restarts").is_none_or(|jobs| {
            jobs.as_object().is_some_and(|jobs| {
                jobs.values().all(|j| {
                    ["complete", "attention", "superseded"]
                        .contains(&j["phase"].as_str().unwrap_or(""))
                })
            })
        })
}

#[tokio::main]
async fn main() {
    if let Err(e) = run().await {
        eprintln!("Manager service: {e}");
        std::process::exit(1);
    }
}
async fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 || args[1] != "--root" {
        return Err("--root required".into());
    }
    let root = PathBuf::from(&args[2]);
    if !root.is_absolute() || !root.join("scripts/control_center.py").is_file() {
        return Err("invalid root".into());
    }
    let pipe = windows::pipe_name(&root)?;
    let mut listener = match windows::server(&pipe, true) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => return Ok(()),
        Err(e) => return Err(e.into()),
    };
    let bundle = std::env::current_exe()?.parent().unwrap().to_path_buf();
    let revision = std::fs::read(bundle.join("runtime-manifest.json"))
        .ok()
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
        .and_then(|v| v["service_revision"].as_str().map(str::to_string))
        .unwrap_or(format!("development-{}", protocol::VERSION));
    let token = uuid::Uuid::new_v4().to_string();
    let service = Arc::new(Service {
        root: root.clone(),
        backend: backend::Backend::new(root, bundle, pipe.clone(), token.clone()),
        token,
        revision,
        notes: Arc::default(),
        gates: Mutex::default(),
        operations: std::sync::Mutex::default(),
        clients: AtomicUsize::new(0),
        stopping: AtomicBool::new(false),
        retire: Mutex::new(()),
        stopped: Notify::new(),
        admission: tokio::sync::RwLock::new(()),
    });
    tokio::spawn({
        let service = service.clone();
        async move {
            let mut idle_since = Instant::now();
            loop {
                tokio::time::sleep(Duration::from_secs(15)).await;
                if service.stopping.load(Ordering::SeqCst) {
                    return;
                }
                if service.clients.load(Ordering::SeqCst) > 0 {
                    idle_since = Instant::now();
                    continue;
                }
                if idle_since.elapsed() < Duration::from_secs(60) || service.backend.pending() > 0 {
                    continue;
                }
                let _retire = service.retire.lock().await;
                let Ok(_admission) = service.admission.try_write() else {
                    continue;
                };
                if service.backend.status().await["backend_status"] == "faulted"
                    && !service.backend.reconnect().await
                {
                    continue;
                }
                if service.backend.status().await["backend_status"] != "not_started"
                    && !can_stop(&service.backend.request("state", json!({})).await)
                {
                    continue;
                }
                if service.clients.load(Ordering::SeqCst) > 0 || service.backend.pending() > 0 {
                    continue;
                }
                service.stopping.store(true, Ordering::SeqCst);
                service.stopped.notify_one();
                return;
            }
        }
    });
    loop {
        tokio::select! {result=listener.connect()=>result?,_=service.stopped.notified()=>break}
        let connected = listener;
        listener = windows::server(&pipe, false)?;
        if service.clients.load(Ordering::Relaxed) >= 32 {
            continue;
        }
        service.clients.fetch_add(1, Ordering::SeqCst);
        let service = service.clone();
        tokio::spawn(async move {
            serve(connected, service.clone()).await;
            service.clients.fetch_sub(1, Ordering::SeqCst);
        });
    }
    // Let the retirement response flush. No profile process is terminated.
    tokio::time::sleep(Duration::from_millis(100)).await;
    service.backend.reconnect().await;
    Ok(())
}
async fn serve(pipe: tokio::net::windows::named_pipe::NamedPipeServer, service: Arc<Service>) {
    let (read, write) = tokio::io::split(pipe);
    let mut reader = BufReader::new(read);
    let writer = Arc::new(Mutex::new(write));
    let capacity = Arc::new(Semaphore::new(16));
    while let Ok(Some(bytes)) = protocol::frame(&mut reader, protocol::REQUEST_LIMIT).await {
        let request: Request = match serde_json::from_slice(&bytes) {
            Ok(r) => r,
            Err(_) => {
                let value: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
                let mut response = serde_json::to_vec(&error(
                    value["id"].as_str().unwrap_or(""),
                    "invalid_request",
                    "관리 요청 형식이 올바르지 않습니다.",
                ))
                .unwrap();
                response.push(b'\n');
                let _ = tokio::time::timeout(Duration::from_secs(3), async {
                    writer.lock().await.write_all(&response).await
                })
                .await;
                continue;
            }
        };
        let permit = match capacity.clone().try_acquire_owned() {
            Ok(p) => p,
            Err(_) => {
                let mut response = serde_json::to_vec(&error(
                    &request.id,
                    "busy",
                    "진행 중인 관리 요청이 많습니다.",
                ))
                .unwrap();
                response.push(b'\n');
                let _ = tokio::time::timeout(Duration::from_secs(3), async {
                    writer.lock().await.write_all(&response).await
                })
                .await;
                continue;
            }
        };
        let service = service.clone();
        let writer = writer.clone();
        tokio::spawn(async move {
            let _permit = permit;
            let result = service.dispatch(request).await;
            let mut bytes = serde_json::to_vec(&result).unwrap();
            if bytes.len() > protocol::RESPONSE_LIMIT {
                bytes = serde_json::to_vec(&error(
                    result["id"].as_str().unwrap_or(""),
                    "response_too_large",
                    "관리 응답이 너무 큽니다.",
                ))
                .unwrap();
            }
            bytes.push(b'\n');
            let _ = tokio::time::timeout(Duration::from_secs(3), async {
                writer.lock().await.write_all(&bytes).await
            })
            .await;
        });
    }
}

#[cfg(test)]
mod tests;
