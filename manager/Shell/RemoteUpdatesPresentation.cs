using System.Text.Json;
using System.Text.RegularExpressions;

namespace Codex.ControlCenter.Shell;

internal static class RemoteUpdatesPresentation
{
    internal static string Version(string value)
    {
        if (string.IsNullOrWhiteSpace(value)) return "확인 필요";
        var managed = Regex.Match(value, @"^(\d+\.\d+\.\d+)-managed-[0-9a-f]{16,64}(?:-[0-9a-f]{16})?$");
        return managed.Success ? managed.Groups[1].Value : value;
    }

    internal static string Managed(JsonElement value) => value.S("observation_code") is { Length: > 0 } observation
        ? Error(observation)
        : value.S("state") switch
    {
        "current" or "latest" or "up_to_date" => "이 작업공간에서 제공하는 최신 버전으로 실행 중입니다.",
        "update_available" or "available" =>
            value.S("active_version") != value.S("available_version") &&
            Version(value.S("active_version")) == Version(value.S("available_version"))
                ? "같은 Codex 버전의 새 작업공간 빌드가 준비되었습니다. 작업이 끝난 뒤 적용할 수 있습니다."
                : "작업이 끝난 뒤 새 버전을 적용할 수 있습니다. 예약하면 안전하게 적용할 수 있을 때까지 기다립니다.",
        "busy" or "active" => "SSH 작업이 진행 중입니다. 작업이 끝난 뒤 업데이트를 적용합니다.",
        "stopped" or "not_running" => "현재 실행 중인 작업공간 SSH가 없습니다. 서버에 준비된 파일과 실행 버전은 다를 수 있습니다.",
        "not_prepared" => "먼저 이 계정으로 SSH 프로젝트를 열어 연결하세요. 연결 준비가 필요하면 아래 ‘SSH 연결 준비’를 이용하세요.",
        "attention" or "error" or "failed" => "작업공간 SSH 연결을 확인해야 합니다. 아래 안내를 확인한 뒤 ‘다시 확인’을 눌러 주세요.",
        "ready" or "prepared" => "작업공간 SSH에서 사용할 파일이 준비되었습니다. 실제 실행 버전은 연결 후 확인합니다.",
        _ => "실행 버전과 작업 상태를 확인하지 못했습니다. ‘다시 확인’을 눌러 주세요. 확인될 때까지 업데이트 적용은 대기합니다."
    };

    internal static string StockBlock(JsonElement value) => value.S("update_block_reason") switch
    {
        "standalone_missing" => "터미널 업데이트에 필요한 공식 독립 설치가 없습니다. 아래에서 설치 명령을 복사해 SSH 터미널에서 실행한 뒤 ‘다시 확인’을 누르세요. 작업공간 SSH 연결에는 영향이 없습니다.",
        "standalone_unavailable" => "터미널 업데이트용 독립 설치를 확인하지 못했습니다. 공식 설치 상태를 확인한 뒤 ‘다시 확인’을 누르세요. 작업공간 SSH 연결은 별도로 사용할 수 있습니다.",
        "installation_unsupported" => "이 Codex는 공식 터미널 업데이터가 관리하는 설치가 아닙니다. 설치에 사용한 앱이나 설치 도구에서 업데이트하세요. 업데이트 대기 중인 상태는 아닙니다.",
        "updater_unavailable" => "이 터미널 Codex는 앱에서 요청하는 업데이트 명령을 지원하지 않습니다. 설치에 사용한 도구에서 업데이트하세요.",
        _ => ""
    };

