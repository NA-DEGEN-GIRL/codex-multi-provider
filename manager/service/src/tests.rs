use super::*;
use tempfile::tempdir;

fn note_args() -> Value {
    json!({"task":{"host_id":"local","thread_id":uuid::Uuid::new_v4()},"note_id":uuid::Uuid::new_v4(),"revision":0,"title":"버그 체크","kind":"checklist","body":"","items":[{"id":uuid::Uuid::new_v4(),"text":"프로필을 바꿔 확인","done":false}]})
}

#[test]
fn retirement_requires_known_idle_management_state_not_closed_apps() {
    let mut response = json!({"ok":true,"result":{"profiles":[{"status":"running"}]}});
    assert!(can_stop(&response));
    response["result"]["local_launches"] = json!({"active":1});
    assert!(!can_stop(&response));
    response["result"]["local_launches"] = json!({"active":0});
    assert!(can_stop(&response));
    for phase in ["opening", "waiting", ""] {
        response["result"]["profile_restarts"] = json!({"p":{"phase":phase}});
        assert!(!can_stop(&response));
    }
    for phase in ["complete", "attention", "superseded"] {
        response["result"]["profile_restarts"] = json!({"p":{"phase":phase}});
        assert!(can_stop(&response));
    }
    response["result"]["profile_restarts"] = json!([]);
    assert!(!can_stop(&response));
    response["result"]["profile_restarts"] = json!({});
    for key in ["updates", "startup_updates", "profile_warmup"] {
        for active in [json!(true), json!(null), json!("false")] {
            response["result"][key] = json!({"worker_active":active});
            assert!(!can_stop(&response));
        }
        response["result"][key] = json!({"worker_active":false});
        assert!(can_stop(&response));
    }
}

fn note_fork_map(root: &std::path::Path, mapping: Value) {
    let path = root.join("work/control-center/note-forks.json");
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, mapping.to_string()).unwrap();
}

fn note_list(root: &std::path::Path, args: &Value) -> Value {
    notes::execute(root, "notes.list", args).unwrap()
}

fn note_document_path(root: &std::path::Path, args: &Value) -> PathBuf {
    std::fs::read_dir(root.join("work/control-center/notes"))
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            let document: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
            document["task"] == args["task"]
        })
        .unwrap()
}

fn legacy_note_group(root: &std::path::Path, args: &Value) -> (PathBuf, PathBuf) {
    notes::execute(root, "notes.save", args).unwrap();
    let path = note_document_path(root, args);
    let mut document: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    let group = uuid::Uuid::new_v4().to_string();
    let group_directory = root.join("work/control-center/note-groups");
    std::fs::create_dir_all(&group_directory).unwrap();
    let group_path = group_directory.join(format!("{group}.json"));
    let data = json!({"version":1,"group_id":group,"notes":document["notes"]});
    std::fs::write(&group_path, data.to_string()).unwrap();
    document["version"] = json!(2);
    document["group_id"] = json!(group);
    document["notes"] = json!([]);
    std::fs::write(&path, document.to_string()).unwrap();
    (path, group_path)
}

#[test]
fn forked_conversations_share_edits_additions_deletions_and_revisions() {
    let root = tempdir().unwrap();
    let mut parent = note_args();
    let mut child = note_args();
    parent["body"] = json!("parent body");
    parent["items"][0]["done"] = json!(true);
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    let original = note_list(root.path(), &parent);
    note_fork_map(
        root.path(),
        json!({child["task"]["thread_id"].as_str().unwrap(): parent["task"]["thread_id"]}),
    );
    let inherited = note_list(root.path(), &child);
    assert_eq!(inherited["notes"], original["notes"]);
    assert_eq!(inherited["shared"], true);
    assert_eq!(note_list(root.path(), &parent)["shared"], true);

    child["note_id"] = parent["note_id"].clone();
    child["revision"] = json!(1);
    child["body"] = json!("child edit");
    child["items"] = parent["items"].clone();
    child["items"][0]["text"] = json!("edited in child");
    child["items"][0]["done"] = json!(false);
    let edited = notes::execute(root.path(), "notes.save", &child).unwrap();
    assert_eq!(note_list(root.path(), &parent)["notes"][0], edited["note"]);
    assert_eq!(edited["note"]["revision"], 2);
    assert_eq!(edited["note"]["items"][0]["id"], parent["items"][0]["id"]);

    parent["revision"] = json!(1);
    let conflict = notes::execute(root.path(), "notes.save", &parent).unwrap();
    assert_eq!(conflict["state"], "conflict");
    assert_eq!(conflict["current"], edited["note"]);
    parent["revision"] = json!(2);
    parent["body"] = json!("parent changed");
    let edited = notes::execute(root.path(), "notes.save", &parent).unwrap();
    assert_eq!(note_list(root.path(), &child)["notes"][0], edited["note"]);
    let conflict = notes::execute(root.path(), "notes.save", &child).unwrap();
    assert_eq!(conflict["state"], "conflict");
    assert_eq!(conflict["current"], edited["note"]);

    // New notes from either member are immediately visible to the other.
    for source in [&child, &parent] {
        let mut added = note_args();
        added["task"] = source["task"].clone();
        notes::execute(root.path(), "notes.save", &added).unwrap();
        assert_eq!(
            note_list(root.path(), &parent)["notes"],
            note_list(root.path(), &child)["notes"]
        );
    }
    assert_eq!(
        note_list(root.path(), &parent)["notes"]
            .as_array()
            .unwrap()
            .len(),
        3
    );

    // Both directions of delete and restore share one revision history.
    for (source, other, revision) in [(&child, &parent, 3), (&parent, &child, 5)] {
        let mut mutation = source.clone();
        mutation["revision"] = json!(revision);
        let deleted = notes::execute(root.path(), "notes.delete", &mutation).unwrap();
        assert_eq!(deleted["note"]["deleted"], true);
        assert_eq!(note_list(root.path(), other)["notes"][0], deleted["note"]);
        mutation["task"] = other["task"].clone();
        mutation["revision"] = json!(revision + 1);
        let restored = notes::execute(root.path(), "notes.restore", &mutation).unwrap();
        assert_eq!(restored["note"]["deleted"], false);
        assert_eq!(restored["note"]["body"], "parent changed");
        assert_eq!(note_list(root.path(), source)["notes"][0], restored["note"]);
    }
}

