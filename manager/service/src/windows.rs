use sha2::{Digest, Sha256};
use std::{io, mem::size_of, os::windows::io::AsRawHandle, path::Path, ptr};
use tokio::net::windows::named_pipe::{NamedPipeServer, ServerOptions};
use windows_sys::Win32::{
    Foundation::*,
    Security::{Authorization::*, *},
    System::{Pipes::GetNamedPipeClientProcessId, Threading::*},
};

pub fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(Some(0)).collect()
}
pub struct Handle(pub HANDLE);
unsafe impl Send for Handle {}
unsafe impl Sync for Handle {}
impl Drop for Handle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
struct ExecutionAuthority {
    sid: String,
    elevated: bool,
}

fn authority(process: HANDLE) -> io::Result<ExecutionAuthority> {
    unsafe {
        let mut raw = ptr::null_mut();
        if OpenProcessToken(process, TOKEN_QUERY, &mut raw) == 0 {
            return Err(io::Error::last_os_error());
        }
        let token = Handle(raw);
        let mut elevation = TOKEN_ELEVATION { TokenIsElevated: 0 };
        let mut returned = 0;
        if GetTokenInformation(
            token.0,
            TokenElevation,
            (&mut elevation as *mut TOKEN_ELEVATION).cast(),
            size_of::<TOKEN_ELEVATION>() as u32,
            &mut returned,
        ) == 0
        {
            return Err(io::Error::last_os_error());
        }
        let mut size = 0;
        GetTokenInformation(token.0, TokenUser, ptr::null_mut(), 0, &mut size);
        if size < size_of::<TOKEN_USER>() as u32 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "Missing token user",
            ));
        }
        let mut bytes = vec![0u64; (size as usize).div_ceil(8)];
        if GetTokenInformation(
            token.0,
            TokenUser,
            bytes.as_mut_ptr().cast(),
            size,
            &mut size,
        ) == 0
        {
            return Err(io::Error::last_os_error());
        }
        let user = &*(bytes.as_ptr().cast::<TOKEN_USER>());
        let mut text = ptr::null_mut();
        if ConvertSidToStringSidW(user.User.Sid, &mut text) == 0 {
            return Err(io::Error::last_os_error());
        }
        let mut len = 0;
        while *text.add(len) != 0 {
            len += 1;
        }
        let result = String::from_utf16_lossy(std::slice::from_raw_parts(text, len));
        LocalFree(text.cast());
        Ok(ExecutionAuthority {
            sid: result,
            elevated: elevation.TokenIsElevated != 0,
        })
    }
}

pub fn sid() -> io::Result<String> {
    Ok(authority(unsafe { GetCurrentProcess() })?.sid)
}

fn require_matching_authority(
    expected: &ExecutionAuthority,
    peer: &ExecutionAuthority,
) -> io::Result<()> {
    if expected == peer {
        Ok(())
    } else {
        Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "Workspace execution authority differs",
        ))
    }
}

// Check on the server too: an older/raw client can bypass ManagerClient's
// preflight. Never read or dispatch its first RPC until the OS proves that
// both processes belong to the same user and use the same elevation.
// Returns the client PID, for content-free connection logging only.
pub fn require_client_authority(pipe: &NamedPipeServer) -> io::Result<u32> {
    unsafe {
        let mut client_id = 0;
        if GetNamedPipeClientProcessId(pipe.as_raw_handle(), &mut client_id) == 0 {
            return Err(io::Error::last_os_error());
        }
        let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, client_id);
        if process.is_null() {
            return Err(io::Error::last_os_error());
        }
        let process = Handle(process);
        require_matching_authority(&authority(GetCurrentProcess())?, &authority(process.0)?)?;
        Ok(client_id)
    }
}

fn server_sddl(authority: &ExecutionAuthority) -> String {
    // Explicit no-write-up protection also covers an impersonating caller.
    // The per-user DACL alone does not distinguish a user's two UAC tokens.
    let integrity = if authority.elevated { "HI" } else { "ME" };
    format!("D:P(A;;GA;;;{})S:(ML;;NW;;;{integrity})", authority.sid)
}

