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
    pub(crate) fn path(&self, root: &Path) -> PathBuf {
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
pub struct Image {
    pub id: String,
    pub width: u32,
    pub height: u32,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct Note {
    pub id: String,
    pub title: String,
    pub kind: String,
    pub body: String,
    pub items: Vec<Item>,
    #[serde(default)]
    pub images: Vec<Image>,
    pub revision: u64,
    pub deleted: bool,
    pub updated_at: u64,
}
#[derive(Serialize, Deserialize)]
struct Document {
    version: u32,
    task: TaskKey,
    #[serde(default)]
    group_id: Option<String>,
    #[serde(default)]
    notes: Vec<Note>,
}

#[derive(Serialize, Deserialize)]
struct Group {
    version: u32,
    group_id: String,
    notes: Vec<Note>,
}

/// Group ids come from stored JSON, so they are validated before they are
/// turned into a file name. Anything else could escape the notes directory.
fn valid_group_id(group_id: &str) -> bool {
    uuid::Uuid::parse_str(group_id).is_ok()
}

fn group_path(root: &Path, group_id: &str) -> PathBuf {
    debug_assert!(valid_group_id(group_id));
    root.join("work/control-center/note-groups")
        .join(format!("{group_id}.json"))
}

/// Local native forks share their parent's note group; remote task identities
/// stay isolated.
fn fork_parent(root: &Path, task: &TaskKey) -> Option<String> {
    if task.host_id != "local" {
        return None;
    }
    let path = root.join("work/control-center/note-forks.json");
    let metadata = std::fs::metadata(&path).ok()?;
    if metadata.len() > 4 * 1024 * 1024 {
        return None;
    }
    let value: Value = serde_json::from_str(&std::fs::read_to_string(&path).ok()?).ok()?;
    let entry = value.get(&task.thread_id)?;
    let parent = entry.as_str().map(str::to_string).or_else(|| {
        entry
            .get("parent_thread_id")
            .and_then(Value::as_str)
            .map(str::to_string)
    })?;
    (parent != task.thread_id && uuid::Uuid::parse_str(&parent).is_ok()).then_some(parent)
}

fn read_document(path: &Path, task: &TaskKey) -> Result<Option<Document>, String> {
    if !path.exists() {
        return Ok(None);
    }
    if std::fs::metadata(path)
        .map_err(|_| "메모 파일을 확인하지 못했습니다.")?
        .len()
        > 4 * 1024 * 1024
    {
        return Err("메모 파일 크기를 확인해 주세요.".into());
    }
    let bytes = std::fs::read(path).map_err(|_| "메모 파일을 읽지 못했습니다.")?;
    let value: Document = serde_json::from_slice(&bytes)
        .map_err(|_| "메모 파일이 손상되었습니다. 원본은 보존되었습니다.")?;
    if ![1, 2, 3].contains(&value.version) {
        return Err("메모 형식을 확인할 수 없습니다. 원본은 보존되었습니다.".into());
    }
    if &value.task != task {
        return Err("메모 작업 정보가 다릅니다. 원본은 보존되었습니다.".into());
    }
    Ok(Some(value))
}

fn read_group(root: &Path, group_id: &str) -> Result<Group, String> {
    if !valid_group_id(group_id) {
        return Err("메모 그룹 형식이 다릅니다. 원본은 보존되었습니다.".into());
    }
    let path = group_path(root, group_id);
    if std::fs::metadata(&path)
        .map_err(|_| "메모 그룹을 확인하지 못했습니다.")?
        .len()
        > 4 * 1024 * 1024
    {
        return Err("메모 그룹 크기를 확인해 주세요.".into());
    }
    let bytes = std::fs::read(&path).map_err(|_| "메모 그룹을 읽지 못했습니다.")?;
    let value: Group = serde_json::from_slice(&bytes)
        .map_err(|_| "메모 그룹이 손상되었습니다. 원본은 보존되었습니다.")?;
    if ![1, 2].contains(&value.version) || value.group_id != group_id {
        return Err("메모 그룹 형식이 다릅니다. 원본은 보존되었습니다.".into());
    }
    Ok(value)
}

fn write_group(root: &Path, group: &Group) -> Result<(), String> {
    if !valid_group_id(&group.group_id) {
        return Err("메모 그룹 형식이 다릅니다. 원본은 보존되었습니다.".into());
    }
    let mut value = serde_json::to_value(group).map_err(|_| "메모를 저장하지 못했습니다.")?;
    // Old services must refuse image documents rather than silently dropping
    // unknown attachment fields during a later text-only save.
    value["version"] = json!(if group.notes.iter().any(|n| !n.images.is_empty()) {
        2
    } else {
        1
    });
    let bytes = serde_json::to_vec_pretty(&value).map_err(|_| "메모를 저장하지 못했습니다.")?;
    if bytes.len() > 4 * 1024 * 1024 {
        return Err("작업 메모 용량을 초과했습니다.".into());
    }
    windows::atomic(&group_path(root, &group.group_id), &bytes)
        .map_err(|_| "메모 저장에 실패했습니다. 편집 내용은 유지됩니다.".into())
}

fn write_document(root: &Path, document: &Document) -> Result<(), String> {
    let mut value = serde_json::to_value(document).map_err(|_| "메모를 저장하지 못했습니다.")?;
    if document.notes.iter().any(|n| !n.images.is_empty()) {
        value["version"] = json!(3);
    }
    let bytes = serde_json::to_vec_pretty(&value).map_err(|_| "메모를 저장하지 못했습니다.")?;
    if bytes.len() > 4 * 1024 * 1024 {
        return Err("작업 메모 용량을 초과했습니다.".into());
    }
    windows::atomic(&document.task.path(root), &bytes)
        .map_err(|_| "메모 저장에 실패했습니다. 편집 내용은 유지됩니다.".into())
}

fn clone_notes(notes: &[Note]) -> Vec<Note> {
    notes
        .iter()
        .map(|note| Note {
            id: uuid::Uuid::new_v4().to_string(),
            title: note.title.clone(),
            kind: note.kind.clone(),
            body: note.body.clone(),
            items: note
                .items
                .iter()
                .map(|item| Item {
                    id: uuid::Uuid::new_v4().to_string(),
                    text: item.text.clone(),
                    done: item.done,
                })
                .collect(),
            images: note.images.clone(),
            revision: 0,
            deleted: note.deleted,
            updated_at: note.updated_at,
        })
        .collect()
}

/// Moves an independent document's notes into a new shared group so a native
/// fork can join it. Note ids, revisions and checklist items are preserved.
/// The group is written first, so a failed document write can never hide the
/// notes; the unreferenced group file is removed again on that failure.
fn move_notes_into_group(root: &Path, source: &mut Document) -> Result<String, String> {
    let version = source.version;
    let group_id = uuid::Uuid::new_v4().to_string();
    let group = Group {
        version: 1,
        group_id: group_id.clone(),
        notes: source.notes.clone(),
    };
    write_group(root, &group)?;
    let notes = std::mem::take(&mut source.notes);
    source.version = 2;
    source.group_id = Some(group_id.clone());
    if let Err(error) = write_document(root, source) {
        let _ = std::fs::remove_file(group_path(root, &group_id));
        source.version = version;
        source.group_id = None;
        source.notes = notes;
        return Err(error);
    }
    Ok(group_id)
}

pub fn needs_fork_refresh(root: &Path, args: &Value) -> bool {
    TaskKey::parse(args)
        .and_then(|task| crate::note_aliases::resolve(root, task))
        .map(|task| {
            (task.host_id == "local" || crate::note_aliases::is_remote(&task.host_id))
                && !task.path(root).exists()
        })
        .unwrap_or(false)
}

fn load_task(
    root: &Path,
    task: TaskKey,
    visited: &mut HashSet<String>,
) -> Result<Document, String> {
    if let Some(document) = read_document(&task.path(root), &task)? {
        return Ok(document); // Never overwrite a task that already has its own notes.
    }
    if visited.len() >= 64 || !visited.insert(task.thread_id.clone()) {
        return Err("메모 원본 작업의 분기 관계를 확인하지 못했습니다.".into());
    }
    let mut document = Document {
        version: 1,
        task: task.clone(),
        group_id: None,
        notes: vec![],
    };
    if let Some(parent) = fork_parent(root, &task) {
        let mut source = load_task(
            root,
            TaskKey {
                host_id: task.host_id.clone(),
                thread_id: parent,
            },
            visited,
        )?;
        // A native fork shares the parent's mutable note group instead of
        // copying a snapshot. Documents that already exist are never
        // rewritten by this path, so old independent notes stay untouched.
        let group_id = match source.group_id.clone() {
            // Join the parent's existing group, including legacy groups.
            Some(group_id) => {
                read_group(root, &group_id)?;
                group_id
            }
            // The parent still owns independent notes: move them into a new
            // group, keeping their ids and revisions, so both tasks share it.
            // This also persists an empty group, so a later parent addition
            // shows up in the fork.
            None => move_notes_into_group(root, &mut source)?,
        };
        document.version = 2;
        document.group_id = Some(group_id);
        document.notes = vec![];
        write_document(root, &document)?;
    }
    Ok(document)
}

pub fn execute(root: &Path, command: &str, args: &Value) -> Result<Value, String> {
    let task = crate::note_aliases::resolve(root, TaskKey::parse(args)?)?;
    let mut document = load_task(root, task, &mut HashSet::new())?;

    if command == "notes.fork" {
        // "Split notes" copies a shared group into a private document with
        // fresh ids. An already independent document has nothing to copy and
        // must keep its ids, so a repeated split changes nothing.
        if document.group_id.is_none() {
            let notes = resolved_notes(root, &document)?;
            return Ok(
                json!({"state": "forked", "task": document.task, "notes": notes,
                             "shared": false}),
            );
        }
        let current = resolved_notes(root, &document)?;
        let split = clone_notes(&current);
        document.version = 1;
        document.group_id = None;
        document.notes = split.clone();
        write_document(root, &document)?;
        return Ok(
            json!({"state": "forked", "task": document.task, "notes": split,
                         "shared": false}),
        );
    }
    if command == "notes.list" {
        return Ok(
            json!({"task": document.task, "notes": resolved_notes(root, &document)?,
                         "shared": document.group_id.is_some(), "image_attachments_version": 1}),
        );
    }
    let id = protocol::uuid(args, "note_id")?;
    let expected = args["revision"].as_u64().ok_or("?? ??? ????.")?;
    let mut notes = resolved_notes(root, &document)?;
    let index = notes.iter().position(|n| n.id == id);
    let current = index.map(|i| notes[i].revision).unwrap_or(0);
    if current != expected {
        return Ok(json!({"state": "conflict", "current": index.map(|i| notes[i].clone())}));
    }
    let mut note = index.map(|i| notes[i].clone()).unwrap_or(Note {
        id,
        title: String::new(),
        kind: "text".into(),
        body: String::new(),
        items: vec![],
        images: vec![],
        revision: 0,
        deleted: false,
        updated_at: 0,
    });
    match command {
        "notes.save" => {
            if note.deleted {
                return Err("??? ?????. ?? ? ??? ???.".into());
            }
            note.title = args["title"].as_str().unwrap_or("").trim().into();
            if note.title.is_empty()
                || note.title.chars().count() > 80
                || note.title.chars().any(char::is_control)
            {
                return Err("?? ??? 1~80?? ??? ???.".into());
            }
            note.kind = args["kind"].as_str().unwrap_or("text").into();
            if !["text", "checklist"].contains(&note.kind.as_str()) {
                return Err("???? ?? ?? ?????.".into());
            }
            note.body = args["body"].as_str().unwrap_or("").into();
            note.items = serde_json::from_value(args.get("items").cloned().unwrap_or(json!([])))
                .map_err(|_| "????? ??? ???? ????.".to_string())?;
            if note.body.len() > 128 * 1024 || note.items.len() > 500 {
                return Err("??? ?? ???. ??? ?? ???.".into());
            }
            let mut ids = HashSet::new();
            for item in &note.items {
                if uuid::Uuid::parse_str(&item.id).is_err()
                    || !ids.insert(&item.id)
                    || item.text.len() > 8192
                {
                    return Err("????? ??? ??? ???.".into());
                }
            }
            if let Some(value) = args.get("images") {
                let images: Vec<Image> = serde_json::from_value(value.clone())
                    .map_err(|_| "이미지 첨부 형식을 확인해 주세요.".to_string())?;
                validate_images(root, &images, &note.images)?;
                note.images = images;
            }
        }
        "notes.delete" if index.is_some() => note.deleted = true,
        "notes.restore" if index.is_some() => note.deleted = false,
        _ => return Err("???? ?? ?? ?????.".into()),
    }
    note.revision += 1;
    note.updated_at = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;
    match index {
        Some(i) => notes[i] = note.clone(),
        None if notes.len() < 128 => notes.push(note.clone()),
        _ => return Err("?? ?? ?? ????.".into()),
    }
    store_notes(root, &mut document, notes)?;
    Ok(json!({"state": "saved", "note": note}))
}

fn validate_images(root: &Path, images: &[Image], previous: &[Image]) -> Result<(), String> {
    use std::io::Read;
    if images.len() > 24 {
        return Err("한 메모에는 이미지를 24개까지 첨부할 수 있습니다.".into());
    }
    let mut ids = HashSet::new();
    for image in images {
        if image.id.len() != 64
            || !image
                .id
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            || !ids.insert(&image.id)
            || image.width == 0
            || image.height == 0
            || u64::from(image.width) * u64::from(image.height) > 40_000_000
        {
            return Err("이미지 첨부 정보가 올바르지 않습니다.".into());
        }
        // Immutable content-addressed assets are checked once when attached,
        // never rehashed for every text keystroke/autosave.
        if previous
            .iter()
            .any(|p| p.id == image.id && p.width == image.width && p.height == image.height)
        {
            continue;
        }
        let path = root
            .join("work/control-center/note-images")
            .join(format!("{}.png", image.id));
        let file = std::fs::File::open(path)
            .map_err(|_| "이미지 원본을 찾지 못했습니다. 다시 첨부해 주세요.")?;
        let size = file
            .metadata()
            .map_err(|_| "이미지 파일을 확인하지 못했습니다.")?
            .len();
        if size > 16 * 1024 * 1024 || size < 24 {
            return Err("이미지 한 장은 16MB 이하여야 합니다.".into());
        }
        let mut bytes = Vec::new();
        file.take(16 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "이미지를 읽지 못했습니다.")?;
        if bytes.len() > 16 * 1024 * 1024
            || bytes.len() < 24
            || &bytes[..8] != b"\x89PNG\r\n\x1a\n"
            || &bytes[12..16] != b"IHDR"
            || u32::from_be_bytes(bytes[16..20].try_into().unwrap()) != image.width
            || u32::from_be_bytes(bytes[20..24].try_into().unwrap()) != image.height
            || format!("{:x}", Sha256::digest(&bytes)) != image.id
        {
            return Err("이미지 원본이 손상되었거나 변경되었습니다. 다시 첨부해 주세요.".into());
        }
    }
    Ok(())
}

fn resolved_notes(root: &Path, document: &Document) -> Result<Vec<Note>, String> {
    match document.group_id.as_deref() {
        Some(group_id) => Ok(read_group(root, group_id)?.notes),
        None => Ok(document.notes.clone()),
    }
}

fn store_notes(root: &Path, document: &mut Document, notes: Vec<Note>) -> Result<(), String> {
    match document.group_id.clone() {
        Some(group_id) => write_group(
            root,
            &Group {
                version: 1,
                group_id,
                notes,
            },
        ),
        None => {
            document.notes = notes;
            write_document(root, document)
        }
    }
}
