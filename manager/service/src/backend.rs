use crate::protocol::{self, frame};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    io::{AsyncWriteExt, BufReader},
    process::{Child, Command},
    sync::{Mutex as AsyncMutex, mpsc, oneshot},
};

struct Worker {
    sender: Option<mpsc::Sender<Value>>,
    child: Child,
    reader: tokio::task::JoinHandle<()>,
    writer: tokio::task::JoinHandle<()>,
}
pub struct Backend {
    root: PathBuf,
    bundle: PathBuf,
    pipe: String,
    token: String,
    worker: AsyncMutex<Option<Worker>>,
    pending: Arc<Mutex<HashMap<String, oneshot::Sender<Value>>>>,
    fault: Arc<Mutex<bool>>,
    lifecycle: tokio::sync::RwLock<()>,
}
impl Backend {
    pub fn new(root: PathBuf, bundle: PathBuf, pipe: String, token: String) -> Self {
        Self {
            root,
            bundle,
            pipe,
            token,
            worker: AsyncMutex::new(None),
            pending: Arc::default(),
            fault: Arc::default(),
            lifecycle: tokio::sync::RwLock::new(()),
        }
    }
    pub fn pending(&self) -> usize {
        self.pending.lock().unwrap().len()
    }
    pub async fn status(&self) -> Value {
        let worker = self.worker.lock().await;
        json!({"backend_pid":worker.as_ref().and_then(|w|w.child.id()),"backend_status":if *self.fault.lock().unwrap(){"faulted"}else if worker.is_none(){"not_started"}else if self.pending()>0{"busy"}else{"ready"},"pending_requests":self.pending()})
    }
    async fn start(&self) -> Result<mpsc::Sender<Value>, String> {
        let mut worker = self.worker.lock().await;
        if *self.fault.lock().unwrap() {
            return Err("관리 어댑터 연결에 문제가 있습니다. 연결 상태를 확인해 주세요.".into());
        }
        if let Some(w) = worker.as_ref() {
            return w
                .sender
                .clone()
                .ok_or("관리 어댑터 종료를 확인하고 있습니다.".into());
        }
        let script = if self.bundle.join("runtime-manifest.json").is_file() {
            self.bundle.join("scripts/control_center.py")
        } else {
            self.root.join("scripts/control_center.py")
        };
        let python = find_python(&self.bundle).await?;
        let mut command = Command::new(python);
        command
            .args(["-u", "-X", "utf8"])
            .arg(script)
            .args(["--serve", "--root"])
            .arg(&self.root)
            .current_dir(&self.root)
            .creation_flags(0x08000000)
            .env("PYTHONIOENCODING", "utf-8")
            .env("PYTHONUTF8", "1")
            .env(
                "CODEX_MANAGER_PROTOCOL_VERSION",
                protocol::VERSION.to_string(),
            )
            .env("CODEX_MANAGER_SERVICE_PIPE", &self.pipe)
            .env("CODEX_MANAGER_BROKER_TOKEN", &self.token)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::null());
        let mut child = command
            .spawn()
            .map_err(|_| "관리 호환 어댑터를 시작하지 못했습니다.")?;
        let mut input = child.stdin.take().unwrap();
        let output = child.stdout.take().unwrap();
        let (sender, mut receiver) = mpsc::channel::<Value>(64);
        let pending = self.pending.clone();
        let fault = self.fault.clone();
        let writer = tokio::spawn(async move {
            while let Some(value) = receiver.recv().await {
                let mut bytes = serde_json::to_vec(&value).unwrap();
                bytes.push(b'\n');
                if input.write_all(&bytes).await.is_err() {
                    fail(&pending, &fault);
                    break;
                }
            }
        });
        let pending = self.pending.clone();
        let fault = self.fault.clone();
        let reader = tokio::spawn(async move {
            let mut reader = BufReader::new(output);
            loop {
                let line = match frame(&mut reader, protocol::RESPONSE_LIMIT).await {
                    Ok(Some(line)) => line,
                    _ => break,
                };
                let value: Value = match serde_json::from_slice(&line) {
                    Ok(value) => value,
                    Err(_) => break,
                };
                let Some(id) = value["id"].as_str() else {
                    break;
                };
                if !value["ok"].is_boolean() {
                    break;
                }
                let request = pending.lock().unwrap().remove(id);
                if let Some(request) = request {
                    let _ = request.send(value);
                } else {
                    break; // Unknown IDs are protocol corruption, not notifications.
                }
            }
            fail(&pending, &fault);
        });
        *worker = Some(Worker {
            sender: Some(sender.clone()),
            child,
            reader,
            writer,
        });
        Ok(sender)
    }
    pub async fn request(&self, command: &str, args: Value) -> Value {
        let _lifecycle = self.lifecycle.read().await;
        let id = uuid::Uuid::new_v4().to_string();
        let sender = match self.start().await {
            Ok(s) => s,
            Err(e) => return protocol::error(&id, "backend_unavailable", &e),
        };
        let (tx, rx) = oneshot::channel();
        {
            let mut pending = self.pending.lock().unwrap();
            if pending.len() >= 64 {
                return protocol::error(
                    &id,
                    "busy",
                    "관리 요청이 많습니다. 잠시 후 다시 시도해 주세요.",
                );
            }
            pending.insert(id.clone(), tx);
        }
        if sender
            .send(json!({"id":id,"command":command,"args":args}))
            .await
            .is_err()
        {
            self.pending.lock().unwrap().remove(&id);
            return protocol::error(&id, "backend_unavailable", "관리 어댑터가 종료되었습니다.");
        }
        match tokio::time::timeout(
            Duration::from_secs(if command == "state" { 20 } else { 180 }),
            rx,
        )
        .await
        {
            Ok(Ok(value)) => value,
            _ => protocol::error(
                &id,
                "request_pending",
                "응답 대기 시간이 지났습니다. 이미 시작된 처리는 계속될 수 있습니다. 중복 실행하지 않고 상태를 확인해 주세요.",
            ),
        }
    }
    pub async fn reconnect(&self) -> bool {
        let Ok(_lifecycle) = self.lifecycle.try_write() else {
            return false;
        };
        if self.pending() != 0 {
            return false;
        }
        let mut worker = self.worker.lock().await;
        if let Some(old) = worker.as_mut() {
            old.sender.take();
            if !matches!(
                tokio::time::timeout(Duration::from_secs(3), old.child.wait()).await,
                Ok(Ok(_))
            ) {
                *self.fault.lock().unwrap() = true;
                return false;
            }
            // Join the old readers before publishing a replacement. Their EOF
            // must never fault a newly started adapter or drain its requests.
            if !old.reader.is_finished() || !old.writer.is_finished() {
                tokio::time::sleep(Duration::from_millis(20)).await;
                if !old.reader.is_finished() || !old.writer.is_finished() {
                    return false;
                }
            }
        }
        worker.take();
        *self.fault.lock().unwrap() = false;
        true
    }
}
fn fail(pending: &Mutex<HashMap<String, oneshot::Sender<Value>>>, fault: &Mutex<bool>) {
    *fault.lock().unwrap() = true;
    for (id, tx) in pending.lock().unwrap().drain() {
        let _ = tx.send(protocol::error(
            &id,
            "backend_unavailable",
            "관리 어댑터 연결이 종료되었습니다. 실행 요청을 자동 재전송하지 않았습니다.",
        ));
    }
}
async fn find_python(bundle: &Path) -> Result<PathBuf, String> {
    if let Ok(path) = std::fs::read_to_string(bundle.join("python-path.txt")) {
        let p = PathBuf::from(path.trim());
        if p.is_absolute() && p.is_file() {
            return Ok(p);
        }
    }
    let local = std::env::var("LOCALAPPDATA").unwrap_or_default();
    for version in ["314", "313", "312", "311"] {
        for path in [
            format!(r"C:\Python{version}\python.exe"),
            format!(r"{local}\Programs\Python\Python{version}\python.exe"),
        ] {
            if Path::new(&path).is_file() {
                return Ok(path.into());
            }
        }
    }
    Err("Python 3.11 이상을 찾지 못했습니다.".into())
}
