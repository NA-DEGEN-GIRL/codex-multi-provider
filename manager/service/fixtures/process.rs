#![windows_subsystem = "windows"]
use std::{
    os::windows::process::CommandExt,
    time::{Duration, Instant},
};
fn main() {
    let args: Vec<_> = std::env::args().collect();
    let path = std::env::var("CODEX_PROCESS_FIXTURE_DIRECTORY")
        .expect("isolated fixture directory required");
    let path = std::path::PathBuf::from(path);
    assert!(path.starts_with(std::env::temp_dir()));
    let child = args.iter().any(|v| v == "--child");
    let mut descendant = None;
    if !child {
        let process = std::process::Command::new(std::env::current_exe().unwrap())
            .arg("--child")
            .creation_flags(0x08000000)
            .spawn()
            .unwrap();
        std::fs::write(path.join("child.pid"), process.id().to_string()).unwrap();
        descendant = Some(process);
    }
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(20) && !path.join("exit").exists() {
        std::thread::sleep(Duration::from_millis(25));
    }
    if let Some(mut process) = descendant {
        let _ = process.wait();
    }
}
