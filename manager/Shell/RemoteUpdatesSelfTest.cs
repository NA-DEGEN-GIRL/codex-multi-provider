using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;

namespace Codex.ControlCenter.Shell;

internal static class RemoteUpdatesSelfTest
{
    internal static async Task RunAsync(string report)
    {
        int checks = 0;
        void Require(bool condition, string message)
        {
            if (!condition) throw new InvalidOperationException(message);
            checks++;
        }

        const string profileId = "fixture-profile";
        const string firstHost = "fixture-a";
        const string secondHost = "fixture-b";
        bool? autoCheck = null, autoApply = null;
        bool stockSupported = true, canSchedule = true;
        bool managedHashes = false, managedMissing = false;
        string stockObservation = new('a', 64), stockHostIdentity = new('d', 64);
        string? errorCode = null;
        string? managedObservationCode = null;
        string? stockBlockReason = null;
        string? stockUpdateMode = null;
        string managedState = "update_available", stockState = "available";
        string managedMessage = "관리 런타임 새 버전을 사용할 수 있습니다.";
        object? job = null;
        object? stockJob = null;
        TaskCompletionSource<JsonElement>? delayedCheck = null;
        var requests = new List<(string Command, JsonElement Args)>();

        JsonElement Snapshot(string alias)
        {
            var result = new Dictionary<string, object?>
            {
                ["alias"] = alias,
                ["profile_id"] = profileId,
                ["checking"] = false,
                ["managed"] = new
                {
                    active_version = managedMissing ? null : managedHashes ? "0.153.4-managed-aaaaaaaaaaaaaaaa" : alias == firstHost ? "1.0.0" : "2.0.0",
                    prepared_version = managedHashes ? "0.153.4-managed-cccccccccccccccc" : alias == firstHost ? "1.1.0" : "2.1.0",
                    available_version = managedHashes ? "0.153.4-managed-bbbbbbbbbbbbbbbb" : alias == firstHost ? "1.2.0" : "2.2.0",
                    can_schedule = canSchedule,
                    state = managedState,
                    observation_code = managedObservationCode,
                    message = managedMessage
                },
                ["stock"] = new
                {
                    cli_version = "0.155.0",
                    daemon_version = "0.154.0",
                    daemon_state = "running",
                    observation_id = stockObservation,
                    host_identity = stockHostIdentity,
                    state = stockState,
                    message = "기본 CLI 작업 상태는 자동으로 확인할 수 없습니다.",
                    update_supported = stockSupported,
                    update_block_reason = stockBlockReason,
                    update_mode = stockUpdateMode,
                    safe_auto_update = false,
                    update_job = stockJob
                },
                ["job"] = job,
                ["checked_at"] = "2026-09-21T00:00:00Z"
            };
            if (autoCheck is not null) result["auto_check"] = autoCheck.Value;
            if (autoApply is not null) result["auto_apply"] = autoApply.Value;
            if (errorCode is not null) result["error"] = errorCode;
            return JsonSerializer.SerializeToElement(result);
        }

        Task<JsonElement> Request(string command, object? args)
        {
            var input = JsonSerializer.SerializeToElement(args);
            requests.Add((command, input));
            var alias = input.S("alias");
            Require(input.S("profile_id") == profileId && alias is firstHost or secondHost,
                "every remote update request retains its selected profile and host");
            switch (command)
            {
                case "remote.updates.status": break;
                case "remote.updates.check":
                    if (alias == firstHost && delayedCheck is not null) return delayedCheck.Task;
                    break;
                case "remote.updates.settings":
                    autoCheck = input.GetProperty("auto_check").GetBoolean();
                    autoApply = input.GetProperty("auto_apply").GetBoolean();
                    break;
                case "remote.updates.schedule":
                    job = new { id = "fixture-job", state = "pending", message = "작업 종료 후 적용 예약" };
                    break;
                case "remote.updates.cancel": job = null; break;
                case "remote.updates.stock_update":
                    Require(input.B("confirmed"), "stock update requires an explicit work-finished confirmation");
                    break;
                case "remote.prepare": break;
                default: throw new InvalidOperationException("Unexpected fixture request: " + command);
            }
            return Task.FromResult(Snapshot(alias));
        }

        var hosts = JsonSerializer.SerializeToElement(new
        {
            hosts = new[] { new { alias = firstHost }, new { alias = secondHost } }
        });
        var window = new RemoteUpdatesWindow(profileId, hosts, Request)
        {
            WindowStartupLocation = WindowStartupLocation.Manual,
            Left = -28000, Top = -28000, ShowInTaskbar = false, ShowActivated = false
        };
        T Control<T>(string name) where T : FrameworkElement =>
            Descendants(window).OfType<T>().Single(value => value.Name == name);
        void Click(string name) => Control<Button>(name).RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        void Toggle(string name, bool value)
        {
            var control = Control<CheckBox>(name);
            control.IsChecked = value;
            control.RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent));
        }
        async Task Settle() => await window.Dispatcher.InvokeAsync(window.UpdateLayout, DispatcherPriority.Background);
        string Text(string name) => Control<TextBlock>(name).Text;
        void Capture(string path)
        {
            var content = (FrameworkElement)window.Content;
            var visual = new DrawingVisual();
            using (var drawing = visual.RenderOpen())
            {
                var rectangle = new Rect(0, 0, content.ActualWidth, content.ActualHeight);
                drawing.DrawRectangle(window.Background, null, rectangle);
                drawing.DrawRectangle(new VisualBrush(content), null, rectangle);
            }
            var bitmap = new RenderTargetBitmap((int)content.ActualWidth, (int)content.ActualHeight, 96, 96, PixelFormats.Pbgra32);
            bitmap.Render(visual);
            using var output = File.Create(path);
            var encoder = new PngBitmapEncoder(); encoder.Frames.Add(BitmapFrame.Create(bitmap)); encoder.Save(output);
        }
        bool closed = false;

        try
        {
            Require(requests.Count == 0, "constructing the window performs no remote I/O");
            window.Show();
            await window.LoadAsync();
            await Settle();
            Require(requests.Count > 0 && requests.All(r => r.Command == "remote.updates.status"),
                "opening reads cached status without checking or changing the remote host");
            Require(Control<CheckBox>("AutoCheck").IsChecked == true, "automatic checks default to enabled when absent");
            Require(Control<CheckBox>("AutoApply").IsChecked == false, "managed automatic application defaults to disabled when absent");
            Require(Control<CheckBox>("StockConfirmation").IsChecked == false && !Control<Button>("StockUpdate").IsEnabled,
                "stock update starts disabled without work-finished confirmation");
            var beforeUnconfirmed = requests.Count;
            Click("StockUpdate");
            Require(requests.Count == beforeUnconfirmed, "programmatic stock activation cannot bypass the explicit confirmation gate");
            var details = Control<Expander>("RemoteUpdateDetails");
            Require(!details.IsExpanded, "technical build identities and prepared files start in collapsed details");
            details.IsExpanded = true; await Settle();
            Require(Text("ManagedActiveVersion").Contains("1.0.0") && Text("ManagedPreparedVersion").Contains("1.1.0") &&
                Text("ManagedAvailableVersion").Contains("1.2.0"), "active, prepared, and available managed versions are distinguishable");
            details.IsExpanded = false; await Settle();
            Require(Text("StockCliVersion").Contains("0.155.0") && Text("StockDaemonVersion").Contains("0.154.0"),
                "stock CLI and running daemon versions are distinguishable");
            Require(Descendants(window).OfType<TextBlock>().Any(t => t.Text == "작업공간에서 쓰는 Codex") &&
                Descendants(window).OfType<TextBlock>().Any(t => t.Text == "SSH 터미널에서 쓰는 Codex"),
                "headings distinguish the workspace SSH client from terminal Codex");
            managedHashes = true;
            await window.LoadAsync(); await Settle();
            Require(Text("ManagedActiveVersion") == "0.153.4" && Text("ManagedAvailableVersion") == "0.153.4" &&
                Text("ManagedGuidance").Contains("새 작업공간 빌드"), "same-version managed build updates remain understandable without raw hashes");
            details.IsExpanded = true; await Settle();
            Require(Text("RemoteUpdateDiagnostics").Contains("0.153.4-managed-aaaaaaaaaaaaaaaa") &&
                Text("RemoteUpdateDiagnostics").Contains("0.153.4-managed-bbbbbbbbbbbbbbbb"),
                "full build identities remain available for diagnosis");
            details.IsExpanded = false; managedHashes = false;
            await window.LoadAsync(); await Settle();

            Toggle("AutoCheck", false); await Settle();
            Require(requests[^1].Command == "remote.updates.settings" && autoCheck == false && autoApply == false,
                "disabling automatic checking does not opt in to managed application");
            Toggle("AutoApply", true); await Settle();
            Require(requests[^1].Command == "remote.updates.settings" && autoCheck == false && autoApply == true,
                "managed application preference is independent of automatic checking");
            Require(requests.All(r => r.Command != "remote.updates.stock_update"), "managed preferences never request a stock update");
            Toggle("AutoCheck", true); Toggle("AutoApply", false); await Settle();

            canSchedule = false;
            await window.LoadAsync(); await Settle();
            Require(!Control<Button>("ScheduleManaged").IsEnabled, "a host never enrolled for this profile cannot schedule a managed update");
            canSchedule = true;
            managedState = "current"; stockState = "current";
            await window.LoadAsync(); await Settle();
            Require(Text("ManagedState").Contains("최신") && !Control<Button>("ScheduleManaged").IsEnabled,
                "an available managed bundle already in use is current without duplicate scheduling");
            Require(Text("StockState").Contains("버전 일치") && !Text("StockState").Contains("최신"),
                "matching stock versions do not claim an upstream latest-version check");
            managedState = "attention"; errorCode = "ssh_host_changed";
            await window.LoadAsync(); await Settle();
            Require(!Control<Button>("ScheduleManaged").IsEnabled && Text("RemoteUpdateFeedback").Contains("연결 대상") &&
                !Text("RemoteUpdateFeedback").Contains(errorCode),
                "a blocked managed update gives a human recovery explanation without exposing a raw code");
            managedState = "update_available"; stockState = "update_needed"; errorCode = null;
            await window.LoadAsync(); await Settle();

            Require(Control<Button>("ScheduleManaged").IsEnabled, "a known managed update can be scheduled");
            Click("ScheduleManaged"); await Settle();
            Require(requests[^1].Command == "remote.updates.schedule" && !requests[^1].Args.B("confirmed"),
                "managed scheduling has its own action without stock confirmation");
            Require(Text("JobState").Length > 0 && Control<Button>("CancelManaged").IsEnabled,
                "a pending managed job is visible and can be cancelled");
            Click("CancelManaged"); await Settle();
            Require(requests[^1].Command == "remote.updates.cancel", "cancelling a managed reservation sends the scoped cancel request");

            Toggle("StockConfirmation", true); await Settle();
            Require(Control<Button>("StockUpdate").IsEnabled, "an explicitly confirmed supported stock update is available");
            Click("StockUpdate"); await Settle();
            Require(requests.Any(r => r.Command == "remote.updates.stock_update" && r.Args.B("confirmed")),
                "manual stock update carries the affirmative work-finished confirmation");
            Require(requests.Last(r => r.Command == "remote.updates.stock_update").Args.S("observation_id") == new string('a', 64),
                "manual stock update sends the exact displayed observation token");
            Toggle("StockConfirmation", true);
            stockObservation = new string('b', 64);
            await window.LoadAsync(); await Settle();
            Require(Control<CheckBox>("StockConfirmation").IsChecked == false && !Control<Button>("StockUpdate").IsEnabled,
                "a changed observation clears consent even when CLI and daemon versions are unchanged");
            var beforeStaleConfirmation = requests.Count;
            Click("StockUpdate");
            Require(requests.Count == beforeStaleConfirmation, "a stale confirmation cannot submit a manual update");
            Toggle("StockConfirmation", true);
            stockHostIdentity = new string('e', 64);
            await window.LoadAsync(); await Settle();
            Require(Control<CheckBox>("StockConfirmation").IsChecked == false && !Control<Button>("StockUpdate").IsEnabled,
                "a changed host identity clears consent even when the alias and observation token are unchanged");
            Toggle("StockConfirmation", true);
            stockObservation = new string('c', 64);
            Click("StockUpdate"); await Settle();
            Require(requests.Last(r => r.Command == "remote.updates.stock_update").Args.S("observation_id") == new string('b', 64),
                "an unseen backend refresh cannot replace the observation the user confirmed");
            stockObservation = "";
            await window.LoadAsync(); Toggle("StockConfirmation", true); await Settle();
            var beforeMissingObservation = requests.Count;
            Click("StockUpdate");
            Require(!Control<Button>("StockUpdate").IsEnabled && requests.Count == beforeMissingObservation,
                "a missing observation token cannot fall back to a newer backend observation");
            stockObservation = new string('c', 64);
            stockSupported = false;
            await window.LoadAsync(); Toggle("StockConfirmation", true); await Settle();
            Require(!Control<Button>("StockUpdate").IsEnabled, "confirmation cannot enable an unsupported stock updater");
            stockSupported = true;

            stockState = "unavailable";
            await window.LoadAsync(); Toggle("StockConfirmation", true); await Settle();
            var beforeUnverified = requests.Count;
            Click("StockUpdate");
            Require(!Control<Button>("StockUpdate").IsEnabled && requests.Count == beforeUnverified && Text("StockActionHint").Contains("다시 확인"),
                "stale support and observation metadata cannot enable an unverified terminal update");
            stockState = "update_needed";
            stockJob = new { state = "attention", verification_pending = false };
            await window.LoadAsync(); Toggle("StockConfirmation", true); await Settle();
            Require(!Control<Button>("StockUpdate").IsEnabled && Text("StockActionHint").Contains("앞선 업데이트"),
                "an uncertain previous terminal update offers status recovery instead of another update");
            stockJob = null;

            stockSupported = false; stockBlockReason = "standalone_missing";
            stockJob = new { state = "unsupported", verification_pending = false };
            await window.LoadAsync(); await Settle();
            Require(!Control<CheckBox>("StockConfirmation").IsEnabled && !Control<Button>("StockUpdate").IsEnabled &&
                Control<Button>("StockSetup").Visibility == Visibility.Visible,
                "a missing standalone install offers setup instead of an unusable update confirmation");
            Require(Text("StockActionHint").Contains("독립 설치가 없습니다") && !Text("StockActionHint").Contains("앞선 업데이트 결과") &&
                Text("StockGuidance").Contains("적용되지 않고 끝났습니다"),
                "unsupported exit zero is explained as a finished non-update rather than endless verification");
            stockBlockReason = "installation_unsupported";
            await window.LoadAsync(); await Settle();
            Require(Control<Button>("StockSetup").Visibility == Visibility.Collapsed && Text("StockActionHint").Contains("설치 도구"),
                "other installer ownership is explained without offering an unrelated reinstall");
            stockBlockReason = null; stockSupported = true; stockJob = null;
            await window.LoadAsync(); Toggle("StockConfirmation", true); await Settle();
            Require(Control<Button>("StockUpdate").IsEnabled,
                "a fresh supported installation can be confirmed again after a terminal unsupported result");

            stockUpdateMode = "npm"; stockJob = new { state = "unsupported", verification_pending = false };
            await window.LoadAsync(); Toggle("StockConfirmation", true); await Settle();
            Require(Control<Button>("StockUpdate").IsEnabled && Text("StockGuidance").Contains("npm 방식으로 진행할 수 있습니다") &&
                Text("StockActionHint").Contains("npm 설치 감지됨") && Control<Button>("StockSetup").Visibility == Visibility.Collapsed,
                "a verified npm fallback enables the normal confirmation without requiring manual standalone setup");
            stockJob = new { state = "applying", step = "installing_npm" };
            await window.LoadAsync(); await Settle();
            Require(!Control<CheckBox>("StockConfirmation").IsEnabled && Text("StockGuidance").Contains("npm으로 터미널 Codex"),
                "npm install progress blocks duplicate clicks and explains the current step");
            stockJob = new { state = "complete" };
            await window.LoadAsync(); await Settle();
            Require(Text("StockGuidance").Contains("실행 상태를 확인했습니다"), "npm completion is shown after runtime verification");
            stockJob = null; stockUpdateMode = null;

            managedState = "unknown";
            managedMessage = "작업 상태를 확인할 수 없어 적용을 대기합니다.";
            job = new { id = "fixture-job", state = "unknown", message = managedMessage };
            await window.LoadAsync(); await Settle();
            var unknownLabel = Text("ManagedState");
            Require(unknownLabel.Length > 0 && !unknownLabel.Contains("최신") && !unknownLabel.Contains("완료"),
                "unknown activity is not presented as current or successfully completed");
            Require(Text("ManagedGuidance").Contains("확인하지 못했습니다") && Text("ManagedGuidance").Contains("대기"),
                "unknown activity explains that application waits for verification");
            managedState = "busy";
            managedMessage = "실행 중인 작업이 끝나면 적용합니다.";
            job = new { id = "fixture-job", state = "busy", message = managedMessage };
            await window.LoadAsync(); await Settle();
            Require(Text("ManagedState") != unknownLabel && !Control<Button>("ScheduleManaged").IsEnabled,
                "busy activity is distinguishable from unknown activity and does not queue duplicate work");

            managedState = "unknown"; managedHashes = managedMissing = true; job = null;
            errorCode = "remote_maintenance_unverified";
            await window.LoadAsync(); await Settle();
            Require(Text("ManagedActiveVersion") == "확인 필요" && Text("ManagedAvailableVersion") == "0.153.4" &&
                Text("RemoteUpdateFeedback").Contains("작업공간 SSH") && Text("RemoteUpdateFeedback").Contains("다시 확인") &&
                !Text("RemoteUpdateFeedback").Contains(errorCode),
                "an unverified workspace runtime is explained separately from terminal Codex and includes a recovery action");
            details.IsExpanded = true; await Settle();
            Require(Text("RemoteUpdateDiagnostics").Contains(errorCode), "the exact maintenance diagnostic remains available in secondary details");
            details.IsExpanded = false; await Settle();
            var connectionPng = Path.Combine(Path.GetDirectoryName(report)!, Path.GetFileNameWithoutExtension(report) + "-connection-status.png");
            Control<ScrollViewer>("RemoteUpdatesScroll").ScrollToTop(); await Settle(); Capture(connectionPng);
            managedHashes = managedMissing = false; errorCode = null;

            managedHashes = true; managedState = "current";
            foreach (var observation in new[] { "remote_listener_unavailable", "remote_idle_binding_missing", "remote_maintenance_unverified" })
            {
                managedObservationCode = observation;
                await window.LoadAsync(); await Settle();
                Require(Text("ManagedActiveVersion") == "0.153.4" && !Text("ManagedState").Contains("최신"),
                    "an observed version is retained without claiming verified current activity for " + observation);
                Require(Text("ManagedGuidance").Contains(observation == "remote_listener_unavailable" ? "프로세스가 남아" : observation == "remote_idle_binding_missing" ? "작업 상태를 자동으로 확인할 수 없어" : "확인하지 못했습니다") &&
                    !Text("ManagedGuidance").Contains(observation) && Text("RemoteUpdateDiagnostics").Contains(observation),
                    "workspace observation guidance explains the specific cause and keeps its raw code secondary for " + observation);
                Require(!Control<Button>("ScheduleManaged").IsEnabled,
                    "an unverified observation does not enable an immediate update on an otherwise current runtime");
            }
            managedObservationCode = "remote_listener_unavailable"; managedState = "unknown";
            job = new { id = "fixture-job", state = "waiting" };
            await window.LoadAsync(); await Settle();
            Require(Text("ManagedGuidance").Contains("작업이 끝났는지 알 수 없어") &&
                !Control<Button>("ScheduleManaged").IsEnabled && Text("JobState").Contains("기다리고"),
                "a missing listener with a live process stays pending rather than being treated as idle or exited");
            Control<ScrollViewer>("RemoteUpdatesScroll").ScrollToTop(); await Settle(); Capture(connectionPng);
            managedHashes = false; managedObservationCode = null;

            managedState = "update_available"; managedMessage = "관리 런타임 새 버전을 사용할 수 있습니다."; job = null;
            await window.LoadAsync(); await Settle();
            Toggle("StockConfirmation", true);
            delayedCheck = new TaskCompletionSource<JsonElement>(TaskCreationOptions.RunContinuationsAsynchronously);
            var lateFirstHost = Snapshot(firstHost);
            Click("CheckNow"); await Settle();
            var selector = Control<ComboBox>("HostSelector");
            Require(selector.IsEnabled, "host selection stays available during a read-only version check");
            selector.SelectedIndex = 1; await Settle();
            Require(Control<CheckBox>("StockConfirmation").IsChecked == false && !Control<Button>("StockUpdate").IsEnabled,
                "switching hosts clears stock work-finished confirmation");
            delayedCheck.SetResult(lateFirstHost);
            await Settle(); await Settle();
            Require(Text("ManagedActiveVersion").Contains("2.0.0") && !Text("ManagedActiveVersion").Contains("1.0.0"),
                "a late check response cannot overwrite the newly selected host");

            window.Width = 560; window.UpdateLayout();
            var confirmation = Control<CheckBox>("StockConfirmation");
            confirmation.BringIntoView(); await Settle();
            var confirmationText = (TextBlock)confirmation.Content;
            var confirmationPoint = confirmationText.TranslatePoint(new Point(), window);
            Require(confirmationText.TextWrapping == TextWrapping.Wrap && confirmationPoint.X >= 0 &&
                confirmationPoint.X + confirmationText.ActualWidth < window.ActualWidth,
                "the full manual-update confirmation wraps within the narrow window");
            foreach (var button in Descendants(window).OfType<Button>().Where(b => b.IsVisible && b.Name is
                "CheckNow" or "ScheduleManaged" or "CancelManaged" or "StockUpdate" or "PrepareRemote"))
            {
                var point = button.TranslatePoint(new Point(), window);
                Require(point.X >= 0 && point.X + button.ActualWidth <= window.ActualWidth,
                    button.Name + " remains inside the narrow window");
            }
            var narrowPng = Path.Combine(Path.GetDirectoryName(report)!, Path.GetFileNameWithoutExtension(report) + "-narrow.png");
            Control<ScrollViewer>("RemoteUpdatesScroll").ScrollToBottom(); await Settle();
            var manualAction = Control<Button>("StockUpdate");
            var manualPoint = manualAction.TranslatePoint(new Point(), window);
            Require(manualPoint.Y >= 0 && manualPoint.Y + manualAction.ActualHeight < window.ActualHeight,
                "scrolling the narrow window exposes the complete manual-update action");
            Capture(narrowPng);
            window.Width = 760; window.UpdateLayout();
            Control<ScrollViewer>("RemoteUpdatesScroll").ScrollToTop(); await Settle();
            var png = Path.ChangeExtension(report, ".png");
            Capture(png);
            window.Close(); closed = true;
            var requestsAtClose = requests.Count;
            await Task.Delay(2200);
            Require(requests.Count == requestsAtClose, "closing the window stops cached polling without issuing more RPCs");
            File.WriteAllText(report, JsonSerializer.Serialize(new
            {
                passed = true, checks, png, narrow_png = narrowPng, connection_status_png = connectionPng,
                isolation = "Synthetic SSH hosts and cached RPC responses; no live profiles, processes, or SSH connections accessed"
            }));
        }
        finally { if (!closed) window.Close(); }
    }

    private static IEnumerable<DependencyObject> Descendants(DependencyObject parent)
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(parent); i++)
        {
            var child = VisualTreeHelper.GetChild(parent, i);
            yield return child;
            foreach (var descendant in Descendants(child)) yield return descendant;
        }
    }
}
