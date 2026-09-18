use crate::{protocol, windows};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct TaskKey {
    pub host_id: String,
    pub thread_id: String,
}
impl TaskKey {
    pub fn parse(args: &Value) -> Result<Self, String> {
        let task = &args["task"];
        let host = task["host_id"].as_str().unwrap_or("local");
        if host.is_empty() || host.len() > 256 || host.chars().any(char::is_control) {
            return Err("작업 호스트가 올바르지 않습니다.".into());
        }
        Ok(Self {
            host_id: host.into(),
            thread_id: protocol::uuid(task, "thread_id")?,
        })
    }
    fn path(&self, root: &Path) -> PathBuf {
        let hash = format!("{:x}", Sha256::digest(serde_json::to_vec(self).unwrap()));
        root.join("work/control-center/notes")
            .join(format!("{hash}.json"))
    }
}

#[derive(Clone, Serialize, Deserialize)]
pub struct Item {
    pub id: String,
    pub text: String,
    pub done: bool,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Note {
    pub id: String,
    pub title: String,
    pub kind: String,
    pub body: String,
    pub items: Vec<Item>,
    pub revision: u64,
    pub deleted: bool,
    pub updated_at: u64,
}
#[derive(Serialize, Deserialize)]
struct Document {
    version: u32,
    task: TaskKey,
    notes: Vec<Note>,
}

pub fn execute(root: &Path, command: &str, args: &Value) -> Result<Value, String> {
    let task = TaskKey::parse(args)?;
    let path = task.path(root);
    let mut doc = if path.exists() {
        if std::fs::metadata(&path)
            .map_err(|_| "메모 파일을 확인하지 못했습니다.")?
            .len()
            > 4 * 1024 * 1024
        {
            return Err("메모 파일 크기를 확인해 주세요.".into());
        }
        let bytes = std::fs::read(&path).map_err(|_| "메모 파일을 읽지 못했습니다.")?;
        let value: Document = serde_json::from_slice(&bytes)
            .map_err(|_| "메모 파일이 손상되었습니다. 원본을 보존했습니다.")?;
        if value.version != 1 || value.task != task {
            return Err("메모 작업 정보가 다릅니다. 원본을 보존했습니다.".into());
        }
        value
    } else {
        Document {
            version: 1,
            task,
            notes: vec![],
        }
    };
    if command == "notes.list" {
        return Ok(json!({"task":doc.task,"notes":doc.notes}));
    }
    let id = protocol::uuid(args, "note_id")?;
    let expected = args["revision"].as_u64().ok_or("메모 버전이 없습니다.")?;
    let index = doc.notes.iter().position(|n| n.id == id);
    let current = index.map(|i| doc.notes[i].revision).unwrap_or(0);
    if current != expected {
        return Ok(json!({"state":"conflict","current":index.map(|i| doc.notes[i].clone())}));
    }
    let mut note = index.map(|i| doc.notes[i].clone()).unwrap_or(Note {
        id,
        title: String::new(),
        kind: "text".into(),
        body: String::new(),
        items: vec![],
        revision: 0,
        deleted: false,
        updated_at: 0,
    });
    match command {
        "notes.save" => {
            if note.deleted {
                return Err("삭제된 메모입니다. 복구 후 편집해 주세요.".into());
            }
            note.title = args["title"].as_str().unwrap_or("").trim().into();
            if note.title.is_empty()
                || note.title.chars().count() > 80
                || note.title.chars().any(char::is_control)
            {
                return Err("메모 이름은 1~80자로 입력해 주세요.".into());
            }
            note.kind = args["kind"].as_str().unwrap_or("text").into();
            if !["text", "checklist"].contains(&note.kind.as_str()) {
                return Err("지원하지 않는 메모 종류입니다.".into());
            }
            note.body = args["body"].as_str().unwrap_or("").into();
            note.items = serde_json::from_value(args.get("items").cloned().unwrap_or(json!([])))
                .map_err(|_| "체크리스트 형식이 잘못되었습니다.")?;
            if note.body.len() > 128 * 1024 || note.items.len() > 500 {
                return Err("메모가 너무 큽니다. 새 탭으로 나눠 주세요.".into());
            }
            let mut ids = HashSet::new();
            for item in &note.items {
                if uuid::Uuid::parse_str(&item.id).is_err()
                    || !ids.insert(&item.id)
                    || item.text.len() > 8192
                {
                    return Err("체크리스트 항목을 확인해 주세요.".into());
                }
            }
        }
        "notes.delete" if index.is_some() => note.deleted = true,
        "notes.restore" if index.is_some() => note.deleted = false,
        _ => return Err("지원하지 않는 메모 요청입니다.".into()),
    }
    note.revision += 1;
    note.updated_at = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;
    match index {
        Some(i) => doc.notes[i] = note.clone(),
        None if doc.notes.len() < 128 => doc.notes.push(note.clone()),
        _ => return Err("메모 탭이 너무 많습니다.".into()),
    }
    let bytes = serde_json::to_vec_pretty(&doc).map_err(|_| "메모를 저장하지 못했습니다.")?;
    if bytes.len() > 4 * 1024 * 1024 {
        return Err("작업 메모 용량을 초과했습니다.".into());
    }
    windows::atomic(&path, &bytes)
        .map_err(|_| "메모 저장에 실패했습니다. 편집 내용은 유지됩니다.")?;
    Ok(json!({"state":"saved","note":note}))
}