#[test]
fn explicit_note_fork_detaches_current_task_with_fresh_ids_and_is_idempotent() {
    let root = tempdir().unwrap();
    let mut parent = note_args();
    parent["body"] = json!("keep this body");
    parent["items"][0]["done"] = json!(true);
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    let mut deleted = note_args();
    deleted["task"] = parent["task"].clone();
    notes::execute(root.path(), "notes.save", &deleted).unwrap();
    deleted["revision"] = json!(1);
    notes::execute(root.path(), "notes.delete", &deleted).unwrap();
    let child = note_args();
    note_fork_map(
        root.path(),
        json!({child["task"]["thread_id"].as_str().unwrap(): parent["task"]["thread_id"]}),
    );
    let before = note_list(root.path(), &child);
    let parent_before = note_list(root.path(), &parent);
    let split = notes::execute(root.path(), "notes.fork", &child).unwrap();
    assert_eq!(split["state"], "forked");
    assert_eq!(split["shared"], false);
    assert_eq!(split["task"], child["task"]);
    assert_eq!(split["notes"].as_array().unwrap().len(), 2);
    for (copy, original) in split["notes"]
        .as_array()
        .unwrap()
        .iter()
        .zip(before["notes"].as_array().unwrap())
    {
        assert_ne!(copy["id"], original["id"]);
        assert!(uuid::Uuid::parse_str(copy["id"].as_str().unwrap()).is_ok());
        assert_eq!(copy["revision"], 0);
        for key in ["title", "kind", "body", "deleted", "updated_at"] {
            assert_eq!(copy[key], original[key]);
        }
        for (item, old_item) in copy["items"]
            .as_array()
            .unwrap()
            .iter()
            .zip(original["items"].as_array().unwrap())
        {
            assert_ne!(item["id"], old_item["id"]);
            assert!(uuid::Uuid::parse_str(item["id"].as_str().unwrap()).is_ok());
            assert_eq!(item["text"], old_item["text"]);
            assert_eq!(item["done"], old_item["done"]);
        }
    }
    assert_eq!(note_list(root.path(), &parent), parent_before);
    assert_eq!(note_list(root.path(), &child)["notes"], split["notes"]);
    assert_eq!(note_list(root.path(), &child)["shared"], false);
    let repeated = notes::execute(root.path(), "notes.fork", &child).unwrap();
    assert_eq!(repeated["notes"], split["notes"]);
    assert_eq!(repeated["shared"], false);

    let mut edit = child.clone();
    edit["note_id"] = split["notes"][0]["id"].clone();
    edit["body"] = json!("detached edit");
    notes::execute(root.path(), "notes.save", &edit).unwrap();
    assert_eq!(note_list(root.path(), &parent), parent_before);
    let detached = note_list(root.path(), &child);
    parent["revision"] = json!(1);
    parent["body"] = json!("source edit");
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    assert_eq!(note_list(root.path(), &child), detached);
}

#[test]
fn splitting_a_standalone_document_retains_note_and_checklist_ids() {
    let root = tempdir().unwrap();
    let args = note_args();
    notes::execute(root.path(), "notes.save", &args).unwrap();
    let before = note_list(root.path(), &args);
    for _ in 0..2 {
        let split = notes::execute(root.path(), "notes.fork", &args).unwrap();
        assert_eq!(split["shared"], false);
        assert_eq!(split["notes"], before["notes"]);
        assert_eq!(note_list(root.path(), &args), before);
    }
}