pub fn pipe_name(root: &Path) -> io::Result<String> {
    let root = root
        .to_string_lossy()
        .trim_end_matches(['\\', '/'])
        .to_uppercase();
    let hash = format!(
        "{:X}",
        Sha256::digest(format!("{}\n{root}", sid()?).as_bytes())
    );
    Ok(format!("CodexControlCenter.service.{}", &hash[..32]))
}

pub fn server(name: &str, first: bool) -> io::Result<NamedPipeServer> {
    unsafe {
        let sddl = wide(&server_sddl(&authority(GetCurrentProcess())?));
        let mut security = ptr::null_mut();
        if ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.as_ptr(),
            1,
            &mut security,
            ptr::null_mut(),
        ) == 0
        {
            return Err(io::Error::last_os_error());
        }
        let attributes = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: security,
            bInheritHandle: 0,
        };
        let result = ServerOptions::new()
            .first_pipe_instance(first)
            .reject_remote_clients(true)
            .create_with_security_attributes_raw(
                format!(r"\\.\pipe\{name}"),
                (&attributes as *const SECURITY_ATTRIBUTES)
                    .cast_mut()
                    .cast(),
            );
        LocalFree(security);
        result
    }
}

#[cfg(test)]
mod execution_authority_tests {
    use super::*;

    #[test]
    fn token_probes_and_mismatches_fail_closed() {
        let current = authority(unsafe { GetCurrentProcess() }).unwrap();
        assert_eq!(sid().unwrap(), current.sid);
        require_matching_authority(&current, &current).unwrap();
        assert!(authority(ptr::null_mut()).is_err());
        let different_mode = ExecutionAuthority {
            sid: current.sid.clone(),
            elevated: !current.elevated,
        };
        assert_eq!(
            require_matching_authority(&current, &different_mode)
                .unwrap_err()
                .kind(),
            io::ErrorKind::PermissionDenied
        );
        let different_user = ExecutionAuthority {
            sid: "S-1-5-18".into(),
            elevated: current.elevated,
        };
        if current.sid != different_user.sid {
            assert!(require_matching_authority(&current, &different_user).is_err());
        }
        for elevated in [false, true] {
            let expected = if elevated { "HI" } else { "ME" };
            let descriptor = server_sddl(&ExecutionAuthority {
                sid: current.sid.clone(),
                elevated,
            });
            assert_eq!(
                descriptor,
                format!("D:P(A;;GA;;;{})S:(ML;;NW;;;{expected})", current.sid)
            );
        }
    }

    #[tokio::test]
    async fn actual_pipe_client_is_probed_without_a_live_workspace() {
        let name = format!("CodexControlCenter.authority-test.{}", uuid::Uuid::new_v4());
        let pipe = server(&name, true).unwrap();
        assert!(require_client_authority(&pipe).is_err());
        let _client = tokio::net::windows::named_pipe::ClientOptions::new()
            .open(format!(r"\\.\pipe\{name}"))
            .unwrap();
        tokio::time::timeout(std::time::Duration::from_secs(3), pipe.connect())
            .await
            .unwrap()
            .unwrap();
        require_client_authority(&pipe).unwrap();
    }
}

pub fn atomic(path: &Path, bytes: &[u8]) -> io::Result<()> {
    use std::io::Write;
    std::fs::create_dir_all(path.parent().ok_or(io::ErrorKind::InvalidInput)?)?;
    let temporary = path.with_extension(format!("{}.tmp", uuid::Uuid::new_v4()));
    let result = (|| {
        let mut file = std::fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        drop(file);
        unsafe {
            use windows_sys::Win32::Storage::FileSystem::{
                MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH, MoveFileExW,
            };
            if MoveFileExW(
                wide(&temporary.to_string_lossy()).as_ptr(),
                wide(&path.to_string_lossy()).as_ptr(),
                MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
            ) == 0
            {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(temporary);
    }
    result
}