    internal static string Stock(JsonElement value) => value.S("update_mode") == "npm"
        ? "npm 설치를 업데이트한 뒤 이 서버의 터미널 Codex를 다시 시작합니다. 실행 버전까지 확인하며, 작업공간의 계정별 SSH 서비스는 유지합니다."
        : StockBlock(value) is { Length: > 0 } blocked ? blocked : value.S("state") switch
    {
        "current" => "설치된 버전과 백그라운드 실행 버전이 같습니다. 새 공식 버전은 수동 업데이트할 때 확인합니다.",
        "update_needed" => $"터미널에는 {Version(value.S("cli_version"))}이 설치되어 있지만, 백그라운드에서는 {Version(value.S("daemon_version"))}이 실행 중입니다. 터미널 Codex 작업을 모두 마친 뒤 업데이트하세요.",
        "stopped" or "not_running" => "터미널 Codex의 백그라운드 실행이 중지되었거나 실행 상태를 확인할 수 없습니다.",
        "missing" or "not_installed" => "이 SSH 서버에서 터미널용 Codex를 찾지 못했습니다.",
        "available" or "update_available" => "터미널 Codex는 작업 종료 여부를 자동으로 확인할 수 없습니다. 작업을 마친 뒤 직접 업데이트하세요.",
        _ => "터미널 Codex의 설치·실행 버전을 확인하지 못했습니다. SSH 연결을 확인한 뒤 ‘다시 확인’을 눌러 주세요."
    };

    internal static string Job(JsonElement job, bool terminal = false) =>
        terminal && job.S("state") == "applying" && job.S("step") is { Length: > 0 } step ? step switch
        {
            "installing_npm" => "npm으로 터미널 Codex를 업데이트하고 있습니다…",
            "restarting_terminal" => "이전 터미널 Codex의 종료를 확인하고 있습니다…",
            "starting_terminal" => "새 터미널 Codex를 시작하고 실행 버전을 확인하고 있습니다…",
            _ => "터미널 Codex 업데이트 결과를 확인하고 있습니다…"
        } : job.S("state") switch
    {
        "queued" or "pending" => "업데이트를 예약했습니다. 작업 종료와 연결 상태를 확인한 뒤 적용합니다.",
        "waiting" or "waiting_for_idle" or "waiting_for_work" or "blocked" or "busy" => "작업이 진행 중이거나 상태 확인이 끝나지 않아 기다리고 있습니다.",
        "recovering" => "앞선 업데이트 결과를 확인하고 있습니다. 결과 확인 전에는 같은 업데이트를 다시 실행하지 않습니다.",
        "dispatching" or "starting" or "applying" or "running" or "updating" or "preparing" => terminal
            ? "터미널 Codex 업데이트를 진행하고 있습니다. 완료 후 실행 버전을 확인합니다."
            : "작업공간 SSH 업데이트를 진행하고 있습니다. 완료될 때까지 기다려 주세요.",
        "complete" or "completed" or "success" => "업데이트 후 실행 상태를 확인했습니다.",
        "cancelled" or "canceled" => "업데이트 예약을 취소했습니다.",
        "unsupported" => "앞선 요청은 업데이트가 적용되지 않고 끝났습니다. 현재 설치 방식에서는 해당 업데이트 명령을 지원하지 않습니다.",
        "failed" => Error(job.S("code")),
        "attention" or "unknown" => job.B("verification_pending")
            ? "업데이트 명령은 끝났으며 실행 결과를 다시 확인하고 있습니다. 중복 실행하지 마세요."
            : "앞선 업데이트가 완료됐는지 확인하지 못했습니다. ‘다시 확인’을 눌러 상태를 확인하세요. 확인 전에는 다시 실행하지 않습니다.",
        _ => "업데이트 상태를 확인하고 있습니다."
    };