#[test]
fn nested_forks_share_groups_and_new_descendants_follow_detached_parent() {
    let root = tempdir().unwrap();
    let mut parent = note_args();
    parent["body"] = json!("original");
    let child = note_args();
    let grandchild = note_args();
    let sibling = note_args();
    let later_child = note_args();
    let later_grandchild = note_args();
    let parent_id = parent["task"]["thread_id"].as_str().unwrap();
    let child_id = child["task"]["thread_id"].as_str().unwrap();
    let grandchild_id = grandchild["task"]["thread_id"].as_str().unwrap();
    let sibling_id = sibling["task"]["thread_id"].as_str().unwrap();
    let later_child_id = later_child["task"]["thread_id"].as_str().unwrap();
    note_fork_map(
        root.path(),
        json!({child_id: parent_id, grandchild_id: child_id, sibling_id: parent_id}),
    );
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    let leaf = note_list(root.path(), &grandchild);
    for task in [&parent, &child, &sibling] {
        let list = note_list(root.path(), task);
        assert_eq!(list["notes"], leaf["notes"]);
        assert_eq!(list["shared"], true);
    }
    let split = notes::execute(root.path(), "notes.fork", &child).unwrap();
    assert_eq!(split["shared"], false);
    assert_ne!(split["notes"][0]["id"], leaf["notes"][0]["id"]);
    // Already joined descendants and siblings stay in the original group.
    assert_eq!(note_list(root.path(), &grandchild), leaf);
    note_fork_map(
        root.path(),
        json!({child_id: parent_id, grandchild_id: child_id, sibling_id: parent_id,
        later_child_id: child_id, later_grandchild["task"]["thread_id"].as_str().unwrap(): later_child_id}),
    );
    let later = note_list(root.path(), &later_grandchild);
    assert_eq!(later["notes"], split["notes"]);
    assert_eq!(later["shared"], true);
    assert_eq!(
        note_list(root.path(), &later_child)["notes"],
        split["notes"]
    );
    assert_eq!(note_list(root.path(), &child)["shared"], true);

    let mut edit = later_grandchild.clone();
    edit["note_id"] = split["notes"][0]["id"].clone();
    edit["body"] = json!("detached family edit");
    let saved = notes::execute(root.path(), "notes.save", &edit).unwrap();
    assert_eq!(note_list(root.path(), &child)["notes"][0], saved["note"]);
    assert_eq!(
        note_list(root.path(), &later_child)["notes"][0],
        saved["note"]
    );
    for task in [&parent, &sibling, &grandchild] {
        assert_eq!(note_list(root.path(), task)["notes"], leaf["notes"]);
    }
    parent["revision"] = json!(1);
    parent["body"] = json!("original family edit");
    let saved = notes::execute(root.path(), "notes.save", &parent).unwrap();
    for task in [&sibling, &grandchild] {
        assert_eq!(note_list(root.path(), task)["notes"][0], saved["note"]);
    }
    assert_eq!(
        note_list(root.path(), &child)["notes"][0]["body"],
        "detached family edit"
    );
}

#[test]
fn splitting_parent_does_not_move_existing_children() {
    let root = tempdir().unwrap();
    let parent = note_args();
    let mut child = note_args();
    let sibling = note_args();
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    note_fork_map(
        root.path(),
        json!({child["task"]["thread_id"].as_str().unwrap(): parent["task"]["thread_id"],
        sibling["task"]["thread_id"].as_str().unwrap(): parent["task"]["thread_id"]}),
    );
    let before = note_list(root.path(), &child);
    assert_eq!(note_list(root.path(), &sibling)["notes"], before["notes"]);
    let split = notes::execute(root.path(), "notes.fork", &parent).unwrap();
    assert_eq!(split["shared"], false);
    assert_eq!(note_list(root.path(), &child), before);
    child["note_id"] = before["notes"][0]["id"].clone();
    child["revision"] = json!(1);
    child["body"] = json!("children still share");
    notes::execute(root.path(), "notes.save", &child).unwrap();
    assert_eq!(
        note_list(root.path(), &sibling)["notes"],
        note_list(root.path(), &child)["notes"]
    );
    assert_eq!(note_list(root.path(), &parent)["notes"], split["notes"]);
}

#[test]
fn preexisting_standalone_child_is_preserved_when_fork_metadata_appears() {
    let root = tempdir().unwrap();
    let parent = note_args();
    let child = note_args();
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    notes::execute(root.path(), "notes.save", &child).unwrap();
    let before = note_list(root.path(), &child);
    let path = note_document_path(root.path(), &child);
    let bytes = std::fs::read(&path).unwrap();
    note_fork_map(
        root.path(),
        json!({child["task"]["thread_id"].as_str().unwrap(): parent["task"]["thread_id"]}),
    );
    assert_eq!(note_list(root.path(), &child), before);
    assert_eq!(std::fs::read(path).unwrap(), bytes);
    assert_eq!(note_list(root.path(), &parent)["shared"], false);
}

