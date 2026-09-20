//! Saved catalog links and editable remote IDs refer to the same note document.
use crate::notes::TaskKey;
use serde_json::Value;
use std::path::Path;

pub fn is_remote(host: &str) -> bool {
    host.starts_with("ssh:") || host.starts_with("remote-ssh-discovered:")
}

pub fn resolve(root: &Path, task: TaskKey) -> Result<TaskKey, String> {
    if !is_remote(&task.host_id) || task.path(root).exists() {
        return Ok(task);
    }
    let path = root.join("work/control-center/note-aliases.json");
    if !path.exists() {
        return Ok(task);
    }
    let metadata = std::fs::symlink_metadata(&path).map_err(|e| e.to_string())?;
    if !metadata.is_file() || metadata.len() > 4 * 1024 * 1024 {
        return Err("메모 연결 파일을 확인하지 못했습니다.".into());
    }
    let aliases: Value = serde_json::from_slice(&std::fs::read(path).map_err(|e| e.to_string())?)
        .map_err(|_| "메모 연결 정보가 손상되었습니다. 원본은 보존되었습니다.")?;
    let key = format!("{}\0{}", task.host_id, task.thread_id);
    let Some(target) = aliases.get(key).and_then(Value::as_str) else {
        return Ok(task);
    };
    if uuid::Uuid::parse_str(target).is_err() {
        return Err("메모 원본 작업 정보가 올바르지 않습니다.".into());
    }
    let resolved = TaskKey {
        host_id: task.host_id,
        thread_id: target.into(),
    };
    if !resolved.path(root).is_file() {
        return Err("연결된 원본 메모를 찾지 못했습니다. 새 메모로 덮어쓰지 않았습니다.".into());
    }
    Ok(resolved)
}