    internal static string Error(string code) => code switch
    {
        "remote_idle_binding_missing" =>
            "이 연결의 작업 상태를 자동으로 확인할 수 없어 업데이트를 보류합니다. 연결 실패를 뜻하는 것은 아닙니다. 진행 중인 작업은 그대로 유지합니다.",
        "remote_listener_unavailable" =>
            "서버에 Codex 프로세스가 남아 있지만 SSH 요청에 응답하지 않습니다. 작업이 끝났는지 알 수 없어 업데이트를 보류합니다. 연결을 복구한 뒤 ‘다시 확인’을 눌러 주세요.",
        "remote_maintenance_unverified" or "managed_observation_unavailable" or "ssh_observation_unavailable" =>
            "작업공간 SSH의 실행 상태를 확인하지 못했습니다. SSH 연결을 확인한 뒤 ‘다시 확인’을 눌러 주세요.",
        "ssh_connection_failed" or "ssh_timeout" or "ssh_failed" or "remote_probe_incomplete" =>
            "SSH 서버에 연결하거나 응답을 확인하지 못했습니다. Codex의 SSH 연결 오류를 먼저 확인한 뒤 ‘다시 확인’을 눌러 주세요.",
        "ssh_host_changed" => "SSH 연결 대상이 이전과 달라졌습니다. Codex의 SSH 설정에서 서버와 계정을 확인한 뒤 다시 연결하세요.",
        "ssh_missing" => "Windows OpenSSH를 찾지 못했습니다. SSH 클라이언트 설치 상태를 확인해 주세요.",
        "unknown_ssh_alias" or "invalid_ssh_alias" => "SSH 설정에 등록된 연결을 선택해 주세요. 연결 목록이 바뀌었다면 이 창을 다시 여세요.",
        "ssh_update_busy" => "이 SSH 연결을 확인하거나 업데이트하는 중입니다. 끝난 뒤 다시 확인해 주세요.",
        "ssh_not_enrolled" => "이 계정으로 SSH 프로젝트를 먼저 열어 연결한 뒤 업데이트를 예약하세요.",
        "ssh_generation_changed" or "remote_binding_changed" or "remote_configuration_changed" =>
            "확인 중 계정이나 SSH 연결 설정이 바뀌었습니다. 현재 연결을 확인한 뒤 ‘다시 확인’을 눌러 주세요.",
        "ssh_target_changed" => "업데이트할 파일이 바뀌었습니다. ‘다시 확인’으로 새 버전을 확인해 주세요.",
        "remote_artifact_missing" or "invalid_artifact" =>
            "이 서버에 사용할 작업공간 업데이트 파일을 확인하지 못했습니다. 작업공간 앱을 최신 버전으로 준비한 뒤 다시 확인하세요.",
        "stock_versions_changed_refresh_required" or "stock_update_requires_current_observation" =>
            "터미널 Codex 상태가 확인 이후 바뀌었습니다. ‘다시 확인’을 누른 뒤 작업 종료 확인란을 다시 선택하세요.",
        "stock_update_requires_confirmation" => "터미널 Codex 작업을 모두 마친 뒤 작업 종료 확인란을 선택하세요.",
        "stock_npm_target_changed" => "확인한 뒤 터미널 Codex의 설치나 실행 상태가 바뀌었습니다. ‘다시 확인’을 누른 뒤 업데이트하세요.",
        "stock_npm_install_failed" or "stock_npm_install_unverified" => "npm 설치를 완료하지 못했습니다. 서버의 네트워크와 npm 설치 권한을 확인한 뒤 다시 시도하세요. 기존 서비스에 종료 요청을 보내지 않았습니다.",
        "stock_npm_exit_pending" => "터미널 Codex의 종료가 아직 확인되지 않았습니다. 중복 실행하지 않고 상태를 확인해야 합니다.",
        "stock_npm_start_unverified" => "npm 설치는 끝났지만 새 터미널 서비스의 응답을 아직 확인하지 못했습니다. ‘다시 확인’을 눌러 주세요.",
        "stock_version_operation_failed" or "stock_observation_unavailable" or "stock_update_unverified" =>
            "터미널 Codex의 상태나 업데이트 결과를 확인하지 못했습니다. ‘다시 확인’으로 상태를 확인하세요. 완료 여부를 모르는 업데이트는 다시 실행하지 마세요.",
        "ssh_update_attention" or "remote_runtime_exited" or "remote_start_timeout" =>
            "이전 작업공간 SSH 업데이트의 실행 결과를 확인해야 합니다. SSH 연결 오류를 확인한 뒤 ‘다시 확인’을 눌러 주세요.",
        "ssh_target_unknown" or "remote_operation_pending" or "remote_exit_unverified" or "remote_preparation_pending" or "remote_start_pending" or "remote_process_changed" or "remote_start_unverified" =>
            "작업공간 SSH의 이전 요청 결과를 확인하고 있습니다. 확인될 때까지 업데이트를 보류합니다. ‘다시 확인’으로 상태를 확인해 주세요.",
        _ => code.Any(character => character is >= '\uAC00' and <= '\uD7A3') ? code
            : "SSH 상태를 확인하지 못했습니다. 연결 상태를 확인한 뒤 ‘다시 확인’을 눌러 주세요."
    };
}
