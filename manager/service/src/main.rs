#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
mod backend;
mod note_aliases;
mod notes;
mod processes;
mod protocol;
mod records;
mod windows;

use protocol::{Request, error, ok};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncWriteExt, BufReader},
    net::windows::named_pipe::NamedPipeServer,
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
    draining: AtomicBool,
    retire: Mutex<()>,
    stopped: Notify,
    admission: tokio::sync::RwLock<()>,
    records: Arc<records::Hub>,
    /// Record-only connections: never counted in `clients`, so they cannot
    /// hold retirement, idle exit or a management connection slot.
    record_clients: AtomicUsize,
    /// Accepted connections that have not sent a request yet. Not counted
    /// in `clients` either: a record writer's reconnect loop must not look
    /// like another management window to retirement or idle exit.
    pending_clients: AtomicUsize,
}

/// Management (general) connections.
const MAX_CLIENTS: usize = 32;
/// Connections accepted but still waiting for their first request.
const MAX_PENDING_CLIENTS: usize = 64;

/// Takes a slot under `cap`, atomically with every other taker.
fn try_join(counter: &AtomicUsize, cap: usize) -> bool {
    counter
        .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
            (n < cap).then_some(n + 1)
        })
        .is_ok()
}
impl Service {
    async fn dispatch(self: &Arc<Self>, request: Request) -> Value {
        let id = &request.id;
        let command = request.command.as_str();
        if id.is_empty() || id.len() > 128 || !request.args.is_object() {
            return error(id, "invalid_request", "관리 요청 형식이 올바르지 않습니다.");
        }
        if command.starts_with("records.") {
            return self.records_command(id, command, &request.args).await;
        }
        if command == "supervisor.status" {
            let mut status = self.backend.status().await;
            status["version"] = json!(protocol::VERSION);
            status["supervisor_pid"] = json!(std::process::id());
            status["service_revision"] = json!(self.revision);
            status["engine"] = json!("rust");
            status["clients"] = json!(self.clients.load(Ordering::Relaxed));
            status["record_clients"] = json!(self.record_clients.load(Ordering::Relaxed));
            status["pending_clients"] = json!(self.pending_clients.load(Ordering::Relaxed));
            status["records"] = self.records.status();
            status["preserves_background_profiles"] = json!(true);
            status["graceful_shutdown"] = json!(true);
            status["shutdown_draining"] = json!(self.draining.load(Ordering::SeqCst));
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
        if command == "supervisor.retire" || command == "supervisor.shutdown" {
            let explicit = command == "supervisor.shutdown";
            if explicit && request.version != protocol::VERSION {
                return error(id, "protocol_version", "관리 프로그램 버전이 맞지 않습니다.");
            }
            let _guard = self.retire.lock().await;
            let Ok(_admission) = self.admission.try_write() else {
                return error(id, "backend_busy", "진행 중인 관리 요청이 있습니다.");
            };
            if self.clients.load(Ordering::Relaxed) > 1 {
                return error(id, "backend_busy", "다른 관리창이 연결되어 있습니다. 다른 관리창을 닫아 주세요.");
            }
            if self.backend.pending() > 0
                || !self.operations.lock().unwrap().is_empty()
            {
                return error(id, "backend_busy", "진행 중인 관리 작업이 있습니다.");
            }
            if !self.draining.load(Ordering::SeqCst)
                && self.backend.status().await["backend_status"] == "faulted"
                && !self.backend.reconnect().await
            {
                return error(
                    id,
                    "backend_busy",
                    "기존 관리 어댑터의 종료를 확인하고 있습니다.",
                );
            }
            if !self.draining.load(Ordering::SeqCst)
                && self.backend.status().await["backend_status"] != "not_started" {
                let state = self.backend.request("state", json!({})).await;
                if !(if explicit { can_drain(&state) } else { can_stop(&state) }) {
                    return error(
                        id,
                        "backend_busy",
                        "실행 중인 프로필과 관리 작업을 유지합니다. 완전 종료 후 관리 서비스를 바꿀 수 있습니다.",
                    );
                }
            }
            if explicit || self.draining.load(Ordering::SeqCst) {
                // EOF stops schedulers and lets admitted workers finish. A saved
                // SSH retry/verification journal is not a live worker. Never
                // kill the adapter or replay an uncertain remote operation.
                self.draining.store(true, Ordering::SeqCst);
                if !self.backend.reconnect().await {
                    return error(id, "backend_busy", "진행 중인 SSH 확인과 관리 작업을 마무리하고 있습니다. 예약 기록은 보존됩니다.");
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
        if self.draining.load(Ordering::SeqCst) {
            return error(id, "service_draining", "관리 작업을 마무리하며 종료 중입니다. 잠시 뒤 완전 종료를 다시 눌러 주세요.");
        }
        let _admission = self.admission.read().await;
        if self.stopping.load(Ordering::SeqCst) || self.draining.load(Ordering::SeqCst) {
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
            // A native fork may be opened before the next manager state poll.
            // Resolve only this task's metadata before joining its note group.
            if notes::needs_fork_refresh(&self.root, &request.args) {
                let refreshed = self
                    .backend
                    .request("notes.refresh_forks", request.args.clone())
                    .await;
                if refreshed["ok"] != true {
                    return error(
                        id,
                        "notes_failed",
                        "메모 원본 작업을 확인하지 못했습니다. 다시 불러와 주세요.",
                    );
                }
            }
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
        let root = self.root.clone();
        let _ = tokio::task::spawn_blocking(move || log_event(&root, &event)).await;
        result
    }

    // Content-free (thread ids, hosts, kinds): no broker token and no protocol
    // version pin (injected adapters outlive service updates), and never
    // admission, profile gates or the backend. A long poll must not hold up
    // retirement or any other request.
    async fn records_command(&self, id: &str, command: &str, args: &Value) -> Value {
        match command {
            "records.poll" => {
                let query = match records::Query::parse(args) {
                    Ok(query) => query,
                    Err(message) => return error(id, "invalid_request", &message),
                };
                if self.stopping.load(Ordering::SeqCst) {
                    return ok(id, records::closing(&query));
                }
                ok(id, self.records.poll(query).await)
            }
            // Same grouping pipeline as the signal files; identity binding
            // and the rate budget are per connection (see `records_gate`).
            "records.publish" => {
                let publish = match records::Publish::parse(args) {
                    Ok(publish) => publish,
                    Err(message) => return error(id, "invalid_request", &message),
                };
                if self.stopping.load(Ordering::SeqCst) {
                    return ok(id, records::publish_closing());
                }
                match self.records.publish(publish, records::now_ms()) {
                    records::Published::Accepted(n) => ok(id, json!({"accepted":n})),
                    records::Published::Busy => error(
                        id,
                        "records_busy",
                        "기록 동기화 대기열이 가득 찼습니다. 잠시 뒤 다시 보내 주세요.",
                    ),
                    records::Published::Closing => ok(id, records::publish_closing()),
                }
            }
            _ => error(id, "unknown_command", "지원하지 않는 관리 명령입니다."),
        }
    }
}

// IDs, timing and status only: never request arguments, notes, keys or tokens.
fn log_event(root: &Path, event: &Value) {
    use std::io::Write;
    let path = root.join("work/control-center/logs/rust-service.jsonl");
    let _ = std::fs::create_dir_all(path.parent().unwrap());
    if std::fs::metadata(&path).is_ok_and(|m| m.len() > 2 * 1024 * 1024) {
        let _ = std::fs::rename(&path, path.with_extension("previous.jsonl"));
    }
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
    {
        // One append per line: `writeln!` would emit a Value piecewise and
        // interleave with concurrent loggers (e.g. many record connections).
        let line = format!("{event}\n");
        let _ = f.write_all(line.as_bytes());
    }
}

fn log_async(root: &Path, event: Value) {
    let root = root.to_path_buf();
    drop(tokio::task::spawn_blocking(move || {
        log_event(&root, &event)
    }));
}

fn allowed(command: &str) -> bool {
    [
        "manager.startup",
        "manager.stop_warmup",
        "manager.resume_launches",
        "profile.cleanup",
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
        "skills.bridge.set",
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
        "profile.remote_restart",
        "profile.remote_stop",
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
        "remote.updates.status",
        "remote.updates.check",
        "remote.updates.settings",
        "remote.updates.schedule",
        "remote.updates.cancel",
        "remote.updates.stock_update",
    ]
    .contains(&command)
}
fn can_stop(response: &Value) -> bool {
    can_stop_management(response, false)
}

fn can_drain(response: &Value) -> bool {
    response["result"]["local_launches"]["stopping"] == true
        && response["result"]["local_launches"]["active"] == 0
        && can_stop_management(response, true)
}

fn can_stop_management(response: &Value, drain_remote_queue: bool) -> bool {
    let state = &response["result"];
    response["ok"] == true
        && state["profiles"].is_array()
        // UI disconnection or a shell update must not retire the backend that
        // owns SSH forwards, skill execution, pending requests and profile work.
        // Unknown observations are not proof that an owned process has exited.
        && profiles_exited(&state["profiles"])
        && state.get("view_instances").is_none_or(profiles_exited)
        && state.get("local_launches").is_none_or(|launches| launches["active"] == 0)
        && ["updates", "startup_updates", "profile_warmup", "remote_updates"].iter().all(|key| {
            (drain_remote_queue && *key == "remote_updates") || state[key]
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

fn profiles_exited(profiles: &Value) -> bool {
    profiles.as_array().is_some_and(|profiles| {
        profiles.iter().all(|profile| {
            matches!(
                profile["status"].as_str(),
                Some("not_started" | "stopped" | "unprepared")
            ) && profile.get("process_id").is_none_or(Value::is_null)
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
    let listener = match windows::server(&pipe, true) {
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
    // Only the pipe owner reads and writes the record journal.
    let records = Arc::new(records::Hub::open(&root));
    records.start();
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
        draining: AtomicBool::new(false),
        retire: Mutex::new(()),
        stopped: Notify::new(),
        admission: tokio::sync::RwLock::new(()),
        records,
        record_clients: AtomicUsize::new(0),
        pending_clients: AtomicUsize::new(0),
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
    listen(service.clone(), pipe, listener).await?;
    // Let the retirement response flush. No profile process is terminated.
    tokio::time::sleep(Duration::from_millis(100)).await;
    service.backend.reconnect().await;
    Ok(())
}

async fn listen(
    service: Arc<Service>,
    pipe: String,
    mut listener: NamedPipeServer,
) -> std::io::Result<()> {
    let result = loop {
        tokio::select! {
            result = listener.connect() => if let Err(e) = result { break Err(e) },
            _ = service.stopped.notified() => break Ok(()),
        }
        let connected = listener;
        listener = match windows::server(&pipe, false) {
            Ok(listener) => listener,
            Err(e) => break Err(e),
        };
        if !try_join(&service.pending_clients, MAX_PENDING_CLIENTS) {
            continue;
        }
        let service = service.clone();
        tokio::spawn(async move {
            let counter = match serve(connected, service.clone()).await {
                Connection::Pending => &service.pending_clients,
                Connection::Records => &service.record_clients,
                Connection::General => &service.clients,
            };
            counter.fetch_sub(1, Ordering::SeqCst);
        });
    };
    // Pending record polls answer {closing:true} at once; groups still in
    // their quiet window are journaled for the next service instance.
    service.records.close();
    let records = service.records.clone();
    let _ = tokio::task::spawn_blocking(move || records.finish(records::now_ms())).await;
    result
}

/// A connection is pending from accept until its first request decides
/// whether it is a management client or a record-only (poll/publish) one.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Connection {
    Pending,
    General,
    Records,
}

/// Per-connection record rules, checked before dispatch. The first valid
/// `profile` a connection sends becomes its identity (the origin its
/// publishes carry); a later request claiming another profile is refused.
/// Publishes also spend the connection's and the service-wide rate budget.
/// Returns the refusal.
fn records_gate(
    service: &Service,
    request: &Request,
    identity: &mut Option<String>,
    throttle: &mut records::Throttle,
    pid: u32,
) -> Option<Value> {
    let publish = match request.command.as_str() {
        "records.publish" => true,
        "records.poll" => false,
        // Answered by dispatch as unknown_command.
        _ => return None,
    };
    if publish {
        let events = request.args["events"].as_array().map_or(0, Vec::len);
        if !service
            .records
            .admit_publish(throttle, events, Instant::now())
        {
            return Some(error(
                &request.id,
                "records_busy",
                "기록 동기화 요청이 너무 많습니다. 잠시 뒤 다시 보내 주세요.",
            ));
        }
    }
    // A missing or malformed profile binds nothing; dispatch rejects it.
    let claimed = protocol::uuid(&request.args, "profile").ok()?;
    match identity {
        Some(bound) if *bound != claimed => Some(error(
            &request.id,
            "profile_mismatch",
            "이 기록 동기화 연결은 다른 프로필의 연결입니다.",
        )),
        Some(_) => None,
        None => {
            // Caller identity for diagnostics only, once: a PID and profile UUID.
            log_async(
                &service.root,
                json!({"event":"records.connected","pid":pid,"profile":claimed}),
            );
            *identity = Some(claimed);
            None
        }
    }
}

type PipeWriter = Arc<Mutex<tokio::io::WriteHalf<NamedPipeServer>>>;

async fn write_line(writer: &PipeWriter, value: &Value) {
    let mut response = serde_json::to_vec(value).unwrap();
    response.push(b'\n');
    let _ = tokio::time::timeout(Duration::from_secs(3), async {
        writer.lock().await.write_all(&response).await
    })
    .await;
}

async fn serve(pipe: NamedPipeServer, service: Arc<Service>) -> Connection {
    let Ok(pid) = windows::require_client_authority(&pipe) else {
        return Connection::Pending;
    };
    let (read, write) = tokio::io::split(pipe);
    let mut reader = BufReader::new(read);
    let writer = Arc::new(Mutex::new(write));
    let capacity = Arc::new(Semaphore::new(16));
    let mut class = Connection::Pending;
    let (connected, mut polls, mut profile) = (Instant::now(), 0u64, None);
    let mut throttle = records::Throttle::connection();
    let mut record_tasks: Vec<tokio::task::AbortHandle> = Vec::new();
    while let Ok(Some(bytes)) = protocol::frame(&mut reader, protocol::REQUEST_LIMIT).await {
        let request: Request = match serde_json::from_slice(&bytes) {
            Ok(r) => r,
            Err(_) => {
                let value: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
                let response = error(
                    value["id"].as_str().unwrap_or(""),
                    "invalid_request",
                    "관리 요청 형식이 올바르지 않습니다.",
                );
                write_line(&writer, &response).await;
                continue;
            }
        };
        let record = request.command.starts_with("records.");
        // Every class change takes its new slot under that pool's cap before
        // releasing the old one; a refused switch keeps the old class.
        match (class, record) {
            (Connection::Pending, true) => {
                if !try_join(&service.record_clients, records::MAX_CONNECTIONS) {
                    let response =
                        error(&request.id, "records_busy", "기록 동기화 연결이 많습니다.");
                    write_line(&writer, &response).await;
                    break;
                }
                service.pending_clients.fetch_sub(1, Ordering::SeqCst);
                class = Connection::Records;
            }
            (Connection::Pending, false) => {
                if !try_join(&service.clients, MAX_CLIENTS) {
                    let response = error(&request.id, "busy", "관리창 연결이 많습니다.");
                    write_line(&writer, &response).await;
                    break;
                }
                service.pending_clients.fetch_sub(1, Ordering::SeqCst);
                class = Connection::General;
            }
            (Connection::Records, false) => {
                // Mixed use counts as management again, within its 32 slots;
                // refused, the connection stays a record connection.
                if !try_join(&service.clients, MAX_CLIENTS) {
                    let response = error(&request.id, "busy", "관리창 연결이 많습니다.");
                    write_line(&writer, &response).await;
                    continue;
                }
                service.record_clients.fetch_sub(1, Ordering::SeqCst);
                class = Connection::General;
            }
            _ => {}
        }
        polls += u64::from(record);
        if record
            && let Some(refusal) =
                records_gate(&service, &request, &mut profile, &mut throttle, pid)
        {
            write_line(&writer, &refusal).await;
            continue;
        }
        let permit = match capacity.clone().try_acquire_owned() {
            Ok(p) => p,
            Err(_) => {
                let response = error(&request.id, "busy", "진행 중인 관리 요청이 많습니다.");
                write_line(&writer, &response).await;
                continue;
            }
        };
        // Only a parked poll is aborted on disconnect; a publish is short and
        // runs to completion even if its writer has already gone.
        let poll = request.command == "records.poll";
        let service = service.clone();
        let writer = writer.clone();
        let task = tokio::spawn(async move {
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
        if poll {
            record_tasks.retain(|task| !task.is_finished());
            record_tasks.push(task.abort_handle());
        }
    }
    // A departed poller's wait is read-only: never keep it parked.
    for task in record_tasks {
        task.abort();
    }
    if polls > 0 {
        log_async(
            &service.root,
            json!({"event":"records.disconnected","pid":pid,"profile":profile,"polls":polls,"connected_ms":connected.elapsed().as_millis()}),
        );
    }
    class
}

#[cfg(test)]
mod tests;