#[test]
fn empty_forks_share_future_changes_in_both_directions() {
    for child_writes_first in [false, true] {
        let root = tempdir().unwrap();
        let parent = note_args();
        let child = note_args();
        let grandchild = note_args();
        note_fork_map(
            root.path(),
            json!({child["task"]["thread_id"].as_str().unwrap(): parent["task"]["thread_id"],
            grandchild["task"]["thread_id"].as_str().unwrap(): child["task"]["thread_id"]}),
        );
        let empty = note_list(root.path(), &grandchild);
        assert_eq!(empty["notes"], json!([]));
        assert_eq!(empty["shared"], true);
        let first = if child_writes_first {
            &grandchild
        } else {
            &parent
        };
        notes::execute(root.path(), "notes.save", first).unwrap();
        let saved = note_list(root.path(), first);
        for task in [&parent, &child, &grandchild] {
            assert_eq!(note_list(root.path(), task)["notes"], saved["notes"]);
            assert_eq!(note_list(root.path(), task)["shared"], true);
        }
    }
}

#[test]
fn detaching_empty_fork_keeps_later_parent_notes_out() {
    let root = tempdir().unwrap();
    let parent = note_args();
    let child = note_args();
    note_fork_map(
        root.path(),
        json!({child["task"]["thread_id"].as_str().unwrap(): parent["task"]["thread_id"]}),
    );
    let split = notes::execute(root.path(), "notes.fork", &child).unwrap();
    assert_eq!(split["shared"], false);
    assert_eq!(split["notes"], json!([]));
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    assert_eq!(note_list(root.path(), &child)["notes"], json!([]));
    notes::execute(root.path(), "notes.save", &child).unwrap();
    assert_eq!(
        note_list(root.path(), &parent)["notes"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert_eq!(
        note_list(root.path(), &parent)["notes"][0]["id"],
        parent["note_id"]
    );
}

#[test]
fn remote_hosts_do_not_inherit_local_fork_relationships() {
    let root = tempdir().unwrap();
    let parent = note_args();
    let child = note_args();
    note_fork_map(
        root.path(),
        json!({child["task"]["thread_id"].as_str().unwrap(): parent["task"]["thread_id"]}),
    );
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    assert_eq!(
        note_list(root.path(), &child)["notes"][0]["id"],
        parent["note_id"]
    );
    let local = note_list(root.path(), &child);
    let mut remote_parent = parent.clone();
    remote_parent["task"]["host_id"] = json!("ssh:other-host");
    notes::execute(root.path(), "notes.save", &remote_parent).unwrap();
    let mut remote_child = child.clone();
    remote_child["task"]["host_id"] = json!("ssh:other-host");
    assert_eq!(note_list(root.path(), &remote_child)["notes"], json!([]));
    assert_eq!(note_list(root.path(), &remote_child)["shared"], false);
    notes::execute(root.path(), "notes.save", &remote_child).unwrap();
    assert_eq!(
        note_list(root.path(), &remote_parent)["notes"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    assert_eq!(note_list(root.path(), &child), local);
}

#[test]
fn fork_cycles_fail_without_creating_documents_or_groups() {
    let root = tempdir().unwrap();
    let a = note_args();
    let b = note_args();
    note_fork_map(
        root.path(),
        json!({a["task"]["thread_id"].as_str().unwrap(): b["task"]["thread_id"],
        b["task"]["thread_id"].as_str().unwrap(): a["task"]["thread_id"]}),
    );
    for command in ["notes.list", "notes.save", "notes.fork"] {
        assert!(notes::execute(root.path(), command, &a).is_err());
        assert!(!root.path().join("work/control-center/notes").exists());
        assert!(!root.path().join("work/control-center/note-groups").exists());
    }
}

#[test]
fn legacy_shared_group_is_reused_and_split_preserves_other_members() {
    let root = tempdir().unwrap();
    let parent = note_args();
    let (path, group_path) = legacy_note_group(root.path(), &parent);
    let parent_bytes = std::fs::read(&path).unwrap();
    let group_bytes = std::fs::read(&group_path).unwrap();
    let data: Value = serde_json::from_slice(&group_bytes).unwrap();
    let child = note_args();
    note_fork_map(
        root.path(),
        json!({child["task"]["thread_id"].as_str().unwrap(): parent["task"]["thread_id"]}),
    );
    let inherited = note_list(root.path(), &child);
    assert_eq!(inherited["notes"], data["notes"]);
    assert_eq!(inherited["shared"], true);
    assert_eq!(std::fs::read(&path).unwrap(), parent_bytes);
    assert_eq!(std::fs::read(&group_path).unwrap(), group_bytes);
    let split = notes::execute(root.path(), "notes.fork", &parent).unwrap();
    assert_eq!(split["shared"], false);
    assert_ne!(split["notes"][0]["id"], data["notes"][0]["id"]);
    assert_eq!(note_list(root.path(), &parent)["shared"], false);
    assert_eq!(note_list(root.path(), &child), inherited);
    assert_eq!(std::fs::read(&group_path).unwrap(), group_bytes);
}

#[test]
fn corrupt_or_mismatched_groups_cannot_overwrite_documents_or_groups() {
    for damage in [
        "invalid json",
        "wrong group",
        "unsupported version",
        "missing",
    ] {
        let root = tempdir().unwrap();
        let parent = note_args();
        let (document_path, group_path) = legacy_note_group(root.path(), &parent);
        let mut data: Value = serde_json::from_slice(&std::fs::read(&group_path).unwrap()).unwrap();
        match damage {
            "invalid json" => std::fs::write(&group_path, b"broken group").unwrap(),
            "wrong group" => {
                data["group_id"] = json!(uuid::Uuid::new_v4());
                std::fs::write(&group_path, data.to_string()).unwrap();
            }
            "unsupported version" => {
                data["version"] = json!(999);
                std::fs::write(&group_path, data.to_string()).unwrap();
            }
            "missing" => std::fs::remove_file(&group_path).unwrap(),
            _ => unreachable!(),
        }
        let document_bytes = std::fs::read(&document_path).unwrap();
        let group_bytes = std::fs::read(&group_path).ok();
        let child = note_args();
        note_fork_map(
            root.path(),
            json!({child["task"]["thread_id"].as_str().unwrap(): parent["task"]["thread_id"]}),
        );
        for command in ["notes.list", "notes.save", "notes.fork"] {
            for task in [&parent, &child] {
                assert!(
                    notes::execute(root.path(), command, task).is_err(),
                    "{damage}: {command}"
                );
                assert_eq!(std::fs::read(&document_path).unwrap(), document_bytes);
                assert_eq!(std::fs::read(&group_path).ok(), group_bytes);
                assert_eq!(
                    std::fs::read_dir(root.path().join("work/control-center/notes"))
                        .unwrap()
                        .count(),
                    1
                );
            }
        }
    }
}

#[test]
fn invalid_group_id_cannot_traverse_to_or_overwrite_another_file() {
    let root = tempdir().unwrap();
    let mut parent = note_args();
    let (document_path, _) = legacy_note_group(root.path(), &parent);
    let mut document: Value =
        serde_json::from_slice(&std::fs::read(&document_path).unwrap()).unwrap();
    document["group_id"] = json!("../outside-group");
    std::fs::write(&document_path, document.to_string()).unwrap();
    let document_bytes = std::fs::read(&document_path).unwrap();
    let outside = root.path().join("work/control-center/outside-group.json");
    // A valid-looking group outside the group directory must never be followed.
    let sentinel = json!({"version":1,"group_id":"../outside-group","notes":[]}).to_string();
    std::fs::write(&outside, &sentinel).unwrap();
    parent["revision"] = json!(0);
    for command in ["notes.list", "notes.save", "notes.fork"] {
        assert!(notes::execute(root.path(), command, &parent).is_err());
        assert_eq!(std::fs::read(&document_path).unwrap(), document_bytes);
        assert_eq!(std::fs::read_to_string(&outside).unwrap(), sentinel);
    }
}

#[test]
fn corrupted_parent_does_not_freeze_an_empty_child() {
    let root = tempdir().unwrap();
    let parent = note_args();
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    let directory = root.path().join("work/control-center/notes");
    let path = std::fs::read_dir(&directory)
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    std::fs::write(&path, b"incomplete").unwrap();
    let child = note_args();
    note_fork_map(
        root.path(),
        json!({child["task"]["thread_id"].as_str().unwrap(): parent["task"]["thread_id"]}),
    );
    assert!(notes::execute(root.path(), "notes.list", &child).is_err());
    assert_eq!(std::fs::read_dir(directory).unwrap().count(), 1);
    assert_eq!(std::fs::read(path).unwrap(), b"incomplete");
}

#[test]
fn checklist_persists_edits_completion_and_conflicts() {
    let root = tempdir().unwrap();
    let mut args = note_args();
    let saved = notes::execute(root.path(), "notes.save", &args).unwrap();
    assert_eq!(saved["note"]["revision"], 1);
    args["revision"] = json!(1);
    args["items"][0]["done"] = json!(true);
    args["items"][0]["text"] = json!("한국어 항목 수정 완료");
    notes::execute(root.path(), "notes.save", &args).unwrap();
    let conflict = notes::execute(root.path(), "notes.save", &args).unwrap();
    assert_eq!(conflict["state"], "conflict");
    assert_eq!(conflict["current"]["revision"], 2);
    let loaded = notes::execute(root.path(), "notes.list", &args).unwrap();
    assert_eq!(loaded["notes"][0]["items"][0]["done"], true);
    assert_eq!(
        loaded["notes"][0]["items"][0]["text"],
        "한국어 항목 수정 완료"
    );
    args["profile_id"] = json!(uuid::Uuid::new_v4()); // profile changes do not change a task's notes
    assert_eq!(
        notes::execute(root.path(), "notes.list", &args).unwrap(),
        loaded
    );
    args["task"]["host_id"] = json!("ssh:other-host");
    assert!(
        notes::execute(root.path(), "notes.list", &args).unwrap()["notes"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[test]
fn deleting_and_restoring_never_discards_body() {
    let root = tempdir().unwrap();
    let mut args = note_args();
    args["kind"] = json!("text");
    args["body"] = json!("기능 아이디어\n두 번째 줄");
    notes::execute(root.path(), "notes.save", &args).unwrap();
    args["revision"] = json!(1);
    let removed = notes::execute(root.path(), "notes.delete", &args).unwrap();
    assert_eq!(removed["note"]["deleted"], true);
    args["revision"] = json!(2);
    assert!(notes::execute(root.path(), "notes.save", &args).is_err());
    let restored = notes::execute(root.path(), "notes.restore", &args).unwrap();
    assert_eq!(restored["note"]["body"], args["body"]);
    assert_eq!(restored["note"]["deleted"], false);
}

#[test]
fn invalid_or_corrupt_notes_leave_existing_files_intact() {
    let root = tempdir().unwrap();
    let mut args = note_args();
    notes::execute(root.path(), "notes.save", &args).unwrap();
    let path = std::fs::read_dir(root.path().join("work/control-center/notes"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let original = std::fs::read(&path).unwrap();
    args["revision"] = json!(1);
    args["items"][0]["id"] = json!("../escape");
    assert!(notes::execute(root.path(), "notes.save", &args).is_err());
    assert_eq!(std::fs::read(&path).unwrap(), original);
    std::fs::write(&path, b"broken data").unwrap();
    assert!(notes::execute(root.path(), "notes.list", &args).is_err());
    assert_eq!(std::fs::read(&path).unwrap(), b"broken data");
}

#[tokio::test]
async fn frames_handle_fragmentation_and_limits() {
    use tokio::io::AsyncWriteExt;
    let (mut tx, rx) = tokio::io::duplex(128);
    let write = tokio::spawn(async move {
        tx.write_all(b"abc").await.unwrap();
        tokio::task::yield_now().await;
        tx.write_all(b"\ndef\n123456789\n").await.unwrap();
    });
    let mut rx = tokio::io::BufReader::new(rx);
    assert_eq!(protocol::frame(&mut rx, 4).await.unwrap().unwrap(), b"abc");
    assert_eq!(protocol::frame(&mut rx, 4).await.unwrap().unwrap(), b"def");
    assert!(protocol::frame(&mut rx, 4).await.is_err());
    write.await.unwrap();
}

#[test]
fn process_identity_and_generation_guards() {
    let own = processes::identity(std::process::id()).unwrap();
    assert_eq!(own.process_id, std::process::id());
    assert!(own.process_created > 0);
    let root = tempdir().unwrap();
    let id = uuid::Uuid::new_v4();
    let generation = uuid::Uuid::new_v4();
    let path = root.path().join("work/control-center/state.json");
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path,serde_json::to_vec(&json!({"profiles":[{"id":id,"generation":generation,"process_id":own.process_id,"process_created":own.process_created,"executable_path":own.executable_path}]})).unwrap()).unwrap();
    assert!(
        processes::execute(
            root.path(),
            "process.stop",
            &json!({"profile_id":id,"generation":uuid::Uuid::new_v4()})
        )
        .is_err()
    );
    assert!(
        processes::execute(
            root.path(),
            "process.stop",
            &json!({"profile_id":id,"generation":generation})
        )
        .is_err()
    ); // not a private desktop path
    assert!(processes::identity(std::process::id()).is_some());
}

#[cfg(feature = "process-fixture")]
#[test]
fn owned_process_launch_and_descendant_stop_preserve_unrelated_process() {
    struct Exit(std::path::PathBuf);
    impl Drop for Exit {
        fn drop(&mut self) {
            let _ = std::fs::write(self.0.join("exit"), b"done");
        }
    }
    let root = tempdir().unwrap();
    let _exit = Exit(root.path().into());
    let fixture =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target/debug/process-fixture.exe");
    assert!(fixture.is_file(), "build process-fixture first");
    let executable = root
        .path()
        .join("artifacts/managed-desktop/fixture/process.exe");
    std::fs::create_dir_all(executable.parent().unwrap()).unwrap();
    std::fs::copy(fixture, &executable).unwrap();
    let id = uuid::Uuid::new_v4().to_string();
    let generation = uuid::Uuid::new_v4().to_string();
    let directory = root.path().join("work/control-center/profiles").join(&id);
    let ui = directory.join("ui");
    let home = directory.join("codex");
    std::fs::create_dir_all(&ui).unwrap();
    std::fs::create_dir_all(&home).unwrap();
    let mut profile = json!({"id":id,"generation":generation,"home":home,"ui_home":ui});
    let state = root.path().join("work/control-center/state.json");
    let write = |profile: &Value| {
        std::fs::write(
            &state,
            serde_json::to_vec(&json!({"profiles":[profile]})).unwrap(),
        )
        .unwrap()
    };
    write(&profile);
    let mut env: std::collections::BTreeMap<String, String> = std::env::vars().collect();
    env.insert("CODEX_HOME".into(), home.to_string_lossy().into());
    env.insert(
        "CODEX_PROCESS_FIXTURE_DIRECTORY".into(),
        root.path().to_string_lossy().into(),
    );
    let args = json!({"profile_id":id,"generation":generation,"environment":env,"executable":executable,"embed":true});
    let started = processes::execute(root.path(), "process.launch", &args).unwrap();
    for key in ["process_id", "process_created", "executable_path"] {
        profile[key] = started[key].clone();
    }
    write(&profile);
    assert!(
        processes::execute(root.path(), "process.launch", &args).is_err(),
        "duplicate launch refused"
    );
    let deadline = Instant::now() + Duration::from_secs(3);
    while !root.path().join("child.pid").exists() && Instant::now() < deadline {
        std::thread::sleep(Duration::from_millis(10));
    }
    let child: u32 = std::fs::read_to_string(root.path().join("child.pid"))
        .unwrap()
        .parse()
        .unwrap();
    assert!(processes::identity(child).is_some());
    let expected = json!({"profile_id":id,"generation":generation});
    profile["ui_home"] = json!(directory.join("wrong-ui"));
    write(&profile);
    assert!(processes::execute(root.path(), "process.stop", &expected).is_err());
    assert!(processes::identity(child).is_some());
    profile["ui_home"] = json!(ui);
    write(&profile);
    let stopped = processes::execute(root.path(), "process.stop", &expected).unwrap();
    assert_eq!(stopped["state"], "stopped");
    assert!(processes::identity(child).is_none());
    assert!(processes::identity(started["process_id"].as_u64().unwrap() as u32).is_none());
    assert!(processes::identity(std::process::id()).is_some());
}

fn service(root: PathBuf) -> Arc<Service> {
    Arc::new(Service {
        backend: backend::Backend::new(
            root.clone(),
            root.clone(),
            "test-pipe".into(),
            "test-token".into(),
        ),
        root,
        token: "test-token".into(),
        revision: "test".into(),
        notes: Arc::default(),
        gates: Mutex::default(),
        operations: std::sync::Mutex::default(),
        clients: AtomicUsize::new(0),
        stopping: AtomicBool::new(false),
        retire: Mutex::new(()),
        stopped: Notify::new(),
        admission: tokio::sync::RwLock::new(()),
    })
}
fn request(command: &str, args: Value) -> Request {
    Request {
        id: uuid::Uuid::new_v4().to_string(),
        command: command.into(),
        args,
        version: protocol::VERSION,
    }
}

#[tokio::test]
async fn first_note_access_resolves_native_fork_before_sharing() {
    let root = tempdir().unwrap();
    let parent = note_args();
    let child = note_args();
    let ids = json!({"parent":parent["task"]["thread_id"],"child":child["task"]["thread_id"]});
    std::fs::write(root.path().join("fixture.json"), ids.to_string()).unwrap();
    std::fs::create_dir(root.path().join("scripts")).unwrap();
    let scripts = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../scripts");
    let import = format!(
        "import sys\nsys.path.insert(0, {})\n",
        serde_json::to_string(&scripts).unwrap()
    );
    std::fs::write(root.path().join("scripts/control_center.py"), import + r#"
import json, sqlite3
from pathlib import Path
from manager_core.note_forks import refresh
root=Path.cwd(); home=root/'fixture-home'; home.mkdir()
ids=json.loads((root/'fixture.json').read_text())
db=sqlite3.connect(home/'state_5.sqlite')
db.execute('CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, source TEXT)')
for child,parent in ((ids['parent'],None),(ids['child'],ids['parent'])):
    rollout=home/(child+'.jsonl')
    rollout.write_text(json.dumps({'type':'session_meta','payload':{'id':child,'forked_from_id':parent,'source':'vscode'}})+'\n')
    db.execute('INSERT INTO threads VALUES(?,?,?)',(child,str(rollout),'vscode'))
db.commit(); db.close()
state={'sources':[{'host_id':'local','home':str(home)}],'profiles':[]}
for raw in sys.stdin:
    request=json.loads(raw)
    try:
        if request['command']=='notes.refresh_forks':
            refresh(root,state,request['args']['task']['thread_id'])
            result={'refreshed':True}
        elif request['command']=='state': result={'profiles':[]}
        else: raise RuntimeError('Unexpected fixture command')
        reply={'id':request['id'],'ok':True,'result':result}
    except Exception:
        reply={'id':request['id'],'ok':False,'error':{'code':'metadata_unavailable'}}
    print(json.dumps(reply),flush=True)
"#).unwrap();
    let service = service(root.path().into());
    assert_eq!(
        service
            .dispatch(request("notes.save", parent.clone()))
            .await["ok"],
        true
    );
    let shared = service.dispatch(request("notes.list", child.clone())).await;
    assert_eq!(shared["ok"], true);
    assert_eq!(shared["result"]["notes"][0]["title"], parent["title"]);
    assert_eq!(shared["result"]["notes"][0]["id"], parent["note_id"]);
    assert_eq!(shared["result"]["shared"], true);
    let repeated = service.dispatch(request("notes.list", child)).await;
    assert_eq!(shared["result"], repeated["result"]);
    let mut parent_edit = parent.clone();
    parent_edit["revision"] = json!(1);
    parent_edit["body"] = json!("concurrent parent edit");
    let mut child_edit = parent_edit.clone();
    child_edit["task"] = shared["result"]["task"].clone();
    child_edit["body"] = json!("concurrent child edit");
    let (parent_save, child_save) = tokio::join!(
        service.dispatch(request("notes.save", parent_edit)),
        service.dispatch(request("notes.save", child_edit))
    );
    assert_eq!(parent_save["ok"], true);
    assert_eq!(child_save["ok"], true);
    let (saved, conflict) = if parent_save["result"]["state"] == "saved" {
        (&parent_save["result"], &child_save["result"])
    } else {
        (&child_save["result"], &parent_save["result"])
    };
    assert_eq!(saved["state"], "saved");
    assert_eq!(conflict["state"], "conflict");
    assert_eq!(saved["note"]["revision"], 2);
    assert_eq!(conflict["current"], saved["note"]);
    for task in [&parent["task"], &shared["result"]["task"]] {
        let loaded = service
            .dispatch(request("notes.list", json!({"task":task})))
            .await;
        assert_eq!(loaded["result"]["notes"][0], saved["note"]);
        assert_eq!(loaded["result"]["shared"], true);
    }
    // A thread not yet indexed cannot create an empty document or lose inheritance.
    let unknown = note_args();
    assert_eq!(
        service.dispatch(request("notes.save", unknown)).await["ok"],
        false
    );
    assert_eq!(
        std::fs::read_dir(root.path().join("work/control-center/notes"))
            .unwrap()
            .count(),
        2
    );
    assert_eq!(
        service
            .dispatch(request("supervisor.retire", json!({})))
            .await["ok"],
        true
    );
}

#[tokio::test]
async fn slow_profile_does_not_block_notes_status_or_other_profile() {
    let root = tempdir().unwrap();
    std::fs::create_dir(root.path().join("scripts")).unwrap();
    std::fs::write(
        root.path().join("scripts/control_center.py"),
        r#"
import sys,json,time,threading,concurrent.futures
lock=threading.Lock()
def run(r):
    time.sleep(r['args'].get('delay',0))
    value={'id':r['id'],'ok':True,'result':{'profiles':[],'echo':r['args']}}
    with lock: print(json.dumps(value),flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    for line in sys.stdin: pool.submit(run,json.loads(line))
"#,
    )
    .unwrap();
    let service = service(root.path().into());
    let slow = tokio::spawn({
        let service = service.clone();
        async move {
            service
                .dispatch(request(
                    "profile.show",
                    json!({"profile_id":"a","delay":1.5}),
                ))
                .await
        }
    });
    tokio::time::sleep(Duration::from_millis(250)).await;
    let now = Instant::now();
    let saved = service.dispatch(request("notes.save", note_args())).await;
    assert_eq!(saved["ok"], true);
    let fast = service
        .dispatch(request("profile.show", json!({"profile_id":"b"})))
        .await;
    assert_eq!(fast["ok"], true);
    assert_eq!(
        service
            .dispatch(request("supervisor.status", json!({})))
            .await["result"]["engine"],
        "rust"
    );
    assert!(now.elapsed() < Duration::from_millis(850));
    assert!(!slow.is_finished());
    assert_eq!(
        service
            .dispatch(request("supervisor.retire", json!({})))
            .await["ok"],
        false
    );
    assert_eq!(slow.await.unwrap()["ok"], true);
    for _ in 0..10 {
        if service.backend.reconnect().await {
            return;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    panic!("isolated backend failed to retire");
}

#[tokio::test]
async fn pipe_acl_and_broker_auth_and_protocol_guard() {
    let root = tempdir().unwrap();
    let name = windows::pipe_name(root.path()).unwrap();
    let server = windows::server(&name, true).unwrap();
    assert!(windows::server(&name, true).is_err());
    drop(server);
    let service = service(root.path().into());
    assert_eq!(
        service.dispatch(request("process.stop", json!({}))).await["error"]["code"],
        "unauthorized"
    );
    let mut wrong = request("notes.list", note_args());
    wrong.version = 1;
    assert_eq!(
        service.dispatch(wrong).await["error"]["code"],
        "protocol_version"
    );
}
