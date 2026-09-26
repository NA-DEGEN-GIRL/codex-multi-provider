use super::*;
use tempfile::tempdir;

fn note_args() -> Value {
    json!({"task":{"host_id":"local","thread_id":uuid::Uuid::new_v4()},"note_id":uuid::Uuid::new_v4(),"revision":0,"title":"버그 체크","kind":"checklist","body":"","items":[{"id":uuid::Uuid::new_v4(),"text":"프로필을 바꿔 확인","done":false}]})
}

#[test]
fn remote_note_aliases_share_existing_revisions_and_preserve_independent_documents() {
    let root = tempdir().unwrap();
    let mut original = note_args();
    original["task"]["host_id"] = json!("ssh:fixture");
    let saved = notes::execute(root.path(), "notes.save", &original).unwrap();
    let mut canonical = original.clone();
    canonical["task"]["thread_id"] = json!(uuid::Uuid::new_v4().to_string());
    let key = format!(
        "ssh:fixture\0{}",
        canonical["task"]["thread_id"].as_str().unwrap()
    );
    let alias_path = root.path().join("work/control-center/note-aliases.json");
    std::fs::write(
        &alias_path,
        serde_json::to_vec(&json!({&key: original["task"]["thread_id"]})).unwrap(),
    )
    .unwrap();
    assert!(!notes::needs_fork_refresh(root.path(), &canonical));
    assert_eq!(
        notes::execute(root.path(), "notes.list", &canonical).unwrap()["notes"][0],
        saved["note"]
    );
    canonical["revision"] = json!(1);
    canonical["body"] = json!("edited through canonical task");
    let edited = notes::execute(root.path(), "notes.save", &canonical).unwrap();
    assert_eq!(
        notes::execute(root.path(), "notes.list", &original).unwrap()["notes"][0],
        edited["note"]
    );
    assert_eq!(
        notes::execute(root.path(), "notes.save", &original).unwrap()["state"],
        "conflict"
    );
    // An existing independent canonical document always wins over an old alias.
    std::fs::write(&alias_path, b"{}").unwrap();
    canonical["revision"] = json!(0);
    canonical["body"] = json!("independent canonical note");
    let independent = notes::execute(root.path(), "notes.save", &canonical).unwrap();
    std::fs::write(
        &alias_path,
        serde_json::to_vec(&json!({key: original["task"]["thread_id"]})).unwrap(),
    )
    .unwrap();
    assert_eq!(
        notes::execute(root.path(), "notes.list", &canonical).unwrap()["notes"][0],
        independent["note"]
    );
    let mut unresolved = canonical.clone();
    unresolved["task"]["thread_id"] = json!(uuid::Uuid::new_v4().to_string());
    let unresolved_key = format!(
        "ssh:fixture\0{}",
        unresolved["task"]["thread_id"].as_str().unwrap()
    );
    std::fs::write(
        &alias_path,
        serde_json::to_vec(&json!({unresolved_key: "../invalid"})).unwrap(),
    )
    .unwrap();
    assert!(notes::execute(root.path(), "notes.list", &unresolved).is_err());
}

