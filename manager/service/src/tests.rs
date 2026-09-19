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

#[test]
fn forked_conversations_copy_notes_once_and_edit_independently() {
    let root = tempdir().unwrap();
    let parent = uuid::Uuid::new_v4().to_string();
    let child = uuid::Uuid::new_v4().to_string();
    let task = |thread: &str| json!({"task":{"host_id":"local","thread_id":thread}});
    let mut args = note_args();
    args["task"]["thread_id"] = json!(parent);
    args["body"] = json!("parent body");
    args["items"][0]["done"] = json!(true);
    notes::execute(root.path(), "notes.save", &args).unwrap();
    let parent_list = notes::execute(root.path(), "notes.list", &task(&parent)).unwrap();

    let forks = root.path().join("work/control-center/note-forks.json");
    std::fs::create_dir_all(forks.parent().unwrap()).unwrap();
    let mut mapping = serde_json::Map::new();
    mapping.insert(child.clone(), json!(parent));
    std::fs::write(&forks, Value::Object(mapping).to_string()).unwrap();

    let child_list = notes::execute(root.path(), "notes.list", &task(&child)).unwrap();
    assert_eq!(child_list["notes"][0]["title"], args["title"]);
    assert_eq!(child_list["notes"][0]["body"], "parent body");
    assert_eq!(child_list["notes"][0]["items"][0]["done"], true);
    assert_ne!(child_list["notes"][0]["id"], parent_list["notes"][0]["id"]);
    assert_ne!(
        child_list["notes"][0]["items"][0]["id"],
        parent_list["notes"][0]["items"][0]["id"]
    );
    assert_eq!(child_list["notes"][0]["revision"], 0);
    assert_eq!(child_list["shared"], false);
    assert_eq!(
        notes::execute(root.path(), "notes.list", &task(&child)).unwrap(),
        child_list
    );
    assert_eq!(
        notes::execute(root.path(), "notes.list", &task(&parent)).unwrap(),
        parent_list
    );

    // The child has its own document immediately, without a separate split.
    let mut edit = task(&child);
    edit["note_id"] = child_list["notes"][0]["id"].clone();
    edit["revision"] = child_list["notes"][0]["revision"].clone();
    edit["title"] = json!("child title");
    edit["kind"] = json!("text");
    edit["body"] = json!("child edit");
    edit["items"] = json!([]);
    notes::execute(root.path(), "notes.save", &edit).unwrap();
    assert_eq!(
        notes::execute(root.path(), "notes.list", &task(&parent)).unwrap(),
        parent_list
    );
    args["revision"] = json!(1);
    args["body"] = json!("parent changed");
    notes::execute(root.path(), "notes.save", &args).unwrap();
    let child_after = notes::execute(root.path(), "notes.list", &task(&child)).unwrap();
    assert_eq!(child_after["notes"][0]["body"], "child edit");
}

fn note_fork_map(root: &std::path::Path, mapping: Value) {
    let path = root.join("work/control-center/note-forks.json");
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, mapping.to_string()).unwrap();
}

#[test]
fn nested_forks_materialize_immediate_parent_and_preserve_existing_child() {
    let root = tempdir().unwrap();
    let mut parent = note_args();
    parent["body"] = json!("original");
    let child = note_args();
    let grandchild = note_args();
    let parent_id = parent["task"]["thread_id"].as_str().unwrap().to_string();
    let child_id = child["task"]["thread_id"].as_str().unwrap();
    let grandchild_id = grandchild["task"]["thread_id"].as_str().unwrap();
    note_fork_map(
        root.path(),
        json!({child_id: parent_id, grandchild_id: child_id}),
    );
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    let leaf = notes::execute(root.path(), "notes.list", &grandchild).unwrap();
    let middle = notes::execute(root.path(), "notes.list", &child).unwrap();
    assert_eq!(leaf["notes"][0]["body"], "original");
    assert_eq!(middle["notes"][0]["body"], "original");
    assert_ne!(leaf["notes"][0]["id"], middle["notes"][0]["id"]);
    parent["revision"] = json!(1);
    parent["body"] = json!("later parent edit");
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    assert_eq!(
        notes::execute(root.path(), "notes.list", &grandchild).unwrap(),
        leaf
    );
    assert_eq!(
        notes::execute(root.path(), "notes.list", &child).unwrap(),
        middle
    );

    let existing = note_args();
    notes::execute(root.path(), "notes.save", &existing).unwrap();
    let before = notes::execute(root.path(), "notes.list", &existing).unwrap();
    note_fork_map(
        root.path(),
        json!({existing["task"]["thread_id"].as_str().unwrap(): parent_id}),
    );
    assert_eq!(
        notes::execute(root.path(), "notes.list", &existing).unwrap(),
        before
    );
}