fn note_image(root: &std::path::Path) -> Value {
    use sha2::{Digest, Sha256};
    // Known 1x1 PNG; the shell separately tests real WPF decoding/encoding.
    let hex = "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082";
    let bytes: Vec<u8> = (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
        .collect();
    let id = format!("{:x}", Sha256::digest(&bytes));
    let directory = root.join("work/control-center/note-images");
    std::fs::create_dir_all(&directory).unwrap();
    std::fs::write(directory.join(format!("{id}.png")), bytes).unwrap();
    json!({"id":id,"width":1,"height":1})
}

#[test]
fn note_images_survive_old_clients_shared_forks_and_independent_removal() {
    let root = tempdir().unwrap();
    let image = note_image(root.path());
    let mut parent = note_args();
    parent["images"] = json!([image]);
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    let disk: Value =
        serde_json::from_slice(&std::fs::read(note_document_path(root.path(), &parent)).unwrap())
            .unwrap();
    assert_eq!(disk["version"], 3); // Old services fail closed instead of erasing images.
    assert_eq!(
        note_list(root.path(), &parent)["image_attachments_version"],
        1
    );
    parent.as_object_mut().unwrap().remove("images");
    parent["revision"] = json!(1);
    parent["body"] = json!("text-only older client edit");
    let saved = notes::execute(root.path(), "notes.save", &parent).unwrap();
    assert_eq!(saved["note"]["images"], json!([image]));
    let child = note_args();
    note_fork_map(
        root.path(),
        json!({child["task"]["thread_id"].as_str().unwrap():parent["task"]["thread_id"]}),
    );
    assert_eq!(
        note_list(root.path(), &child)["notes"][0]["images"],
        json!([image])
    );
    let group = std::fs::read_dir(root.path().join("work/control-center/note-groups"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let group_data: Value = serde_json::from_slice(&std::fs::read(group).unwrap()).unwrap();
    assert_eq!(group_data["version"], 2);
    let split = notes::execute(root.path(), "notes.fork", &child).unwrap();
    let copied = &split["notes"][0];
    assert_eq!(copied["images"], json!([image]));
    let mut change = json!({"task":child["task"],"note_id":copied["id"],"revision":0,
                           "title":"split","body":"","items":[],"images":[]});
    notes::execute(root.path(), "notes.save", &change).unwrap();
    assert_eq!(
        note_list(root.path(), &parent)["notes"][0]["images"],
        json!([image])
    );
    assert_eq!(
        note_list(root.path(), &child)["notes"][0]["images"],
        json!([])
    );
    change["revision"] = json!(1);
    change["images"] = json!([image]);
    notes::execute(root.path(), "notes.save", &change).unwrap();
    change["revision"] = json!(2);
    notes::execute(root.path(), "notes.delete", &change).unwrap();
    change["revision"] = json!(3);
    assert_eq!(
        notes::execute(root.path(), "notes.restore", &change).unwrap()["note"]["images"],
        json!([image])
    );
}

#[test]
fn invalid_note_images_never_overwrite_the_saved_note() {
    let root = tempdir().unwrap();
    let image = note_image(root.path());
    let mut args = note_args();
    notes::execute(root.path(), "notes.save", &args).unwrap();
    args["revision"] = json!(1);
    let before = note_list(root.path(), &args);
    for images in [
        json!([{"id":"../secret","width":1,"height":1}]),
        json!([{"id":"a".repeat(64),"width":1,"height":1}]),
        json!([{"id":image["id"],"width":2,"height":1}]),
        json!([{"id":image["id"],"width":40000001,"height":1}]),
        json!([image.clone(), image.clone()]),
        json!(vec![image.clone(); 25]),
        json!(null),
    ] {
        args["images"] = images;
        assert!(notes::execute(root.path(), "notes.save", &args).is_err());
        assert_eq!(note_list(root.path(), &args), before);
    }
    let path = root
        .path()
        .join("work/control-center/note-images")
        .join(format!("{}.png", image["id"].as_str().unwrap()));
    let mut bytes = std::fs::read(&path).unwrap();
    bytes.push(1);
    std::fs::write(path, bytes).unwrap();
    args["images"] = json!([image]);
    assert!(notes::execute(root.path(), "notes.save", &args).is_err());
    assert_eq!(note_list(root.path(), &args), before);
}

#[test]
fn retirement_requires_exited_profiles_and_known_idle_management_state() {
    let mut response =
        json!({"ok":true,"result":{"profiles":[{"status":"not_started","process_id":null}]}});
    assert!(can_stop(&response));
    for profile in [
        json!({"status":"running","process_id":123}),
        json!({"status":"running"}),
        json!({"status":"unknown"}),
        json!({}),
        json!({"status":"not_started","process_id":123}),
    ] {
        response["result"]["profiles"] = json!([profile]);
        assert!(!can_stop(&response));
    }
    response["result"]["profiles"] = json!([{"status":"not_started","process_id":null}]);
    response["result"]["view_instances"] = json!([{"status":"running","process_id":456}]);
    assert!(!can_stop(&response));
    response["result"]["view_instances"] = json!([]);
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

#[test]
fn explicit_shutdown_only_drains_remote_queue_after_profiles_and_launches_exit() {
    let mut response = json!({"ok":true,"result":{
        "profiles":[], "view_instances":[],
        "local_launches":{"active":0,"stopping":true},
        "remote_updates":{"worker_active":true,"items":[{"job":{"state":"waiting"}}]}
    }});
    assert!(!can_stop(&response)); // Passive UI close/update must preserve the queue.
    assert!(can_drain(&response)); // Explicit exit instead waits for adapter EOF.
    response["result"]["local_launches"]["stopping"] = json!(false);
    assert!(!can_drain(&response));
    response["result"]["local_launches"]["stopping"] = json!(true);
    response["result"]["profiles"] = json!([{"status":"unknown"}]);
    assert!(!can_drain(&response));
    response["result"]["profiles"] = json!([]);
    for key in ["updates", "startup_updates", "profile_warmup"] {
        response["result"][key] = json!({"worker_active":true});
        assert!(!can_drain(&response));
        response["result"][key] = json!({"worker_active":false});
    }
    assert!(can_drain(&response));
    response["result"]["profile_restarts"] = json!({"p":{"phase":"opening"}});
    assert!(!can_drain(&response));
}

#[tokio::test]
async fn full_exit_drains_real_adapter_once_and_keeps_retry_journal() {
    let root = tempdir().unwrap();
    std::fs::create_dir(root.path().join("scripts")).unwrap();
    let state = json!({"profiles":[],"view_instances":[],
        "local_launches":{"active":0,"stopping":true},
        "remote_updates":{"worker_active":true,"items":[{"job":{"state":"waiting"}}]}});
    let journal = root.path().join("fixture-state.json");
    std::fs::write(&journal, state.to_string()).unwrap();
    std::fs::write(root.path().join("scripts/control_center.py"), r#"
import sys,json,time
from pathlib import Path
for line in sys.stdin:
    req=json.loads(line)
    print(json.dumps({'id':req['id'],'ok':True,'result':json.loads(Path('fixture-state.json').read_text())}),flush=True)
# Emulate an already admitted worker finishing after the first bounded wait.
time.sleep(3.4)
Path('adapter-exited').write_text('normal EOF')
"#).unwrap();
    let service = service(root.path().into());
    assert_eq!(service.dispatch(request("state", json!({}))).await["ok"], true);
    assert_eq!(service.dispatch(request("supervisor.retire", json!({}))).await["error"]["code"], "backend_busy");
    service.clients.store(2, Ordering::SeqCst);
    assert_eq!(service.dispatch(request("supervisor.shutdown", json!({}))).await["error"]["code"], "backend_busy");
    assert!(!service.draining.load(Ordering::SeqCst));
    service.clients.store(1, Ordering::SeqCst);
    let first = service.dispatch(request("supervisor.shutdown", json!({}))).await;
    assert_eq!(first["error"]["code"], "backend_busy");
    assert!(service.draining.load(Ordering::SeqCst));
    assert!(!service.stopping.load(Ordering::SeqCst));
    assert_eq!(service.dispatch(request("state", json!({}))).await["error"]["code"], "service_draining");
    assert_eq!(service.dispatch(request("profile.show", json!({}))).await["error"]["code"], "service_draining");
    let result = service.dispatch(request("supervisor.shutdown", json!({}))).await;
    assert_eq!(result["ok"], true, "{result}");
    assert!(service.stopping.load(Ordering::SeqCst));
    assert_eq!(service.backend.status().await["backend_status"], "not_started");
    assert_eq!(std::fs::read_to_string(root.path().join("adapter-exited")).unwrap(), "normal EOF");
    assert_eq!(std::fs::read_to_string(&journal).unwrap(), state.to_string());
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

#[test]
fn stop_without_a_recorded_process_reports_already_stopped_and_close_still_refuses() {
    let root = tempdir().unwrap();
    let id = uuid::Uuid::new_v4().to_string();
    let generation = uuid::Uuid::new_v4().to_string();
    let ui = root
        .path()
        .join("work/control-center/profiles")
        .join(&id)
        .join("ui");
    let state = root.path().join("work/control-center/state.json");
    std::fs::create_dir_all(state.parent().unwrap()).unwrap();
    for ui in [ui.clone(), root.path().join("elsewhere/ui")] {
        std::fs::write(
            &state,
            serde_json::to_vec(&json!({"profiles":[{"id":id,"generation":generation,"ui_home":ui,"process_id":null}]}))
                .unwrap(),
        )
        .unwrap();
        let expected = json!({"profile_id":id,"generation":generation});
        let stopped = processes::execute(root.path(), "process.stop", &expected).unwrap();
        assert_eq!(stopped["state"], "already_stopped");
        assert!(
            !root
                .path()
                .join("work/control-center/instances")
                .join(&id)
                .join("last-recovery.json")
                .exists()
        );
        let mut close = expected.clone();
        close["window_handle"] = json!(1);
        assert!(processes::execute(root.path(), "process.close", &close).is_err());
    }
    // An abort needs the launch's own process-owner.json record.
    let abort = json!({"profile_id":id,"generation":generation,"process_id":1,"process_created":1});
    assert!(processes::execute(root.path(), "process.abort_launch", &abort).is_err());
}

#[cfg(feature = "process-fixture")]
struct FixtureProfile {
    root: tempfile::TempDir,
    executable: PathBuf,
    id: String,
    generation: String,
    ui: PathBuf,
    home: PathBuf,
}

#[cfg(feature = "process-fixture")]
impl FixtureProfile {
    fn new() -> Self {
        let root = tempdir().unwrap();
        let fixture =
            PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target/debug/process-fixture.exe");
        assert!(fixture.is_file(), "build process-fixture first");
        let executable = root
            .path()
            .join("artifacts/managed-desktop/fixture/process.exe");
        std::fs::create_dir_all(executable.parent().unwrap()).unwrap();
        std::fs::copy(fixture, &executable).unwrap();
        let id = uuid::Uuid::new_v4().to_string();
        let directory = root.path().join("work/control-center/profiles").join(&id);
        let (ui, home) = (directory.join("ui"), directory.join("codex"));
        std::fs::create_dir_all(&ui).unwrap();
        std::fs::create_dir_all(&home).unwrap();
        let generation = uuid::Uuid::new_v4().to_string();
        Self {
            root,
            executable,
            id,
            generation,
            ui,
            home,
        }
    }

    fn write(&self, extra: Value) {
        let mut profile =
            json!({"id":self.id,"generation":self.generation,"home":self.home,"ui_home":self.ui});
        for (key, value) in extra.as_object().unwrap() {
            profile[key] = value.clone();
        }
        std::fs::write(
            self.root.path().join("work/control-center/state.json"),
            serde_json::to_vec(&json!({"profiles":[profile]})).unwrap(),
        )
        .unwrap();
    }

    fn environment(&self, directory: &Path) -> Value {
        // Hidden per-drive entries such as `=C:` are not launch variables.
        let mut env: std::collections::BTreeMap<String, String> = std::env::vars()
            .filter(|(key, _)| !key.contains('='))
            .collect();
        env.insert("CODEX_HOME".into(), self.home.to_string_lossy().into());
        env.insert(
            "CODEX_PROCESS_FIXTURE_DIRECTORY".into(),
            directory.to_string_lossy().into(),
        );
        json!(env)
    }

    /// Starts the fixture through process.launch; returns its identity and child pid.
    fn launch(&self, generation: &str) -> (Value, u32) {
        let _ = std::fs::remove_file(self.root.path().join("child.pid"));
        let args = json!({"profile_id":self.id,"generation":generation,
            "environment":self.environment(self.root.path()),"executable":self.executable,"embed":true});
        let started = processes::execute(self.root.path(), "process.launch", &args).unwrap();
        (started, self.child(self.root.path()))
    }

    fn child(&self, directory: &Path) -> u32 {
        let deadline = Instant::now() + Duration::from_secs(3);
        while !directory.join("child.pid").exists() && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(10));
        }
        std::thread::sleep(Duration::from_millis(20));
        std::fs::read_to_string(directory.join("child.pid"))
            .unwrap()
            .parse()
            .unwrap()
    }

    /// Starts the fixture directly (not through the broker) with these arguments.
    fn spawn(&self, executable: &Path, directory: &Path, args: &[String]) -> std::process::Child {
        std::fs::create_dir_all(directory).unwrap();
        std::process::Command::new(executable)
            .args(args)
            .env("CODEX_PROCESS_FIXTURE_DIRECTORY", directory)
            .spawn()
            .unwrap()
    }
}

#[cfg(feature = "process-fixture")]
impl Drop for FixtureProfile {
    fn drop(&mut self) {
        // Every fixture polls for this file and exits.
        for directory in ["", "survivor", "duplicate"] {
            let _ = std::fs::write(self.root.path().join(directory).join("exit"), b"done");
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

#[cfg(feature = "process-fixture")]
#[test]
fn stop_sweeps_the_profile_folder_when_the_recorded_main_is_null_or_exited() {
    let fixture = FixtureProfile::new();
    let expected = json!({"profile_id":fixture.id,"generation":fixture.generation});
    // A desktop on this profile's folder but outside the managed copy, and a
    // managed one whose last --user-data-dir names another folder, survive.
    let outside = fixture.root.path().join("elsewhere/process.exe");
    std::fs::create_dir_all(outside.parent().unwrap()).unwrap();
    std::fs::copy(&fixture.executable, &outside).unwrap();
    let flag = format!("--user-data-dir={}", fixture.ui.display());
    let mut survivor = fixture.spawn(
        &outside,
        &fixture.root.path().join("survivor"),
        std::slice::from_ref(&flag),
    );
    let other = fixture
        .root
        .path()
        .join("work/control-center/profiles/other/ui");
    let mut duplicate = fixture.spawn(
        &fixture.executable,
        &fixture.root.path().join("duplicate"),
        &[flag.clone(), format!("--user-data-dir={}", other.display())],
    );
    let dead = {
        let mut exited = std::process::Command::new("cmd.exe")
            .args(["/D", "/C", "exit"])
            .spawn()
            .unwrap();
        exited.wait().unwrap();
        exited.id()
    };
    for recorded in [
        json!({"process_id":null}),
        json!({"process_id":dead,"process_created":1,"executable_path":fixture.executable}),
    ] {
        // The launch failed before its identity was saved: state.json still
        // holds the old (null or exited) process.
        fixture.write(recorded);
        let (started, child) = fixture.launch(&fixture.generation);
        let main = started["process_id"].as_u64().unwrap() as u32;
        assert!(processes::identity(main).is_some());
        assert!(processes::identity(child).is_some());
        let stopped = processes::execute(fixture.root.path(), "process.stop", &expected).unwrap();
        assert_eq!(stopped["state"], "stopped");
        assert!(processes::identity(main).is_none());
        assert!(processes::identity(child).is_none());
        assert!(
            fixture
                .root
                .path()
                .join("work/control-center/instances")
                .join(&fixture.id)
                .join("last-recovery.json")
                .is_file()
        );
    }
    assert!(survivor.try_wait().unwrap().is_none());
    assert!(duplicate.try_wait().unwrap().is_none());
    assert_eq!(
        processes::execute(fixture.root.path(), "process.stop", &expected).unwrap()["state"],
        "already_stopped"
    );
    assert!(processes::identity(std::process::id()).is_some());
}

#[cfg(feature = "process-fixture")]
#[test]
fn stop_sweeps_past_a_reused_recorded_pid_and_spares_its_new_owner() {
    let fixture = FixtureProfile::new();
    let expected = json!({"profile_id":fixture.id,"generation":fixture.generation});
    // Windows gave the recorded pid to an unrelated live process: a copy
    // outside the managed desktop that never names this profile's folder.
    let outside = fixture.root.path().join("elsewhere/process.exe");
    std::fs::create_dir_all(outside.parent().unwrap()).unwrap();
    std::fs::copy(&fixture.executable, &outside).unwrap();
    let mut unrelated = fixture.spawn(&outside, &fixture.root.path().join("survivor"), &[]);
    let owner = processes::identity(unrelated.id()).unwrap();
    fixture.write(json!({"process_id":null}));
    let (started, child) = fixture.launch(&fixture.generation);
    let main = started["process_id"].as_u64().unwrap() as u32;
    // The recorded lifetime itself with a foreign exe is still refused.
    fixture.write(
        json!({"process_id":owner.process_id,"process_created":owner.process_created,
        "executable_path":fixture.executable}),
    );
    assert!(processes::execute(fixture.root.path(), "process.stop", &expected).is_err());
    assert!(processes::identity(main).is_some());
    assert!(processes::identity(child).is_some());
    let mut orphan = Some((main, child));
    for recorded in [
        // This test process and the unrelated copy, each under a creation
        // time that is not theirs.
        json!({"process_id":std::process::id(),"process_created":1,"executable_path":fixture.executable}),
        json!({"process_id":owner.process_id,"process_created":owner.process_created + 1,
            "executable_path":fixture.executable}),
        // System: a live pid this user cannot open for termination.
        json!({"process_id":4,"process_created":1,"executable_path":fixture.executable}),
    ] {
        let (main, child) = orphan.take().unwrap_or_else(|| {
            fixture.write(json!({"process_id":null}));
            let (started, child) = fixture.launch(&fixture.generation);
            (started["process_id"].as_u64().unwrap() as u32, child)
        });
        fixture.write(recorded.clone());
        let stopped = processes::execute(fixture.root.path(), "process.stop", &expected).unwrap();
        assert_eq!(stopped["state"], "stopped", "{recorded}");
        assert!(processes::identity(main).is_none(), "{recorded}");
        assert!(processes::identity(child).is_none(), "{recorded}");
        let selected: Vec<_> = stopped["processes"]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| p["process_id"].as_u64().unwrap())
            .collect();
        for spared in [owner.process_id, std::process::id(), 4] {
            assert!(!selected.contains(&u64::from(spared)), "{recorded}");
        }
        assert!(unrelated.try_wait().unwrap().is_none(), "{recorded}");
        assert_eq!(
            processes::identity(owner.process_id).map(|p| p.process_created),
            Some(owner.process_created)
        );
    }
    assert!(processes::identity(std::process::id()).is_some());
}

#[cfg(feature = "process-fixture")]
#[test]
fn abort_launch_stops_only_the_recorded_unsaved_launch() {
    let fixture = FixtureProfile::new();
    // The saved profile still names the previous generation and no process.
    fixture.write(json!({"process_id":null}));
    let generation = uuid::Uuid::new_v4().to_string();
    let (started, child) = fixture.launch(&generation);
    let main = started["process_id"].as_u64().unwrap() as u32;
    let abort = json!({"profile_id":fixture.id,"generation":generation,
        "process_id":main,"process_created":started["process_created"]});
    for (key, value) in [
        ("generation", json!(fixture.generation)),
        ("generation", json!(uuid::Uuid::new_v4().to_string())),
        (
            "process_created",
            json!(started["process_created"].as_u64().unwrap() + 1),
        ),
        ("process_id", json!(child)),
    ] {
        let mut wrong = abort.clone();
        wrong[key] = value;
        assert!(
            processes::execute(fixture.root.path(), "process.abort_launch", &wrong).is_err(),
            "{key} mismatch refused"
        );
        assert!(processes::identity(main).is_some());
        assert!(processes::identity(child).is_some());
    }
    // Another profile's recorded process is never part of an abort.
    let state = fixture.root.path().join("work/control-center/state.json");
    let saved = std::fs::read(&state).unwrap();
    std::fs::write(
        &state,
        serde_json::to_vec(&json!({"profiles":[
            {"id":fixture.id,"generation":fixture.generation,"home":fixture.home,"ui_home":fixture.ui,"process_id":null},
            {"id":uuid::Uuid::new_v4(),"process_id":main}]}))
        .unwrap(),
    )
    .unwrap();
    assert!(processes::execute(fixture.root.path(), "process.abort_launch", &abort).is_err());
    assert!(processes::identity(main).is_some());
    // Once state.json owns this generation the launch was saved: no abort.
    fixture.write(json!({"generation":generation,"process_id":main,
        "process_created":started["process_created"],"executable_path":fixture.executable}));
    assert!(processes::execute(fixture.root.path(), "process.abort_launch", &abort).is_err());
    assert!(processes::identity(main).is_some());
    assert!(processes::identity(child).is_some());
    std::fs::write(&state, &saved).unwrap();
    let stopped = processes::execute(fixture.root.path(), "process.abort_launch", &abort).unwrap();
    assert_eq!(stopped["state"], "stopped");
    assert_eq!(stopped["generation"], json!(generation));
    assert!(processes::identity(main).is_none());
    assert!(processes::identity(child).is_none());
    assert_eq!(std::fs::read(&state).unwrap(), saved);
    assert!(
        !fixture
            .root
            .path()
            .join("work/control-center/instances")
            .join(&fixture.id)
            .join("last-recovery.json")
            .exists()
    );
    assert!(processes::identity(std::process::id()).is_some());
}

fn service(root: PathBuf) -> Arc<Service> {
    let records = Arc::new(records::Hub::open(&root));
    // The worker's first scan (no worker runs in tests): polls answer after it.
    records.tick(records::now_ms());
    Arc::new(Service {
        backend: backend::Backend::new(
            root.clone(),
            root.clone(),
            "test-pipe".into(),
            "test-token".into(),
        ),
        records,
        record_clients: AtomicUsize::new(0),
        pending_clients: AtomicUsize::new(0),
        root,
        token: "test-token".into(),
        revision: "test".into(),
        notes: Arc::default(),
        gates: Mutex::default(),
        operations: std::sync::Mutex::default(),
        clients: AtomicUsize::new(0),
        stopping: AtomicBool::new(false),
        draining: AtomicBool::new(false),
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

// Cross-profile record hub. Writer files and journals live in temp roots only.

fn signal_file(root: &std::path::Path, name: &str, bytes: &[u8]) {
    let directory = root.join("work/control-center/record-signals");
    std::fs::create_dir_all(&directory).unwrap();
    // Replace atomically, as adapters and runtime proxies do.
    let temporary = directory.join(format!("{name}.tmp"));
    std::fs::write(&temporary, bytes).unwrap();
    std::fs::rename(&temporary, directory.join(name)).unwrap();
    // Distinct directory timestamps, as between real 400 ms writer flushes.
    std::thread::sleep(Duration::from_millis(20));
}

fn signals(root: &std::path::Path, name: &str, generation: &str, changes: Value) {
    let data = json!({"version":2,"generation":generation,"changes":changes});
    signal_file(root, name, data.to_string().as_bytes());
}

fn new_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

async fn poll_hub(hub: &records::Hub, args: Value) -> Value {
    hub.poll(records::Query::parse(&args).unwrap()).await
}

fn ids(reply: &Value) -> Vec<&str> {
    reply["events"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e["id"].as_str().unwrap())
        .collect()
}

/// Lets the quiet window elapse and the journal write land (at most every
/// 3 s while only plain changes are undelivered), after which the commits
/// are deliverable.
fn settle(hub: &records::Hub, t: &mut u64) {
    hub.tick(*t);
    hub.tick(*t + 1100);
    hub.tick(*t + 4200);
    *t += 5000;
}

#[tokio::test]
async fn record_hub_merges_republished_changes_once_per_quiet_window() {
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let (a, b, c) = (new_id(), new_id(), new_id());
    let (thread, other, stream) = (new_id(), new_id(), new_id());
    let (desktop_a, desktop_b) = (format!("{a}.desktop.json"), format!("{b}.desktop.json"));
    let t = records::now_ms();
    hub.tick(t);
    let start = poll_hub(&hub, json!({"profile":c})).await;
    assert_eq!(
        (start["reset"].clone(), start["cursor"].clone()),
        (json!(true), json!(0))
    );
    let epoch = start["epoch"].clone();
    // Two profiles report one SSH thread inside the quiet window: one event.
    // A shared host broadcasts only starts and renames to every connection,
    // not turns, so neither profile is taken to know the other's change.
    signals(
        root.path(),
        &desktop_a,
        "ga",
        json!([[thread, 1, "ssh:box", "changed"]]),
    );
    hub.tick(t + 100);
    signals(
        root.path(),
        &desktop_b,
        "gb",
        json!([[thread, 7, "ssh:box", "changed"], [other, 8]]),
    );
    hub.tick(t + 500);
    hub.tick(t + 1200);
    assert_eq!(
        (
            hub.status()["pending"].clone(),
            hub.status()["head"].clone()
        ),
        (json!(2), json!(0))
    );
    hub.tick(t + 1300);
    assert_eq!(hub.status()["head"], 2);
    // Plain changes only: journaled (and deliverable) 3 s after the last write.
    assert_eq!(hub.status()["cursor"], 0);
    hub.tick(t + 3100);
    assert_eq!(hub.status()["cursor"], 2);
    let seen = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":0})).await;
    assert_eq!(
        seen,
        json!({"epoch":epoch,"cursor":2,"reset":false,"more":false,"events":[
            {"host":"ssh:box","id":thread,"kind":"changed","seq":1,"origins":[]},
            {"host":"local","id":other,"kind":"changed","seq":2,"origins":[b]}]})
    );
    // Origin suppression: a writer never hears its own report back.
    let own = poll_hub(&hub, json!({"profile":a,"epoch":epoch,"cursor":0})).await;
    assert_eq!(ids(&own), [thread.as_str(), other.as_str()]);
    let theirs = poll_hub(&hub, json!({"profile":b,"epoch":epoch,"cursor":0})).await;
    assert_eq!(
        (ids(&theirs), theirs["cursor"].clone()),
        (vec![thread.as_str()], json!(2))
    );
    // A rewritten file with already-ingested sequence numbers is not news.
    let mut t = t + 4000;
    signals(
        root.path(),
        &desktop_a,
        "ga",
        json!([[thread, 1, "ssh:box", "changed"]]),
    );
    settle(&hub, &mut t);
    assert_eq!(hub.status()["head"], 2);
    // A writer's next entry and a restarted writer (new generation) are.
    signals(
        root.path(),
        &desktop_a,
        "ga",
        json!([[thread, 1, "ssh:box", "changed"], [other, 2]]),
    );
    settle(&hub, &mut t);
    signals(
        root.path(),
        &desktop_b,
        "gb2",
        json!([[thread, 1, "ssh:box", "archived"]]),
    );
    settle(&hub, &mut t);
    let later = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":2})).await;
    // `other` replaced B's event with A's: only a profile that sent both is
    // skipped at every cursor (none); `thread` replaced [a, b] with [b].
    assert_eq!(
        later["events"],
        json!([{"host":"local","id":other,"kind":"changed","seq":3,"origins":[]},
               {"host":"ssh:box","id":thread,"kind":"archived","seq":4,"origins":[b]}])
    );
    // A continuously re-reported (streaming) thread is not starved by the window.
    for (step, seq) in (3..9).enumerate() {
        signals(
            root.path(),
            &desktop_a,
            "ga",
            json!([[thread, 1, "ssh:box", "changed"], [other, 2], [stream, seq]]),
        );
        hub.tick(t + 500 * step as u64);
    }
    assert_eq!(hub.status()["head"], 4);
    hub.tick(t + 3000);
    assert_eq!(hub.status()["head"], 5);
}

#[tokio::test]
async fn record_hub_keeps_last_visibility_and_filters_origins_and_hosts() {
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let (a, b, c) = (new_id(), new_id(), new_id());
    let (task, remote) = (new_id(), new_id());
    let (desktop_a, desktop_b) = (format!("{a}.desktop.json"), format!("{b}.desktop.json"));
    let mut t = records::now_ms();
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    signals(
        root.path(),
        &desktop_a,
        "ga",
        json!([[task, 1, "local", "archived"]]),
    );
    settle(&hub, &mut t);
    // A peer that has not seen the archive reports an ordinary change.
    signals(
        root.path(),
        &desktop_b,
        "gb",
        json!([[task, 1, "local", "changed"]]),
    );
    settle(&hub, &mut t);
    assert_eq!(hub.status()["head"], 1);
    signals(
        root.path(),
        &desktop_b,
        "gb",
        json!([[task, 2, "local", "unarchived"]]),
    );
    settle(&hub, &mut t);
    // Compacted per thread: the last visibility event by hub order wins. An
    // unarchive supersedes nothing: B is skipped only once past A's archive.
    let replay = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":0})).await;
    assert_eq!(
        replay["events"],
        json!([{"host":"local","id":task,"kind":"unarchived","seq":2,"origins":[]}])
    );
    let unseen = poll_hub(&hub, json!({"profile":b,"epoch":epoch,"cursor":0})).await;
    assert_eq!(ids(&unseen), [task.as_str()]);
    let seen = poll_hub(&hub, json!({"profile":b,"epoch":epoch,"cursor":1})).await;
    assert_eq!(seen["events"], json!([]));
    // Deletion is final.
    signals(
        root.path(),
        &desktop_a,
        "ga",
        json!([[task, 3, "local", "deleted"]]),
    );
    settle(&hub, &mut t);
    signals(
        root.path(),
        &desktop_b,
        "gb",
        json!([[task, 4, "local", "changed"]]),
    );
    settle(&hub, &mut t);
    assert_eq!(hub.status()["head"], 3);
    let deleted = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":2})).await;
    assert_eq!(ids(&deleted), [task.as_str()]);
    assert_eq!(deleted["events"][0]["kind"], "deleted");
    let own = poll_hub(&hub, json!({"profile":a,"epoch":epoch,"cursor":2})).await;
    assert_eq!(
        (own["events"].clone(), own["cursor"].clone()),
        (json!([]), json!(3))
    );
    // Runtime-proxy files speak for the same profile as its desktop file.
    signals(
        root.path(),
        &format!("{c}.json"),
        "gc",
        json!([[remote, 1, "ssh:box", "changed"]]),
    );
    settle(&hub, &mut t);
    let local = poll_hub(
        &hub,
        json!({"profile":b,"epoch":epoch,"cursor":3,"hosts":["local"]}),
    )
    .await;
    assert_eq!(
        (local["events"].clone(), local["cursor"].clone()),
        (json!([]), json!(4))
    );
    let ssh = poll_hub(
        &hub,
        json!({"profile":b,"epoch":epoch,"cursor":3,"hosts":["ssh:box"]}),
    )
    .await;
    assert_eq!(ids(&ssh), [remote.as_str()]);
    assert_eq!(ssh["events"][0]["origins"], json!([c]));
    let origin = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":3})).await;
    assert_eq!(origin["events"], json!([]));
}

#[tokio::test]
async fn record_polls_reset_on_unknown_epoch_or_compaction_and_cap_replies() {
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let (a, b, c) = (new_id(), new_id(), new_id());
    let mut t = records::now_ms();
    hub.tick(t);
    let start = poll_hub(&hub, json!({"profile":c})).await;
    let epoch = start["epoch"].clone();
    assert!(epoch.as_str().is_some_and(|e| !e.is_empty()));
    assert_eq!(
        start,
        json!({"epoch":epoch,"cursor":0,"reset":true,"events":[],"more":false})
    );
    for args in [
        json!({"profile":c,"epoch":"another-epoch","cursor":0}),
        json!({"profile":c,"epoch":epoch}),
        json!({"profile":c,"epoch":epoch,"cursor":1}),
    ] {
        assert_eq!(poll_hub(&hub, args).await, start);
    }
    let first: Vec<Value> = (1..=600).map(|seq| json!([new_id(), seq])).collect();
    signals(
        root.path(),
        &format!("{a}.desktop.json"),
        "ga",
        json!(first),
    );
    settle(&hub, &mut t);
    let page = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":0})).await;
    assert_eq!(page["events"].as_array().unwrap().len(), 512);
    assert_eq!(
        (page["more"].clone(), page["cursor"].clone()),
        (json!(true), json!(512))
    );
    let rest = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":512})).await;
    assert_eq!(rest["events"].as_array().unwrap().len(), 88);
    assert_eq!(
        (rest["more"].clone(), rest["cursor"].clone()),
        (json!(false), json!(600))
    );
    // 4096 newer threads evict the 600 oldest; cursors before them reset.
    let newer: Vec<Value> = (1..=4096).map(|seq| json!([new_id(), seq])).collect();
    signals(
        root.path(),
        &format!("{b}.desktop.json"),
        "gb",
        json!(newer),
    );
    settle(&hub, &mut t);
    let status = hub.status();
    assert_eq!(
        (status["threads"].clone(), status["floor"].clone()),
        (json!(4096), json!(600))
    );
    let stale = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":599})).await;
    assert_eq!(
        (stale["reset"].clone(), stale["cursor"].clone()),
        (json!(true), json!(4696))
    );
    assert_eq!(stale["events"], json!([]));
    let current = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":600})).await;
    assert_eq!(current["reset"], false);
    assert_eq!(current["events"].as_array().unwrap().len(), 512);
}

#[tokio::test]
async fn record_long_poll_wakes_on_delivery_and_times_out_otherwise() {
    let root = tempdir().unwrap();
    let hub = Arc::new(records::Hub::open(root.path()));
    let (a, c) = (new_id(), new_id());
    let t = records::now_ms();
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    let started = Instant::now();
    let idle = poll_hub(
        &hub,
        json!({"profile":c,"epoch":epoch,"cursor":0,"wait_ms":300}),
    )
    .await;
    assert!(started.elapsed() >= Duration::from_millis(280));
    assert!(started.elapsed() < Duration::from_secs(3));
    assert_eq!(
        (idle["reset"].clone(), idle["events"].clone()),
        (json!(false), json!([]))
    );
    let waiting = |profile: String| {
        let (hub, epoch) = (hub.clone(), epoch.clone());
        tokio::spawn(async move {
            let args = json!({"profile":profile,"epoch":epoch,"cursor":0,"wait_ms":20000});
            poll_hub(&hub, args).await
        })
    };
    let peer = waiting(c.clone());
    let writer = waiting(a.clone());
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(!peer.is_finished());
    let thread = new_id();
    signals(
        root.path(),
        &format!("{a}.desktop.json"),
        "ga",
        json!([[thread, 1]]),
    );
    let started = Instant::now();
    hub.tick(t + 100);
    hub.tick(t + 1200);
    hub.tick(t + 3200);
    let woke = tokio::time::timeout(Duration::from_secs(2), peer)
        .await
        .unwrap()
        .unwrap();
    assert!(started.elapsed() < Duration::from_secs(2));
    assert_eq!(ids(&woke), [thread.as_str()]);
    // The writer's own poll keeps waiting: its own change is not news to it.
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(!writer.is_finished());
    hub.close();
    let closed = tokio::time::timeout(Duration::from_secs(1), writer)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(closed["closing"], true);
    assert_eq!(closed["events"], json!([]));
}

#[tokio::test]
async fn record_journal_survives_restart_expires_tombstones_and_resets_when_unusable() {
    let root = tempdir().unwrap();
    let journal = root.path().join("work/control-center/record-journal.json");
    let (a, b, c) = (new_id(), new_id(), new_id());
    let (gone, kept, late) = (new_id(), new_id(), new_id());
    let (desktop_a, desktop_b) = (format!("{a}.desktop.json"), format!("{b}.desktop.json"));
    let mut t = records::now_ms();
    let hub = records::Hub::open(root.path());
    hub.tick(t);
    signals(
        root.path(),
        &desktop_a,
        "ga",
        json!([[gone, 1, "local", "deleted"], [kept, 2]]),
    );
    settle(&hub, &mut t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    // Still inside its quiet window at shutdown: committed and journaled.
    signals(root.path(), &desktop_b, "gb", json!([[late, 1]]));
    hub.tick(t);
    assert_eq!(hub.status()["pending"], 1);
    hub.finish(t + 10);
    drop(hub);
    let hub = records::Hub::open(root.path());
    let resumed = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":2})).await;
    assert_eq!(resumed["reset"], false);
    assert_eq!(ids(&resumed), [late.as_str()]);
    // Writer cursors were journaled too: retained files replay nothing.
    t += 2000;
    settle(&hub, &mut t);
    assert_eq!(hub.status()["head"], 3);
    // Tombstones expire after 30 days; cursors before them must reset.
    hub.tick(t + 31 * 24 * 60 * 60 * 1000);
    let status = hub.status();
    assert_eq!(
        (status["threads"].clone(), status["floor"].clone()),
        (json!(2), json!(1))
    );
    let expired = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":0})).await;
    assert_eq!(expired["reset"], true);
    let kept_cursor = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":1})).await;
    assert_eq!(kept_cursor["reset"], false);
    drop(hub);
    // A corrupt journal starts a new epoch and does not replay retained files.
    std::fs::write(&journal, br#"{"version":1,"epoch":"#).unwrap();
    let hub = records::Hub::open(root.path());
    // Before its first scan a new epoch answers nothing but `closing`.
    let early = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":3})).await;
    assert_eq!(early["closing"], true);
    hub.tick(t);
    let fresh = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":3})).await;
    assert_eq!(fresh["reset"], true);
    assert_ne!(fresh["epoch"], epoch);
    settle(&hub, &mut t);
    assert_eq!(hub.status()["head"], 0);
    drop(hub);
    // An unwritable journal still delivers, under a fresh epoch that the
    // older file on disk can never be mistaken for after a crash.
    std::fs::remove_file(&journal).unwrap();
    std::fs::create_dir(&journal).unwrap();
    let hub = records::Hub::open(root.path());
    settle(&hub, &mut t);
    let before = poll_hub(&hub, json!({"profile":c})).await;
    signals(root.path(), &desktop_a, "ga-restarted", json!([[kept, 1]]));
    settle(&hub, &mut t);
    let rotated = poll_hub(
        &hub,
        json!({"profile":c,"epoch":before["epoch"],"cursor":before["cursor"]}),
    )
    .await;
    assert_eq!(rotated["reset"], true);
    assert_ne!(rotated["epoch"], before["epoch"]);
    assert_eq!(hub.status()["volatile"], true);
    signals(
        root.path(),
        &desktop_a,
        "ga-restarted",
        json!([[kept, 1], [late, 2]]),
    );
    hub.tick(t);
    hub.tick(t + 800);
    let delivered = poll_hub(
        &hub,
        json!({"profile":c,"epoch":rotated["epoch"],"cursor":rotated["cursor"]}),
    )
    .await;
    assert_eq!(ids(&delivered), [late.as_str()]);
}

#[tokio::test]
async fn malformed_oversized_and_foreign_signal_files_are_ignored() {
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let c = new_id();
    let mut t = records::now_ms();
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    let (good, stamped, broken) = (new_id(), new_id(), new_id());
    let writers: Vec<String> = (0..5).map(|_| new_id()).collect();
    let broken_file = format!("{}.desktop.json", writers[0]);
    signal_file(root.path(), &broken_file, b"{not json");
    let future = json!({"version":3,"generation":"g","changes":[[broken,1]]});
    signal_file(
        root.path(),
        &format!("{}.desktop.json", writers[1]),
        future.to_string().as_bytes(),
    );
    let flood: Vec<Value> = (1..=4097).map(|seq| json!([broken, seq])).collect();
    signals(
        root.path(),
        &format!("{}.json", writers[2]),
        "g",
        json!(flood),
    );
    let mut oversized = json!({"version":2,"generation":"g","changes":[[broken,1]]})
        .to_string()
        .into_bytes();
    // Above the 4 MiB cap (the largest real file is about 1.7 MB).
    oversized.resize(5 * 1024 * 1024, b' ');
    signal_file(
        root.path(),
        &format!("{}.desktop.json", writers[3]),
        &oversized,
    );
    signals(root.path(), "plugins.json", "g", json!([[broken, 1]]));
    signals(
        root.path(),
        &format!("{}.desktop.json", writers[4]),
        "g",
        json!([
            [good, 1],
            ["not-a-uuid", 2],
            [broken, -3],
            [broken, 4.5],
            [broken, 5, "h".repeat(257), "changed"],
            [broken, 6, "local", "exploded"],
            [broken, 7, "local"],
            "text",
            [stamped, 8, "local", "changed", 1_790_000_000_000u64]
        ]),
    );
    settle(&hub, &mut t);
    let reply = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":0})).await;
    assert_eq!(ids(&reply), [good.as_str(), stamped.as_str()]);
    assert!(reply["events"][0].get("updatedAt").is_none());
    assert_eq!(reply["events"][1]["updatedAt"], 1_790_000_000_000u64);
    // A malformed file is read again once its writer replaces it.
    signals(root.path(), &broken_file, "g", json!([[broken, 1]]));
    settle(&hub, &mut t);
    let fixed = poll_hub(
        &hub,
        json!({"profile":c,"epoch":epoch,"cursor":reply["cursor"]}),
    )
    .await;
    assert_eq!(ids(&fixed), [broken.as_str()]);
}

#[tokio::test]
async fn record_polls_bypass_admission_and_version_pin_and_validate_arguments() {
    let root = tempdir().unwrap();
    let service = service(root.path().into());
    let profile = new_id();
    // Held by retirement and full exit; a record poll never waits for it.
    let exclusive = service.admission.write().await;
    let mut poll = request("records.poll", json!({"profile":profile}));
    poll.version = 1;
    let reply = tokio::time::timeout(Duration::from_secs(1), service.dispatch(poll))
        .await
        .unwrap();
    assert_eq!(reply["result"]["reset"], true);
    drop(exclusive);
    for args in [
        json!({}),
        json!({"profile":"not-a-profile"}),
        json!({"profile":profile,"cursor":-1}),
        json!({"profile":profile,"cursor":"1"}),
        json!({"profile":profile,"hosts":"local"}),
        json!({"profile":profile,"hosts":[""]}),
        json!({"profile":profile,"epoch":7}),
        json!({"profile":profile,"wait_ms":-5}),
    ] {
        let reply = service.dispatch(request("records.poll", args)).await;
        assert_eq!(reply["error"]["code"], "invalid_request");
    }
    let unknown = request("records.subscribe", json!({"profile":profile}));
    assert_eq!(
        service.dispatch(unknown).await["error"]["code"],
        "unknown_command"
    );
    service.stopping.store(true, Ordering::SeqCst);
    let started = Instant::now();
    let closing = service
        .dispatch(request(
            "records.poll",
            json!({"profile":profile,"wait_ms":20000}),
        ))
        .await;
    assert_eq!(closing["result"]["closing"], true);
    assert!(started.elapsed() < Duration::from_secs(1));
}

type PipeClient = BufReader<tokio::net::windows::named_pipe::NamedPipeClient>;

async fn pipe_client(name: &str) -> PipeClient {
    for _ in 0..300 {
        match tokio::net::windows::named_pipe::ClientOptions::new()
            .open(format!(r"\\.\pipe\{name}"))
        {
            Ok(client) => return BufReader::new(client),
            // Between accepting one client and listening for the next.
            Err(_) => tokio::time::sleep(Duration::from_millis(10)).await,
        }
    }
    panic!("pipe server unavailable");
}

async fn pipe_send(client: &mut PipeClient, command: &str, args: Value) {
    let request = json!({"version":protocol::VERSION,"id":new_id(),"command":command,"args":args});
    let mut line = serde_json::to_vec(&request).unwrap();
    line.push(b'\n');
    client.get_mut().write_all(&line).await.unwrap();
}

async fn pipe_receive(client: &mut PipeClient) -> Value {
    use tokio::io::AsyncBufReadExt;
    let mut line = String::new();
    tokio::time::timeout(Duration::from_secs(5), client.read_line(&mut line))
        .await
        .unwrap()
        .unwrap();
    serde_json::from_str(&line).unwrap()
}

async fn pipe_call(client: &mut PipeClient, command: &str, args: Value) -> Value {
    pipe_send(client, command, args).await;
    pipe_receive(client).await
}

async fn eventually(condition: impl Fn() -> bool) {
    for _ in 0..300 {
        if condition() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("condition not reached");
}

#[tokio::test]
async fn record_connections_hold_neither_retirement_nor_management_slots() {
    let root = tempdir().unwrap();
    let service = service(root.path().into());
    let name = format!("CodexControlCenter.records-test.{}", uuid::Uuid::new_v4());
    let listener = windows::server(&name, true).unwrap();
    let listening = tokio::spawn(listen(service.clone(), name.clone(), listener));
    let profile = new_id();
    let mut pollers = Vec::new();
    let mut start = Value::Null;
    for _ in 0..records::MAX_CONNECTIONS {
        let mut client = pipe_client(&name).await;
        start = pipe_call(&mut client, "records.poll", json!({"profile":profile})).await;
        assert_eq!(start["result"]["reset"], true);
        pollers.push(client);
    }
    // Retirement, idle exit and the 32 management slots all read `clients`.
    assert_eq!(service.clients.load(Ordering::SeqCst), 0);
    let records_open = || service.record_clients.load(Ordering::SeqCst);
    assert_eq!(records_open(), records::MAX_CONNECTIONS);
    let mut surplus = pipe_client(&name).await;
    let refused = pipe_call(&mut surplus, "records.poll", json!({"profile":profile})).await;
    assert_eq!(refused["error"]["code"], "records_busy");
    eventually(|| service.clients.load(Ordering::SeqCst) == 0).await;
    let mut shell = pipe_client(&name).await;
    let status = pipe_call(&mut shell, "supervisor.status", json!({})).await;
    assert_eq!(status["result"]["clients"], 1);
    assert_eq!(status["result"]["record_clients"], records::MAX_CONNECTIONS);
    // A record connection that issues management commands counts again.
    let mut mixed = pollers.pop().unwrap();
    pipe_call(&mut mixed, "supervisor.status", json!({})).await;
    assert_eq!(service.clients.load(Ordering::SeqCst), 2);
    drop(mixed);
    eventually(|| service.clients.load(Ordering::SeqCst) == 1).await;
    assert_eq!(records_open(), records::MAX_CONNECTIONS - 1);
    // A parked long poll neither blocks retirement nor delays shutdown.
    let mut waiting = pollers.pop().unwrap();
    let result = &start["result"];
    let args = json!({"profile":profile,"epoch":result["epoch"],"cursor":result["cursor"],"wait_ms":20000});
    pipe_send(&mut waiting, "records.poll", args).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    let started = Instant::now();
    let retired = pipe_call(&mut shell, "supervisor.retire", json!({})).await;
    assert_eq!(retired["ok"], true);
    let closing = pipe_receive(&mut waiting).await;
    assert_eq!(closing["result"]["closing"], true);
    assert!(started.elapsed() < Duration::from_secs(2));
    tokio::time::timeout(Duration::from_secs(5), listening)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
}

// Stage 3a: `records.publish`, a writer's direct path into the same pipeline.

fn report(host: &str, id: &str, kind: &str) -> Value {
    json!({"host":host,"id":id,"kind":kind})
}

fn publish_to(hub: &records::Hub, profile: &str, events: Value, now: u64) -> records::Published {
    let args = json!({"profile":profile,"events":events});
    hub.publish(records::Publish::parse(&args).unwrap(), now)
}

type Listening = tokio::task::JoinHandle<std::io::Result<()>>;

async fn records_pipe(root: &std::path::Path) -> (Arc<Service>, String, Listening) {
    let service = service(root.into());
    let name = format!("CodexControlCenter.records-test.{}", uuid::Uuid::new_v4());
    let listener = windows::server(&name, true).unwrap();
    let listening = tokio::spawn(listen(service.clone(), name.clone(), listener));
    (service, name, listening)
}

async fn retire_pipe(name: &str, listening: Listening) {
    let mut shell = pipe_client(name).await;
    let retired = pipe_call(&mut shell, "supervisor.retire", json!({})).await;
    assert_eq!(retired["ok"], true);
    tokio::time::timeout(Duration::from_secs(5), listening)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
}

#[tokio::test]
async fn published_reports_merge_with_signal_files_under_the_same_rules() {
    use records::Published::Accepted;
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let (a, b, c) = (new_id(), new_id(), new_id());
    let (first, second, shared, pair) = (new_id(), new_id(), new_id(), new_id());
    let mut t = records::now_ms();
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    // `first`: published, then in writer A's adapter and runtime-proxy files.
    // `second`: A's file first, then A's publish. `shared` (on an SSH host)
    // and `pair` (local): A publishes, B's file reports too. One event each;
    // neither profile is taken to know the other's change.
    let mut stamped = report("ssh:box", &first, "changed");
    stamped["updatedAt"] = json!(1_790_000_000_000u64);
    let reports = json!([
        stamped,
        report("ssh:box", &shared, "changed"),
        report("local", &pair, "changed")
    ]);
    assert_eq!(publish_to(&hub, &a, reports, t + 10), Accepted(3));
    signals(
        root.path(),
        &format!("{a}.desktop.json"),
        "ga",
        json!([
            [first, 1, "ssh:box", "changed"],
            [second, 2, "local", "changed"]
        ]),
    );
    signals(
        root.path(),
        &format!("{a}.json"),
        "gp",
        json!([[first, 9, "ssh:box", "changed"]]),
    );
    signals(
        root.path(),
        &format!("{b}.desktop.json"),
        "gb",
        json!([[shared, 1, "ssh:box", "changed"], [pair, 2]]),
    );
    hub.tick(t + 300);
    let late = json!([report("local", &second, "changed")]);
    assert_eq!(publish_to(&hub, &a, late, t + 400), Accepted(1));
    // Published reports wait out the quiet window like file reports.
    hub.tick(t + 900);
    let status = hub.status();
    assert_eq!(
        (status["pending"].clone(), status["head"].clone()),
        (json!(4), json!(0))
    );
    hub.tick(t + 1200);
    assert_eq!(hub.status()["head"], 4);
    hub.tick(t + 3400);
    let seen = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":0})).await;
    assert_eq!(
        seen["events"],
        json!([
            {"host":"ssh:box","id":first,"kind":"changed","seq":1,"origins":[a],"updatedAt":1_790_000_000_000u64},
            {"host":"ssh:box","id":shared,"kind":"changed","seq":2,"origins":[]},
            {"host":"local","id":pair,"kind":"changed","seq":3,"origins":[]},
            {"host":"local","id":second,"kind":"changed","seq":4,"origins":[a]}
        ])
    );
    // Two profiles' changes, local or remote: each hears the other's.
    let own = poll_hub(&hub, json!({"profile":a,"epoch":epoch,"cursor":0})).await;
    assert_eq!(
        (ids(&own), own["cursor"].clone()),
        (vec![shared.as_str(), pair.as_str()], json!(4))
    );
    let peer = poll_hub(&hub, json!({"profile":b,"epoch":epoch,"cursor":0})).await;
    assert_eq!(
        ids(&peer),
        [
            first.as_str(),
            shared.as_str(),
            pair.as_str(),
            second.as_str()
        ]
    );
    // Deletion is final, visibility is last-wins and an archived thread
    // ignores ordinary changes, whichever path reported them. Only the
    // reporter of the deciding kind is skipped: a plain change reported
    // alongside never makes its reporter miss the deletion or archive.
    t += 3000;
    let (gone, hidden, shown, stream) = (new_id(), new_id(), new_id(), new_id());
    let deleted = json!([report("local", &gone, "deleted")]);
    assert_eq!(publish_to(&hub, &a, deleted, t + 10), Accepted(1));
    signals(
        root.path(),
        &format!("{b}.desktop.json"),
        "gb",
        json!([
            [gone, 3, "local", "changed"],
            [hidden, 4, "local", "archived"]
        ]),
    );
    hub.tick(t + 300);
    let mixed = json!([
        report("local", &hidden, "changed"),
        report("local", &shown, "archived"),
        report("local", &shown, "unarchived")
    ]);
    assert_eq!(publish_to(&hub, &a, mixed, t + 400), Accepted(3));
    hub.tick(t + 500);
    assert_eq!(hub.status()["head"], 4);
    hub.tick(t + 1200);
    hub.tick(t + 2400);
    let ordered = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":4})).await;
    assert_eq!(
        ordered["events"],
        json!([
            {"host":"local","id":gone,"kind":"deleted","seq":5,"origins":[a]},
            {"host":"local","id":hidden,"kind":"archived","seq":6,"origins":[b]},
            {"host":"local","id":shown,"kind":"unarchived","seq":7,"origins":[a]}
        ])
    );
    // B is told about A's deletion; A is told about B's archive.
    let to_b = poll_hub(&hub, json!({"profile":b,"epoch":epoch,"cursor":4})).await;
    assert_eq!(ids(&to_b), [gone.as_str(), shown.as_str()]);
    let to_a = poll_hub(&hub, json!({"profile":a,"epoch":epoch,"cursor":4})).await;
    assert_eq!(ids(&to_a), [hidden.as_str()]);
    t += 3000;
    let stale = json!([
        report("local", &gone, "changed"),
        report("local", &gone, "unarchived"),
        report("local", &hidden, "changed")
    ]);
    assert_eq!(publish_to(&hub, &a, stale, t), Accepted(3));
    hub.tick(t + 1200);
    hub.tick(t + 2400);
    assert_eq!(hub.status()["head"], 7);
    // A continuously re-published (streaming) thread waits at most 3 s.
    t += 3000;
    for step in 0..6 {
        let now = t + 500 * step;
        let events = json!([report("local", &stream, "changed")]);
        assert_eq!(publish_to(&hub, &a, events, now), Accepted(1));
        hub.tick(now);
    }
    assert_eq!(hub.status()["head"], 7);
    hub.tick(t + 3000);
    assert_eq!(hub.status()["head"], 8);
}

#[tokio::test]
async fn record_publish_validates_strictly_and_bypasses_admission_and_version() {
    let root = tempdir().unwrap();
    let service = service(root.path().into());
    let (profile, thread) = (new_id(), new_id());
    let good = report("local", &thread, "changed");
    let with = |key: &str, value: Value| {
        let mut changed = good.clone();
        changed[key] = value;
        changed
    };
    let without = |key: &str| {
        let mut changed = good.clone();
        changed.as_object_mut().unwrap().remove(key);
        changed
    };
    let flood: Vec<Value> = (0..=records::MAX_PUBLISH_EVENTS)
        .map(|_| report("local", &new_id(), "changed"))
        .collect();
    let unhyphenated = thread.replace('-', "");
    for args in [
        json!({"events":[good]}),
        json!({"profile":"not-a-profile","events":[good]}),
        json!({"profile":profile}),
        json!({"profile":profile,"events":good}),
        json!({"profile":profile,"events":flood}),
        json!({"profile":profile,"events":[good],"broker_token":"x"}),
        json!({"profile":profile,"events":["text"]}),
        json!({"profile":profile,"events":[[thread, 1, "local", "changed"]]}),
        json!({"profile":profile,"events":[without("host")]}),
        json!({"profile":profile,"events":[without("id")]}),
        json!({"profile":profile,"events":[without("kind")]}),
        json!({"profile":profile,"events":[with("host", json!(""))]}),
        json!({"profile":profile,"events":[with("host", json!("h".repeat(257)))]}),
        json!({"profile":profile,"events":[with("host", json!("a\nb"))]}),
        json!({"profile":profile,"events":[with("host", json!(7))]}),
        json!({"profile":profile,"events":[with("id", json!("not-a-uuid"))]}),
        json!({"profile":profile,"events":[with("id", json!(unhyphenated))]}),
        json!({"profile":profile,"events":[with("kind", json!("exploded"))]}),
        json!({"profile":profile,"events":[with("kind", json!("Changed"))]}),
        json!({"profile":profile,"events":[with("updatedAt", json!(-1))]}),
        json!({"profile":profile,"events":[with("updatedAt", json!("2026-09-26"))]}),
        json!({"profile":profile,"events":[with("title", json!("private"))]}),
        // One malformed report refuses the whole request.
        json!({"profile":profile,"events":[good, with("kind", json!("exploded"))]}),
    ] {
        let reply = service
            .dispatch(request("records.publish", args.clone()))
            .await;
        assert_eq!(reply["error"]["code"], "invalid_request", "{args}");
    }
    assert_eq!(service.records.status()["pending"], 0);
    // Limits are inclusive; every kind the files use is accepted.
    let full: Vec<Value> = (0..records::MAX_PUBLISH_EVENTS)
        .map(|_| report("local", &new_id(), "changed"))
        .collect();
    let edge = json!([
        {"host":"h".repeat(256),"id":thread.to_uppercase(),"kind":"archived","updatedAt":1.5},
        {"host":"ssh:box","id":new_id(),"kind":"unarchived","updatedAt":null},
        report("local", &new_id(), "deleted")
    ]);
    // Held by retirement and full exit; a publish never waits for it, and
    // injected writers outlive a protocol version bump.
    let exclusive = service.admission.write().await;
    for (events, accepted) in [(json!(full), 256), (edge, 3), (json!([]), 0)] {
        let mut publish = request(
            "records.publish",
            json!({"profile":profile,"events":events}),
        );
        publish.version = 1;
        let reply = tokio::time::timeout(Duration::from_secs(1), service.dispatch(publish))
            .await
            .unwrap();
        assert_eq!(reply["result"], json!({"accepted":accepted}));
    }
    drop(exclusive);
    assert_eq!(service.records.status()["pending"], 259);
}

#[tokio::test]
async fn record_publish_during_shutdown_returns_closing_and_keeps_what_it_accepted() {
    let root = tempdir().unwrap();
    let (a, c) = (new_id(), new_id());
    let (kept, refused) = (new_id(), new_id());
    let t = records::now_ms();
    let hub = records::Hub::open(root.path());
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    let events = json!([report("local", &kept, "changed")]);
    assert_eq!(
        publish_to(&hub, &a, events, t + 10),
        records::Published::Accepted(1)
    );
    // Still inside its quiet window at shutdown: committed and journaled.
    hub.finish(t + 20);
    let events = json!([report("local", &refused, "changed")]);
    assert_eq!(
        publish_to(&hub, &a, events, t + 30),
        records::Published::Closing
    );
    drop(hub);
    let hub = records::Hub::open(root.path());
    let resumed = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":0})).await;
    assert_eq!(ids(&resumed), [kept.as_str()]);
    assert_eq!(hub.status()["pending"], 0);
    drop(hub);
    // Through the service: a closing hub or a stopping service takes nothing.
    let args = json!({"profile":a,"events":[report("local", &refused, "changed")]});
    let (closing_root, stopping_root) = (tempdir().unwrap(), tempdir().unwrap());
    let closing = service(closing_root.path().into());
    closing.records.close();
    let stopping = service(stopping_root.path().into());
    stopping.stopping.store(true, Ordering::SeqCst);
    for service in [closing, stopping] {
        let reply = service
            .dispatch(request("records.publish", args.clone()))
            .await;
        assert_eq!(reply["result"], json!({"closing":true,"accepted":0}));
        assert_eq!(service.records.status()["pending"], 0);
    }
}

#[tokio::test]
async fn record_connections_bind_one_profile_identity_and_log_it_once() {
    let root = tempdir().unwrap();
    let (service, name, listening) = records_pipe(root.path()).await;
    let (a, b, c) = (new_id(), new_id(), new_id());
    let (mine, forged, later) = (new_id(), new_id(), new_id());
    let one = |id: &str| json!([report("local", id, "changed")]);
    let mut writer = pipe_client(&name).await;
    // A malformed claim binds nothing.
    let invalid = pipe_call(
        &mut writer,
        "records.publish",
        json!({"profile":"nobody","events":one(&forged)}),
    )
    .await;
    assert_eq!(invalid["error"]["code"], "invalid_request");
    let first = pipe_call(
        &mut writer,
        "records.publish",
        json!({"profile":a,"events":one(&mine)}),
    )
    .await;
    assert_eq!(first["result"], json!({"accepted":1}));
    let spoofed = pipe_call(
        &mut writer,
        "records.publish",
        json!({"profile":b,"events":one(&forged)}),
    )
    .await;
    assert_eq!(spoofed["error"]["code"], "profile_mismatch");
    let polled = pipe_call(&mut writer, "records.poll", json!({"profile":b})).await;
    assert_eq!(polled["error"]["code"], "profile_mismatch");
    // The same profile in another spelling is the same identity.
    let again = pipe_call(
        &mut writer,
        "records.publish",
        json!({"profile":a.to_uppercase(),"events":one(&later)}),
    )
    .await;
    assert_eq!(again["result"], json!({"accepted":1}));
    let start = pipe_call(&mut writer, "records.poll", json!({"profile":a})).await;
    assert_eq!(start["result"]["reset"], true);
    // A connection that polled first is bound to that profile too.
    let mut reader = pipe_client(&name).await;
    pipe_call(&mut reader, "records.poll", json!({"profile":c})).await;
    let borrowed = pipe_call(
        &mut reader,
        "records.publish",
        json!({"profile":a,"events":one(&forged)}),
    )
    .await;
    assert_eq!(borrowed["error"]["code"], "profile_mismatch");
    // Only A's own reports were taken, each carrying A's origin once.
    let t = records::now_ms();
    service.records.tick(t + 800);
    let args = json!({"profile":c,"epoch":start["result"]["epoch"],"cursor":0});
    let seen = poll_hub(&service.records, args).await;
    assert_eq!(ids(&seen), [mine.as_str(), later.as_str()]);
    for event in seen["events"].as_array().unwrap() {
        assert_eq!(event["origins"], json!([a]));
    }
    // One content-free identity line per connection: its PID and profile.
    let log = root
        .path()
        .join("work/control-center/logs/rust-service.jsonl");
    let connected = || -> Vec<Value> {
        std::fs::read_to_string(&log)
            .unwrap_or_default()
            .lines()
            .filter_map(|line| serde_json::from_str::<Value>(line).ok())
            .filter(|entry| entry["event"] == "records.connected")
            .collect()
    };
    eventually(|| connected().len() >= 2).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    let mut profiles: Vec<String> = connected()
        .iter()
        .map(|entry| {
            assert_eq!(entry.as_object().unwrap().len(), 3, "{entry}");
            assert_eq!(entry["pid"], std::process::id());
            entry["profile"].as_str().unwrap().to_string()
        })
        .collect();
    profiles.sort();
    let mut expected = vec![a.clone(), c.clone()];
    expected.sort();
    assert_eq!(profiles, expected);
    let text = std::fs::read_to_string(&log).unwrap();
    for thread in [&mine, &forged, &later] {
        assert!(!text.contains(thread.as_str()));
    }
    drop((writer, reader));
    retire_pipe(&name, listening).await;
}

#[test]
fn concurrent_log_events_stay_whole_lines() {
    let root = tempdir().unwrap();
    std::thread::scope(|scope| {
        for pid in 0..8 {
            let root = root.path();
            scope.spawn(move || {
                for n in 0..50 {
                    let event =
                        json!({"event":"records.connected","pid":pid,"profile":new_id(),"n":n});
                    log_event(root, &event);
                }
            });
        }
    });
    let log = root
        .path()
        .join("work/control-center/logs/rust-service.jsonl");
    let text = std::fs::read_to_string(log).unwrap();
    let lines: Vec<Value> = text
        .lines()
        .map(|line| serde_json::from_str(line).expect(line))
        .collect();
    assert_eq!(lines.len(), 400);
}

#[tokio::test]
async fn record_publish_is_rate_limited_per_connection() {
    // Any sliding one-second window: 20 requests and 2000 events.
    let t0 = Instant::now();
    let second = Duration::from_secs(1);
    let half = Duration::from_millis(500);
    let mut requests = records::Throttle::connection();
    for at in [t0, t0 + half] {
        for _ in 0..records::PUBLISH_REQUESTS / 2 {
            assert!(requests.admit(1, at));
        }
    }
    assert!(!requests.admit(0, t0 + Duration::from_millis(999)));
    // Only the requests older than one second make room again.
    for at in [t0 + second, t0 + second + half] {
        for _ in 0..records::PUBLISH_REQUESTS / 2 {
            assert!(requests.admit(1, at));
        }
        assert!(!requests.admit(1, at));
    }
    let mut events = records::Throttle::connection();
    for _ in 0..7 {
        assert!(events.admit(256, t0));
    }
    assert!(!events.admit(256, t0));
    assert!(events.admit(records::PUBLISH_EVENTS - 7 * 256, t0));
    assert!(!events.admit(1, t0 + Duration::from_millis(500)));
    assert!(events.admit(256, t0 + second));
    // An oversized list is left to validation rather than refused as busy.
    assert!(records::Throttle::connection().admit(100_000, t0));
    // Over the pipe, per connection.
    let root = tempdir().unwrap();
    let (service, name, listening) = records_pipe(root.path()).await;
    let (a, b) = (new_id(), new_id());
    let batch = |count: usize| -> Vec<Value> {
        (0..count)
            .map(|_| report("local", &new_id(), "changed"))
            .collect()
    };
    let mut writer = pipe_client(&name).await;
    let started = Instant::now();
    let mut replies = Vec::new();
    for _ in 0..=records::PUBLISH_REQUESTS {
        let args = json!({"profile":a,"events":batch(1)});
        replies.push(pipe_call(&mut writer, "records.publish", args).await);
    }
    let elapsed = started.elapsed();
    let (last, taken) = replies.split_last().unwrap();
    assert!(taken.iter().all(|r| r["result"]["accepted"] == 1));
    assert_eq!(last["error"]["code"], "records_busy", "{elapsed:?}");
    // Polls are not charged, and another connection has its own budget.
    let poll = pipe_call(&mut writer, "records.poll", json!({"profile":a})).await;
    assert_eq!(poll["ok"], true);
    let mut other = pipe_client(&name).await;
    let args = json!({"profile":a,"events":batch(1)});
    assert_eq!(
        pipe_call(&mut other, "records.publish", args).await["result"]["accepted"],
        1
    );
    let mut bulk = pipe_client(&name).await;
    for _ in 0..7 {
        let args = json!({"profile":b,"events":batch(256)});
        let reply = pipe_call(&mut bulk, "records.publish", args).await;
        assert_eq!(reply["result"]["accepted"], 256);
    }
    let args = json!({"profile":b,"events":batch(256)});
    let refused = pipe_call(&mut bulk, "records.publish", args).await;
    assert_eq!(refused["error"]["code"], "records_busy");
    let args = json!({"profile":b,"events":batch(records::PUBLISH_EVENTS - 7 * 256)});
    let rest = pipe_call(&mut bulk, "records.publish", args).await;
    assert_eq!(rest["result"]["accepted"], 208);
    // Refused requests took nothing.
    assert_eq!(service.records.status()["pending"], 20 + 1 + 2000);
    // A throttled connection stays open and recovers after the window.
    tokio::time::sleep(second).await;
    let args = json!({"profile":a,"events":batch(1)});
    assert_eq!(
        pipe_call(&mut writer, "records.publish", args).await["result"]["accepted"],
        1
    );
    drop((writer, other, bulk));
    retire_pipe(&name, listening).await;
}

#[tokio::test]
async fn published_reports_wake_peer_polls_but_never_their_publisher() {
    use tokio::io::AsyncBufReadExt;
    let root = tempdir().unwrap();
    let (service, name, listening) = records_pipe(root.path()).await;
    let (a, c) = (new_id(), new_id());
    let (thread, answer) = (new_id(), new_id());
    let mut writer = pipe_client(&name).await;
    let start =
        pipe_call(&mut writer, "records.poll", json!({"profile":a})).await["result"].clone();
    let wait = |profile: &str| json!({"profile":profile,"epoch":start["epoch"],"cursor":start["cursor"],"wait_ms":20000});
    let mut peer = pipe_client(&name).await;
    pipe_send(&mut peer, "records.poll", wait(&c)).await;
    let mut own = pipe_client(&name).await;
    pipe_send(&mut own, "records.poll", wait(&a)).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    let mut archived = report("ssh:box", &thread, "archived");
    archived["updatedAt"] = json!(1_790_000_000_000u64);
    let args = json!({"profile":a,"events":[archived]});
    let published = pipe_call(&mut writer, "records.publish", args).await;
    assert_eq!(published["result"], json!({"accepted":1}));
    let t = records::now_ms();
    service.records.tick(t + 100);
    assert_eq!(service.records.status()["head"], 0);
    service.records.tick(t + 800);
    assert_eq!(service.records.status()["head"], 1);
    service.records.tick(t + 2000);
    let woke = pipe_receive(&mut peer).await;
    assert_eq!(
        woke["result"]["events"],
        json!([{"host":"ssh:box","id":thread,"kind":"archived","seq":1,"origins":[a],"updatedAt":1_790_000_000_000u64}])
    );
    // The publisher's own long poll skips its report and keeps waiting...
    let mut line = String::new();
    let quiet = tokio::time::timeout(Duration::from_millis(300), own.read_line(&mut line)).await;
    assert!(quiet.is_err(), "{line}");
    // ...until a peer's report arrives, which it then receives alone.
    let args = json!({"profile":c,"events":[report("local", &answer, "changed")]});
    let answered = pipe_call(&mut peer, "records.publish", args).await;
    assert_eq!(answered["result"]["accepted"], 1);
    service.records.tick(t + 3200);
    service.records.tick(t + 5100);
    let late = pipe_receive(&mut own).await;
    assert_eq!(ids(&late["result"]), [answer.as_str()]);
    assert_eq!(late["result"]["cursor"], 2);
    assert_eq!(late["result"]["events"][0]["origins"], json!([c]));
    drop((writer, peer, own));
    retire_pipe(&name, listening).await;
}

#[tokio::test]
async fn publish_only_connections_hold_neither_retirement_nor_management_slots() {
    let root = tempdir().unwrap();
    let (service, name, listening) = records_pipe(root.path()).await;
    let (a, c) = (new_id(), new_id());
    let (kept, refused) = (new_id(), new_id());
    let mut writer = pipe_client(&name).await;
    let args = json!({"profile":a,"events":[report("local", &kept, "changed")]});
    let published = pipe_call(&mut writer, "records.publish", args).await;
    assert_eq!(published["result"], json!({"accepted":1}));
    // Retirement, idle exit and the 32 management slots all read `clients`.
    assert_eq!(service.clients.load(Ordering::SeqCst), 0);
    assert_eq!(service.record_clients.load(Ordering::SeqCst), 1);
    let mut shell = pipe_client(&name).await;
    let status = pipe_call(&mut shell, "supervisor.status", json!({})).await;
    assert_eq!(status["result"]["clients"], 1);
    assert_eq!(status["result"]["record_clients"], 1);
    let retired = pipe_call(&mut shell, "supervisor.retire", json!({})).await;
    assert_eq!(retired["ok"], true);
    tokio::time::timeout(Duration::from_secs(5), listening)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    // The still-connected writer is told the service is closing; what it
    // published before was flushed into the journal for the next instance.
    let args = json!({"profile":a,"events":[report("local", &refused, "changed")]});
    let late = pipe_call(&mut writer, "records.publish", args).await;
    assert_eq!(late["result"], json!({"closing":true,"accepted":0}));
    drop((writer, shell));
    let hub = records::Hub::open(root.path());
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    let resumed = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":0})).await;
    assert_eq!(ids(&resumed), [kept.as_str()]);
    assert_eq!(resumed["events"][0]["origins"], json!([a]));
}

// Review fixes: replaced events, a full queue, budgets, worker health,
// parsing, connection pools, writer generations and the file cap.

fn one(id: &str, kind: &str) -> Value {
    json!([report("local", id, kind)])
}

/// Commits what is pending and makes it deliverable.
fn step(hub: &records::Hub, t: &mut u64) {
    hub.tick(*t + 800);
    hub.tick(*t + 4000);
    *t += 5000;
}

fn events_of(reply: &Value) -> Vec<Value> {
    reply["events"].as_array().unwrap().clone()
}

#[tokio::test]
async fn replaced_events_keep_visibility_updated_at_and_peer_changes() {
    use records::Published::Accepted;
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let (a, b, c) = (new_id(), new_id(), new_id());
    let (task, shared) = (new_id(), new_id());
    let mut t = records::now_ms();
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    let poll =
        |profile: &str, cursor: &Value| json!({"profile":profile,"epoch":epoch,"cursor":cursor});
    // Review probe: an unarchive (and its updatedAt) is not compacted away
    // by a later plain change while C is not polling.
    assert_eq!(publish_to(&hub, &a, one(&task, "archived"), t), Accepted(1));
    step(&hub, &mut t);
    let seen = poll_hub(&hub, poll(&c, &json!(0))).await;
    assert_eq!(seen["events"][0]["kind"], "archived");
    let mut restored = report("local", &task, "unarchived");
    restored["updatedAt"] = json!(1_790_000_000_000u64);
    assert_eq!(publish_to(&hub, &a, json!([restored]), t), Accepted(1));
    step(&hub, &mut t);
    assert_eq!(publish_to(&hub, &a, one(&task, "changed"), t), Accepted(1));
    step(&hub, &mut t);
    let later = poll_hub(&hub, poll(&c, &seen["cursor"])).await;
    assert_eq!(later["reset"], false);
    assert_eq!(
        later["events"],
        json!([{"host":"local","id":task,"kind":"unarchived","seq":3,"origins":[a],"updatedAt":1_790_000_000_000u64}])
    );
    // Review probe: B's change is not hidden from A behind A's own later
    // change of the same thread.
    let before = later["cursor"].clone();
    assert_eq!(
        publish_to(&hub, &b, one(&shared, "changed"), t),
        Accepted(1)
    );
    step(&hub, &mut t);
    let caught = poll_hub(&hub, poll(&a, &before)).await;
    assert_eq!(ids(&caught), [shared.as_str()]);
    let after = caught["cursor"].clone();
    assert_eq!(
        publish_to(&hub, &a, one(&shared, "changed"), t),
        Accepted(1)
    );
    step(&hub, &mut t);
    let check = |hub: &records::Hub, profile: &str, cursor: &Value, expected: &[&str]| {
        let reply = futures_poll(hub, poll(profile, cursor));
        let got: Vec<String> = events_of(&reply)
            .iter()
            .map(|e| e["id"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(got, expected, "{profile} from {cursor}: {reply}");
        reply
    };
    // A missed B's change: it gets the merged event (no one sent both).
    let missed = check(&hub, &a, &before, &[shared.as_str()]);
    assert_eq!(missed["events"][0]["origins"], json!([]));
    // A already had B's change: its own report is not echoed back.
    check(&hub, &a, &after, &[]);
    // B and C hear A's change.
    check(&hub, &b, &after, &[shared.as_str()]);
    check(&hub, &c, &after, &[shared.as_str()]);
    // A keeps streaming: still quiet for A, whichever cursor past B's.
    for _ in 0..2 {
        assert_eq!(
            publish_to(&hub, &a, one(&shared, "changed"), t),
            Accepted(1)
        );
        step(&hub, &mut t);
    }
    check(&hub, &a, &after, &[]);
    check(&hub, &a, &before, &[shared.as_str()]);
    check(&hub, &b, &after, &[shared.as_str()]);
    // The per-cursor suppression is journaled.
    hub.finish(t);
    drop(hub);
    let hub = records::Hub::open(root.path());
    check(&hub, &a, &after, &[]);
    check(&hub, &a, &before, &[shared.as_str()]);
    check(&hub, &b, &after, &[shared.as_str()]);
}

/// `Hub::poll` without a wait never parks: resolve it inline.
fn futures_poll(hub: &records::Hub, args: Value) -> Value {
    let query = records::Query::parse(&args).unwrap();
    let mut future = std::pin::pin!(hub.poll(query));
    let waker = std::task::Waker::noop();
    match future
        .as_mut()
        .poll(&mut std::task::Context::from_waker(waker))
    {
        std::task::Poll::Ready(reply) => reply,
        std::task::Poll::Pending => panic!("a zero-wait poll parked"),
    }
}

#[tokio::test]
async fn full_pending_refuses_publishes_instead_of_evicting() {
    use records::Published::{Accepted, Busy};
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let (a, b, c) = (new_id(), new_id(), new_id());
    let t = records::now_ms();
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    let batch = || -> Value {
        (0..256)
            .map(|_| report("local", &new_id(), "changed"))
            .collect()
    };
    let first = batch();
    assert_eq!(publish_to(&hub, &a, first.clone(), t), Accepted(256));
    for _ in 1..32 {
        assert_eq!(publish_to(&hub, &a, batch(), t), Accepted(256));
    }
    assert_eq!(hub.status()["pending"], 8192);
    // Review probe: new groups are refused whole and cheaply; nothing is
    // evicted, committed early or moved under the floor.
    let refused: Vec<Value> = (0..20).map(|_| batch()).collect();
    let started = Instant::now();
    for events in refused {
        assert_eq!(publish_to(&hub, &a, events, t), Busy);
    }
    assert!(
        started.elapsed() < Duration::from_secs(1),
        "{:?}",
        started.elapsed()
    );
    let mixed = json!([first[1], report("local", &new_id(), "changed")]);
    assert_eq!(publish_to(&hub, &a, mixed, t), Busy);
    let status = hub.status();
    assert_eq!(
        (
            status["pending"].clone(),
            status["head"].clone(),
            status["floor"].clone()
        ),
        (json!(8192), json!(0), json!(0))
    );
    // Reports for groups already pending still merge.
    assert_eq!(publish_to(&hub, &b, json!([first[1]]), t), Accepted(1));
    // A signal file still gets in: the oldest group is committed first.
    let thread = new_id();
    signals(
        root.path(),
        &format!("{b}.desktop.json"),
        "gb",
        json!([[thread, 1]]),
    );
    hub.tick(t + 10);
    assert_eq!(hub.status()["pending"], 8192);
    let early = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":0})).await;
    assert_eq!(ids(&early), [first[0]["id"].as_str().unwrap()]);
    // Through the service, a full hub answers records_busy.
    let service_root = tempdir().unwrap();
    let service = service(service_root.path().into());
    for _ in 0..32 {
        let args = json!({"profile":a,"events":batch()});
        let reply = service.dispatch(request("records.publish", args)).await;
        assert_eq!(reply["result"]["accepted"], 256);
    }
    let args = json!({"profile":a,"events":batch()});
    let reply = service.dispatch(request("records.publish", args)).await;
    assert_eq!(reply["error"]["code"], "records_busy");
}

#[tokio::test]
async fn publishes_share_a_service_wide_budget() {
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let t0 = Instant::now();
    let second = Duration::from_secs(1);
    let mut admitted = 0;
    for _ in 0..11 {
        let mut connection = records::Throttle::connection();
        for _ in 0..records::PUBLISH_REQUESTS {
            admitted += usize::from(hub.admit_publish(&mut connection, 1, t0));
        }
    }
    assert_eq!(admitted, records::GLOBAL_PUBLISH_REQUESTS);
    // Refused by the service-wide window, a connection keeps its own budget.
    let mut fresh = records::Throttle::connection();
    assert!(!hub.admit_publish(&mut fresh, 0, t0 + Duration::from_millis(999)));
    for _ in 0..records::PUBLISH_REQUESTS {
        assert!(hub.admit_publish(&mut fresh, 0, t0 + second));
    }
    assert!(!hub.admit_publish(&mut fresh, 0, t0 + second));
    // Events: four connections' worth, then nothing more in that second.
    let hub = records::Hub::open(root.path());
    let full = records::PUBLISH_EVENTS;
    for _ in 0..records::GLOBAL_PUBLISH_EVENTS / full {
        let mut connection = records::Throttle::connection();
        for chunk in [256; 7].into_iter().chain([full - 7 * 256]) {
            assert!(hub.admit_publish(&mut connection, chunk, t0));
        }
    }
    let mut fresh = records::Throttle::connection();
    assert!(!hub.admit_publish(&mut fresh, 1, t0));
    assert!(hub.admit_publish(&mut fresh, 0, t0));
    assert!(hub.admit_publish(&mut fresh, 256, t0 + second));
}

#[tokio::test]
async fn record_hub_worker_scans_on_its_own_cadence_and_exits_on_close() {
    let root = tempdir().unwrap();
    let hub = Arc::new(records::Hub::open(root.path()));
    let (a, c) = (new_id(), new_id());
    let ticks = || hub.status()["ticks"].as_u64().unwrap();
    hub.start();
    // Nobody has polled yet: the idle cadence scans about once a second.
    tokio::time::sleep(Duration::from_millis(1500)).await;
    let idle = ticks();
    assert!((1..=3).contains(&idle), "{idle}");
    assert_eq!(hub.status()["worker"], true);
    // A poll switches to the 250 ms cadence at once.
    let start = poll_hub(&hub, json!({"profile":c})).await;
    let before = ticks();
    tokio::time::sleep(Duration::from_millis(1200)).await;
    let busy = ticks() - before;
    assert!(busy >= 3, "{busy}");
    // Reports are delivered without a manual tick.
    let thread = new_id();
    signals(
        root.path(),
        &format!("{a}.desktop.json"),
        "ga",
        json!([[thread, 1]]),
    );
    let started = Instant::now();
    let args = json!({"profile":c,"epoch":start["epoch"],"cursor":start["cursor"],"wait_ms":10000});
    let woke = poll_hub(&hub, args).await;
    assert_eq!(ids(&woke), [thread.as_str()]);
    assert!(started.elapsed() < Duration::from_secs(4));
    // Close stops the worker; nothing ticks afterwards.
    hub.close();
    eventually(|| hub.status()["worker"] == false).await;
    let stopped = ticks();
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert_eq!(ticks(), stopped);
    assert_eq!(hub.status()["health"], "ok");
}

#[tokio::test]
async fn record_hub_survives_a_panicking_tick_and_closes_after_repeated_ones() {
    use records::Published::{Accepted, Closing};
    let root = tempdir().unwrap();
    let hub = Arc::new(records::Hub::open(root.path()));
    let (a, b, c) = (new_id(), new_id(), new_id());
    hub.start();
    // Held until the worker's first scan, then a reset.
    let start = poll_hub(&hub, json!({"profile":c,"wait_ms":5000})).await;
    assert_eq!(start["reset"], true);
    let from = |cursor: &Value| json!({"profile":c,"epoch":start["epoch"],"cursor":cursor,"wait_ms":10000});
    // One panicking tick (holding, and poisoning, the state lock): logged,
    // reported as degraded, and the hub keeps delivering.
    hub.inject_panics.store(1, Ordering::SeqCst);
    eventually(|| hub.status()["panics"] == 1).await;
    let status = hub.status();
    assert_eq!(
        (status["health"].clone(), status["worker"].clone()),
        (json!("degraded"), json!(true))
    );
    let thread = new_id();
    let events = one(&thread, "changed");
    assert_eq!(publish_to(&hub, &a, events, records::now_ms()), Accepted(1));
    let woke = poll_hub(&hub, from(&start["cursor"])).await;
    assert_eq!(ids(&woke), [thread.as_str()]);
    // Repeated panics: the hub closes itself, releasing parked polls so
    // every client falls back to its own catch-up.
    let parked = tokio::spawn({
        let (hub, args) = (hub.clone(), from(&woke["cursor"]));
        async move { poll_hub(&hub, args).await }
    });
    tokio::time::sleep(Duration::from_millis(100)).await;
    hub.inject_panics.store(100, Ordering::SeqCst);
    eventually(|| hub.status()["health"] == "failed").await;
    eventually(|| hub.status()["worker"] == false).await;
    assert_eq!(hub.status()["panics"], 4);
    let released = tokio::time::timeout(Duration::from_secs(1), parked)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(released["closing"], true);
    assert_eq!(poll_hub(&hub, from(&woke["cursor"])).await["closing"], true);
    let late = one(&new_id(), "changed");
    assert_eq!(publish_to(&hub, &b, late, records::now_ms()), Closing);
    // A failed hub writes nothing more at shutdown.
    let journal = root.path().join("work/control-center/record-journal.json");
    let saved = std::fs::read(&journal).unwrap();
    hub.finish(records::now_ms() + 60_000);
    assert_eq!(std::fs::read(&journal).unwrap(), saved);
    let log = root
        .path()
        .join("work/control-center/logs/rust-service.jsonl");
    let lines: Vec<Value> = std::fs::read_to_string(log)
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .filter(|line: &Value| line["event"] == "records.hub")
        .collect();
    let states: Vec<&str> = lines.iter().map(|l| l["state"].as_str().unwrap()).collect();
    assert_eq!(
        states,
        ["panicked", "panicked", "panicked", "panicked", "failed"]
    );
    assert_eq!(lines[4]["reason"], "panicked");
}

#[tokio::test]
async fn case_variants_of_a_thread_id_are_one_thread() {
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let (a, b, c) = (new_id(), new_id(), new_id());
    let thread = new_id();
    let mut t = records::now_ms();
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    // One writer reports one change under differently cased ids, by publish
    // and in its file: one group, one event, the writer's origin once.
    let upper = json!([report("ssh:box", &thread.to_uppercase(), "changed")]);
    assert_eq!(
        publish_to(&hub, &a, upper, t),
        records::Published::Accepted(1)
    );
    signals(
        root.path(),
        &format!("{a}.desktop.json"),
        "ga",
        json!([
            [thread.to_uppercase(), 1, "ssh:box", "changed"],
            [thread, 2, "ssh:box", "changed"]
        ]),
    );
    step(&hub, &mut t);
    let seen = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":0})).await;
    assert_eq!(
        seen["events"],
        json!([{"host":"ssh:box","id":thread,"kind":"changed","seq":1,"origins":[a]}])
    );
    let other = poll_hub(&hub, json!({"profile":b,"epoch":epoch,"cursor":0})).await;
    assert_eq!(ids(&other), [thread.as_str()]);
}

#[tokio::test]
async fn pending_and_record_connections_respect_their_own_pools() {
    assert_eq!(records::MAX_CONNECTIONS, 48);
    let root = tempdir().unwrap();
    let (service, name, listening) = records_pipe(root.path()).await;
    let profile = new_id();
    let count = |counter: &AtomicUsize| counter.load(Ordering::SeqCst);
    // Connections without a first request yet (an authority probe, a
    // writer's reconnect loop) are not management clients.
    let mut idle = Vec::new();
    for _ in 0..10 {
        idle.push(pipe_client(&name).await);
    }
    eventually(|| count(&service.pending_clients) == 10).await;
    assert_eq!(count(&service.clients), 0);
    let mut shells = Vec::new();
    for _ in 0..32 {
        let mut shell = pipe_client(&name).await;
        let status = pipe_call(&mut shell, "supervisor.status", json!({})).await;
        assert_eq!(status["ok"], true);
        shells.push(shell);
    }
    assert_eq!(count(&service.clients), 32);
    // A record connection switching to management needs one of the 32
    // general slots; refused, it stays a working record connection.
    let mut writer = pipe_client(&name).await;
    let poll = json!({"profile":profile});
    assert_eq!(
        pipe_call(&mut writer, "records.poll", poll.clone()).await["ok"],
        true
    );
    let refused = pipe_call(&mut writer, "supervisor.status", json!({})).await;
    assert_eq!(refused["error"]["code"], "busy");
    assert_eq!(
        (count(&service.clients), count(&service.record_clients)),
        (32, 1)
    );
    assert_eq!(
        pipe_call(&mut writer, "records.poll", poll.clone()).await["ok"],
        true
    );
    // A 33rd management connection is refused and closed.
    let mut surplus = pipe_client(&name).await;
    let refused = pipe_call(&mut surplus, "supervisor.status", json!({})).await;
    assert_eq!(refused["error"]["code"], "busy");
    eventually(|| count(&service.pending_clients) == 10).await;
    // Once a slot frees, the switch succeeds.
    drop(shells.pop());
    eventually(|| count(&service.clients) == 31).await;
    let status = pipe_call(&mut writer, "supervisor.status", json!({})).await;
    let result = &status["result"];
    assert_eq!(
        (
            result["clients"].clone(),
            result["record_clients"].clone(),
            result["pending_clients"].clone(),
            result["records"]["health"].clone()
        ),
        (json!(32), json!(0), json!(10), json!("ok"))
    );
    drop((shells, writer, surplus));
    eventually(|| count(&service.clients) == 0).await;
    // Still-silent connections do not hold retirement.
    retire_pipe(&name, listening).await;
    drop(idle);
}

#[tokio::test]
async fn writers_keep_four_generations_across_restarts() {
    let root = tempdir().unwrap();
    let (a, c) = (new_id(), new_id());
    let desktop = format!("{a}.desktop.json");
    let hub = records::Hub::open(root.path());
    let mut t = records::now_ms();
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    let threads: Vec<String> = (0..5).map(|_| new_id()).collect();
    for (n, thread) in threads.iter().enumerate() {
        signals(
            root.path(),
            &desktop,
            &format!("g{n}"),
            json!([[thread, 1]]),
        );
        settle(&hub, &mut t);
    }
    assert_eq!(hub.status()["head"], 5);
    // A remembered generation (g1..g4) replays nothing, also after a restart.
    signals(root.path(), &desktop, "g1", json!([[threads[1], 1]]));
    settle(&hub, &mut t);
    assert_eq!(hub.status()["head"], 5);
    hub.finish(t);
    drop(hub);
    let hub = records::Hub::open(root.path());
    settle(&hub, &mut t);
    signals(root.path(), &desktop, "g4", json!([[threads[4], 1]]));
    settle(&hub, &mut t);
    assert_eq!(hub.status()["head"], 5);
    // g0 was evicted by the fifth generation: its entries are new again.
    signals(root.path(), &desktop, "g0", json!([[threads[0], 1]]));
    settle(&hub, &mut t);
    let replayed = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":5})).await;
    assert_eq!(ids(&replayed), [threads[0].as_str()]);
}

#[tokio::test]
async fn newest_signal_files_win_over_stale_ones_beyond_the_file_cap() {
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let c = new_id();
    let mut t = records::now_ms();
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    // 140 files of removed profiles, untouched for an hour, whose names sort
    // (and so list) before the live writer's.
    let directory = root.path().join("work/control-center/record-signals");
    std::fs::create_dir_all(&directory).unwrap();
    let hour_ago = std::time::SystemTime::now() - Duration::from_secs(3600);
    let stale = json!({"version":2,"generation":"old","changes":[]}).to_string();
    for n in 0..140 {
        let path = directory.join(format!("00000000-0000-4000-8000-{n:012}.desktop.json"));
        std::fs::write(&path, &stale).unwrap();
        std::fs::File::options()
            .write(true)
            .open(&path)
            .unwrap()
            .set_modified(hour_ago)
            .unwrap();
    }
    let live = "ffffffff-ffff-4fff-bfff-ffffffffffff";
    let thread = new_id();
    signals(
        root.path(),
        &format!("{live}.desktop.json"),
        "g",
        json!([[thread, 1]]),
    );
    settle(&hub, &mut t);
    let seen = poll_hub(&hub, json!({"profile":c,"epoch":epoch,"cursor":0})).await;
    assert_eq!(ids(&seen), [thread.as_str()]);
    assert_eq!(seen["events"][0]["origins"], json!([live]));
    assert_eq!(hub.status()["writers"], 128);
}

/// One 3 s window of `streamer` streaming a turn on a local `thread`: a
/// publish every 400 ms and its runtime proxy's file every 800 ms, so the
/// group only commits at the 3 s cap, with the streamer in it. A peer's
/// report lands mid-window. Returns once the window's event is deliverable.
fn stream_window(
    hub: &records::Hub,
    root: &std::path::Path,
    streamer: &str,
    thread: &str,
    t: &mut u64,
    seq: &mut u64,
    peer: Option<(&str, &str)>,
) {
    use records::Published::Accepted;
    for step in 0..8u64 {
        let now = *t + 400 * step;
        let own = json!([report("local", thread, "changed")]);
        assert_eq!(publish_to(hub, streamer, own, now), Accepted(1));
        if step % 2 == 0 {
            *seq += 1;
            let proxy = format!("{streamer}.json");
            signals(root, &proxy, "proxy", json!([[thread, *seq]]));
        }
        if step == 4
            && let Some((profile, kind)) = peer
        {
            let theirs = json!([report("local", thread, kind)]);
            assert_eq!(publish_to(hub, profile, theirs, now), Accepted(1));
        }
        hub.tick(now);
        assert_eq!(hub.status()["pending"], 1);
    }
    hub.tick(*t + 3000);
    assert_eq!(hub.status()["pending"], 0);
    hub.tick(*t + 6100);
    *t += 7000;
}

/// The kinds `profile` receives from `cursor` on; advances the cursor.
async fn heard(
    hub: &records::Hub,
    profile: &str,
    epoch: &Value,
    cursor: &mut Value,
) -> Vec<String> {
    let reply = poll_hub(
        hub,
        json!({"profile":profile,"epoch":epoch,"cursor":cursor}),
    )
    .await;
    assert_eq!(reply["reset"], false);
    *cursor = reply["cursor"].clone();
    reply["events"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| e["kind"].as_str().unwrap().to_string())
        .collect()
}

#[tokio::test]
async fn a_peer_change_during_a_local_stream_reaches_the_streaming_profile() {
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let (a, b, c) = (new_id(), new_id(), new_id());
    let thread = new_id();
    let (mut t, mut seq) = (records::now_ms(), 0);
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":a})).await["epoch"].clone();
    let (mut at_a, mut at_b, mut at_c) = (json!(0), json!(0), json!(0));
    let path = root.path();
    // A streams alone: A never hears its own stream back; B and C do.
    stream_window(&hub, path, &a, &thread, &mut t, &mut seq, None);
    assert!(heard(&hub, &a, &epoch, &mut at_a).await.is_empty());
    assert_eq!(heard(&hub, &b, &epoch, &mut at_b).await, ["changed"]);
    assert_eq!(heard(&hub, &c, &epoch, &mut at_c).await, ["changed"]);
    // B renames the thread mid-stream: two local changes, so A is told.
    stream_window(
        &hub,
        path,
        &a,
        &thread,
        &mut t,
        &mut seq,
        Some((&b, "changed")),
    );
    assert_eq!(heard(&hub, &a, &epoch, &mut at_a).await, ["changed"]);
    assert_eq!(heard(&hub, &b, &epoch, &mut at_b).await, ["changed"]);
    assert_eq!(heard(&hub, &c, &epoch, &mut at_c).await, ["changed"]);
    // A streams alone again, already told of B's rename: no echo to A.
    stream_window(&hub, path, &a, &thread, &mut t, &mut seq, None);
    assert!(heard(&hub, &a, &epoch, &mut at_a).await.is_empty());
    assert_eq!(heard(&hub, &b, &epoch, &mut at_b).await, ["changed"]);
    // B archives it mid-stream: A's later changes do not hide the archive.
    stream_window(
        &hub,
        path,
        &a,
        &thread,
        &mut t,
        &mut seq,
        Some((&b, "archived")),
    );
    assert_eq!(heard(&hub, &a, &epoch, &mut at_a).await, ["archived"]);
    assert!(heard(&hub, &b, &epoch, &mut at_b).await.is_empty());
    // C skipped window 3: compacted per thread, it gets the archive alone.
    assert_eq!(heard(&hub, &c, &epoch, &mut at_c).await, ["archived"]);
    // B deletes it mid-stream: A is told, and it stays deleted.
    stream_window(
        &hub,
        path,
        &a,
        &thread,
        &mut t,
        &mut seq,
        Some((&b, "deleted")),
    );
    assert_eq!(heard(&hub, &a, &epoch, &mut at_a).await, ["deleted"]);
    assert!(heard(&hub, &b, &epoch, &mut at_b).await.is_empty());
    stream_window(&hub, path, &a, &thread, &mut t, &mut seq, None);
    assert!(heard(&hub, &a, &epoch, &mut at_a).await.is_empty());
    assert_eq!(heard(&hub, &c, &epoch, &mut at_c).await, ["deleted"]);
}

// Second-round review: unarchives and shared hosts, a new epoch's first
// scan, volatile journals, reset storms and journal pacing.

/// `id:kind:origins` of what `profile` receives from `cursor` on.
fn received(hub: &records::Hub, profile: &str, epoch: &Value, cursor: &Value) -> Vec<String> {
    let reply = futures_poll(
        hub,
        json!({"profile":profile,"epoch":epoch,"cursor":cursor}),
    );
    assert_eq!(reply["reset"], false, "{reply}");
    events_of(&reply)
        .iter()
        .map(|e| {
            format!(
                "{}:{}:{}",
                e["id"].as_str().unwrap(),
                e["kind"].as_str().unwrap(),
                e["origins"]
            )
        })
        .collect()
}

#[tokio::test]
async fn unarchives_and_shared_hosts_never_hide_a_peers_change() {
    use records::Published::Accepted;
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let (a, b, c) = (new_id(), new_id(), new_id());
    let (x, y, z, remote) = (new_id(), new_id(), new_id(), new_id());
    let mut t = records::now_ms();
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    let latest = |hub: &records::Hub| {
        futures_poll(hub, json!({"profile":a,"epoch":epoch,"cursor":0}))["cursor"].clone()
    };
    // Review probe: once unarchived, an adapter reports every later change
    // as `unarchived`. That supersedes nothing: B's change still reaches A,
    // across events and inside one window, in either order.
    assert_eq!(publish_to(&hub, &b, one(&x, "changed"), t), Accepted(1));
    step(&hub, &mut t);
    assert_eq!(publish_to(&hub, &a, one(&x, "unarchived"), t), Accepted(1));
    step(&hub, &mut t);
    assert_eq!(
        received(&hub, &a, &epoch, &json!(0)),
        [format!("{x}:unarchived:[]")]
    );
    let cursor = latest(&hub);
    assert_eq!(publish_to(&hub, &b, one(&y, "changed"), t), Accepted(1));
    assert_eq!(publish_to(&hub, &a, one(&y, "unarchived"), t), Accepted(1));
    step(&hub, &mut t);
    assert_eq!(
        received(&hub, &a, &epoch, &cursor),
        [format!("{y}:unarchived:[]")]
    );
    let cursor = latest(&hub);
    assert_eq!(publish_to(&hub, &a, one(&z, "unarchived"), t), Accepted(1));
    assert_eq!(publish_to(&hub, &b, one(&z, "changed"), t), Accepted(1));
    step(&hub, &mut t);
    assert_eq!(
        received(&hub, &a, &epoch, &cursor),
        [format!("{z}:unarchived:[]")]
    );
    // Review probe: two profiles' changes to one SSH thread. A shared host
    // broadcasts starts and renames to every connection but turns only to
    // subscribers, so neither is taken to know the other's change.
    let cursor = latest(&hub);
    let shared = json!([report("ssh:host", &remote, "changed")]);
    assert_eq!(publish_to(&hub, &a, shared.clone(), t), Accepted(1));
    assert_eq!(publish_to(&hub, &b, shared, t), Accepted(1));
    step(&hub, &mut t);
    for profile in [&a, &b, &c] {
        assert_eq!(
            received(&hub, profile, &epoch, &cursor),
            [format!("{remote}:changed:[]")]
        );
    }
    // A deletion or archive still supersedes: its reporter alone is skipped.
    let cursor = latest(&hub);
    assert_eq!(publish_to(&hub, &b, one(&x, "changed"), t), Accepted(1));
    assert_eq!(publish_to(&hub, &a, one(&x, "archived"), t), Accepted(1));
    step(&hub, &mut t);
    assert!(received(&hub, &a, &epoch, &cursor).is_empty());
    assert_eq!(
        received(&hub, &b, &epoch, &cursor),
        [format!("{x}:archived:[\"{a}\"]")]
    );
}

#[tokio::test]
async fn a_new_epoch_answers_polls_only_after_adopting_the_writers_files() {
    let root = tempdir().unwrap();
    let hub = Arc::new(records::Hub::open(root.path()));
    let (w, c) = (new_id(), new_id());
    let (retained, fresh) = (new_id(), new_id());
    let proxy = format!("{w}.json");
    // Review probe: before the first scan there is no reset at cursor 0
    // (the client's catch-up would run before the scan adopts what is
    // written meanwhile): an immediate poll hears `closing`...
    let early = poll_hub(&hub, json!({"profile":c})).await;
    assert_eq!(
        (early["closing"].clone(), early["reset"].clone()),
        (json!(true), json!(false))
    );
    // ...and a long poll is held until the scan.
    let held = tokio::spawn({
        let hub = hub.clone();
        let args = json!({"profile":c,"wait_ms":5000});
        async move { poll_hub(&hub, args).await }
    });
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(!held.is_finished());
    let adopted = json!([[retained, 1, "local", "changed"]]);
    signals(root.path(), &proxy, "g1", adopted);
    let mut t = records::now_ms();
    hub.tick(t);
    let start = tokio::time::timeout(Duration::from_secs(1), held)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        (start["reset"].clone(), start["cursor"].clone()),
        (json!(true), json!(0))
    );
    // The client's catch-up starts after the scan and reads the adopted
    // entry itself; everything written later is delivered.
    let both = json!([
        [retained, 1, "local", "changed"],
        [fresh, 2, "local", "changed"]
    ]);
    signals(root.path(), &proxy, "g1", both);
    settle(&hub, &mut t);
    let args = json!({"profile":c,"epoch":start["epoch"],"cursor":0});
    assert_eq!(ids(&poll_hub(&hub, args).await), [fresh.as_str()]);
}

#[tokio::test]
async fn a_journal_written_while_volatile_restarts_under_a_new_epoch() {
    let root = tempdir().unwrap();
    let journal = root.path().join("work/control-center/record-journal.json");
    let (a, c) = (new_id(), new_id());
    let (first, second) = (new_id(), new_id());
    let desktop = format!("{a}.desktop.json");
    let read = |path: &std::path::Path| -> Value {
        serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
    };
    // Journal writes fail (a directory in its place): delivery continues
    // under a volatile epoch, ahead of anything on disk.
    std::fs::create_dir_all(&journal).unwrap();
    let hub = records::Hub::open(root.path());
    let mut t = records::now_ms();
    hub.tick(t);
    signals(root.path(), &desktop, "ga", json!([[first, 1]]));
    settle(&hub, &mut t);
    assert_eq!(hub.status()["volatile"], true);
    let before = poll_hub(&hub, json!({"profile":c})).await;
    // Writes work again; the first journal written is still marked.
    std::fs::remove_dir(&journal).unwrap();
    signals(
        root.path(),
        &desktop,
        "ga",
        json!([[first, 1], [second, 2]]),
    );
    settle(&hub, &mut t);
    assert_eq!(read(&journal)["volatile"], true);
    assert_eq!(hub.status()["volatile"], false);
    let args = json!({"profile":c,"epoch":before["epoch"],"cursor":before["cursor"]});
    let cursor = poll_hub(&hub, args).await["cursor"].clone();
    // A crash now: the marked journal never resumes its epoch (delivery may
    // have run past its head), so every client resets.
    drop(hub);
    let hub = records::Hub::open(root.path());
    hub.tick(t);
    let args = json!({"profile":c,"epoch":before["epoch"],"cursor":cursor});
    let reply = poll_hub(&hub, args).await;
    assert_eq!(reply["reset"], true);
    assert_ne!(reply["epoch"], before["epoch"]);
    // Its replacement is written unmarked and resumes normally.
    let written = read(&journal);
    assert!(written.get("volatile").is_none(), "{written}");
    assert_eq!(written["epoch"], reply["epoch"]);
    drop(hub);
    let hub = records::Hub::open(root.path());
    hub.tick(t);
    let args = json!({"profile":c,"epoch":reply["epoch"],"cursor":reply["cursor"]});
    assert_eq!(poll_hub(&hub, args).await["reset"], false);
    let log = root
        .path()
        .join("work/control-center/logs/rust-service.jsonl");
    let log = std::fs::read_to_string(log).unwrap();
    assert!(log.contains(r#""state":"rotated""#), "{log}");
}

#[tokio::test]
async fn resets_wait_for_an_evicting_burst_to_be_journaled() {
    let root = tempdir().unwrap();
    let journal = root.path().join("work/control-center/record-journal.json");
    let hub = Arc::new(records::Hub::open(root.path()));
    let (a, b, c) = (new_id(), new_id(), new_id());
    let (desktop_a, desktop_b) = (format!("{a}.desktop.json"), format!("{b}.desktop.json"));
    let mut t = records::now_ms();
    hub.tick(t);
    let epoch = poll_hub(&hub, json!({"profile":c})).await["epoch"].clone();
    signals(root.path(), &desktop_a, "ga", json!([[new_id(), 1]]));
    settle(&hub, &mut t);
    assert_eq!(hub.status()["cursor"], 1);
    // A burst of 4100 threads evicts undelivered ones, and its journal write
    // fails once: the floor stays above delivery.
    std::fs::remove_file(&journal).unwrap();
    std::fs::create_dir(&journal).unwrap();
    let burst = |count: usize| -> Value { (1..=count).map(|seq| json!([new_id(), seq])).collect() };
    signals(root.path(), &desktop_a, "ga2", burst(2100));
    signals(root.path(), &desktop_b, "gb", burst(2000));
    hub.tick(t);
    hub.tick(t + 1100);
    let status = hub.status();
    assert_eq!(
        (
            status["floor"].clone(),
            status["cursor"].clone(),
            status["volatile"].clone()
        ),
        (json!(5), json!(1), json!(false))
    );
    // Answered now, a reset would only reset again (its cursor is under the
    // floor): an immediate poll still gets it, a waiting one is held.
    let stale = json!({"profile":c,"epoch":epoch,"cursor":1});
    assert_eq!(poll_hub(&hub, stale.clone()).await["cursor"], 1);
    let held = tokio::spawn({
        let (hub, mut args) = (hub.clone(), stale);
        args["wait_ms"] = json!(5000);
        async move { poll_hub(&hub, args).await }
    });
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(!held.is_finished());
    std::fs::remove_dir(&journal).unwrap();
    hub.tick(t + 1200);
    let reset = tokio::time::timeout(Duration::from_secs(1), held)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(
        (reset["reset"].clone(), reset["cursor"].clone()),
        (json!(true), json!(4101))
    );
    let args = json!({"profile":c,"epoch":epoch,"cursor":4101});
    assert_eq!(poll_hub(&hub, args).await["reset"], false);
}

#[tokio::test]
async fn plain_change_streams_are_journaled_every_3_s_and_visibility_within_1_s() {
    use records::Published::Accepted;
    let root = tempdir().unwrap();
    let hub = records::Hub::open(root.path());
    let (a, c) = (new_id(), new_id());
    let (stream, hidden) = (new_id(), new_id());
    let t = records::now_ms();
    hub.tick(t);
    poll_hub(&hub, json!({"profile":c})).await;
    let delivered = || hub.status()["cursor"].as_u64().unwrap();
    // After a quiet spell a commit is journaled (and deliverable) at once.
    assert_eq!(
        publish_to(&hub, &a, one(&stream, "changed"), t),
        Accepted(1)
    );
    hub.tick(t + 800);
    assert_eq!(delivered(), 1);
    // A stream of plain changes: at most one journal write every 3 s.
    let again = one(&stream, "changed");
    assert_eq!(publish_to(&hub, &a, again, t + 1000), Accepted(1));
    hub.tick(t + 1800);
    hub.tick(t + 3700);
    assert_eq!((hub.status()["head"].clone(), delivered()), (json!(2), 1));
    hub.tick(t + 3800);
    assert_eq!(delivered(), 2);
    // A deletion or visibility change goes out within a second.
    let archived = one(&hidden, "archived");
    assert_eq!(publish_to(&hub, &a, archived, t + 4000), Accepted(1));
    hub.tick(t + 4800);
    assert_eq!(delivered(), 3);
}