#[test]
fn empty_fork_snapshot_stays_empty_and_other_hosts_do_not_inherit() {
    let root = tempdir().unwrap();
    let parent = note_args();
    let child = note_args();
    let parent_id = parent["task"]["thread_id"].as_str().unwrap();
    let child_id = child["task"]["thread_id"].as_str().unwrap();
    note_fork_map(root.path(), json!({child_id: parent_id}));
    assert_eq!(
        notes::execute(root.path(), "notes.list", &child).unwrap()["notes"],
        json!([])
    );
    notes::execute(root.path(), "notes.save", &parent).unwrap();
    assert_eq!(
        notes::execute(root.path(), "notes.list", &child).unwrap()["notes"],
        json!([])
    );

    let mut remote_parent = parent.clone();
    remote_parent["task"]["host_id"] = json!("ssh:other-host");
    notes::execute(root.path(), "notes.save", &remote_parent).unwrap();
    let mut remote_child = child.clone();
    remote_child["task"]["host_id"] = json!("ssh:other-host");
    assert_eq!(
        notes::execute(root.path(), "notes.list", &remote_child).unwrap()["notes"],
        json!([])
    );
}

#[test]
fn fork_cycles_fail_without_creating_empty_snapshots() {
    let root = tempdir().unwrap();
    let a = note_args();
    let b = note_args();
    note_fork_map(
        root.path(),
        json!({a["task"]["thread_id"].as_str().unwrap(): b["task"]["thread_id"],
                                   b["task"]["thread_id"].as_str().unwrap(): a["task"]["thread_id"]}),
    );
    assert!(notes::execute(root.path(), "notes.list", &a).is_err());
    assert!(!root.path().join("work/control-center/notes").exists());
}

#[test]
fn legacy_shared_group_can_be_copied_and_split_without_changing_source() {
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
    let mut document: Value = serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
    let group = uuid::Uuid::new_v4().to_string();
    let group_directory = root.path().join("work/control-center/note-groups");
    std::fs::create_dir_all(&group_directory).unwrap();
    let group_path = group_directory.join(format!("{group}.json"));
    let data = json!({"version":1,"group_id":group,"notes":document["notes"]});
    std::fs::write(&group_path, data.to_string()).unwrap();
    document["version"] = json!(2);
    document["group_id"] = json!(group);
    document["notes"] = json!([]);
    std::fs::write(&path, document.to_string()).unwrap();
    let parent_bytes = std::fs::read(&path).unwrap();
    let group_bytes = std::fs::read(&group_path).unwrap();

    let child = note_args();
    note_fork_map(
        root.path(),
        json!({child["task"]["thread_id"].as_str().unwrap(): parent["task"]["thread_id"]}),
    );
    let cloned = notes::execute(root.path(), "notes.list", &child).unwrap();
    assert_eq!(cloned["notes"][0]["title"], parent["title"]);
    assert_eq!(cloned["shared"], false);
    assert_eq!(std::fs::read(&path).unwrap(), parent_bytes);
    assert_eq!(std::fs::read(&group_path).unwrap(), group_bytes);

    // The explicit split command still detaches old shared documents.
    let split = notes::execute(root.path(), "notes.fork", &parent).unwrap();
    assert_ne!(split["notes"][0]["id"], data["notes"][0]["id"]);
    assert_eq!(
        notes::execute(root.path(), "notes.list", &parent).unwrap()["shared"],
        false
    );
    assert_eq!(std::fs::read(&group_path).unwrap(), group_bytes);
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
async fn first_note_access_resolves_native_fork_before_copying() {
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
    let copied = service.dispatch(request("notes.list", child.clone())).await;
    assert_eq!(copied["ok"], true);
    assert_eq!(copied["result"]["notes"][0]["title"], parent["title"]);
    assert_ne!(copied["result"]["notes"][0]["id"], parent["note_id"]);
    assert_eq!(copied["result"]["shared"], false);
    let repeated = service.dispatch(request("notes.list", child)).await;
    assert_eq!(copied["result"], repeated["result"]);
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
