using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Interop;
using Codex.ControlCenter.Shell;

internal static class ModalBackdropScenario
{
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(nint hwnd);
    [DllImport("user32.dll")] private static extern bool IsWindowEnabled(nint hwnd);
    [DllImport("user32.dll")] private static extern nint GetWindow(nint hwnd, uint command);
    [DllImport("user32.dll")] private static extern nint GetForegroundWindow();
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")] private static extern nint GetStyle(nint hwnd, int index);
    [DllImport("user32.dll", EntryPoint = "SetWindowLongPtrW")] private static extern nint SetStyle(nint hwnd, int index, nint value);

    internal static async Task RunAsync(Window owner, NativeWindowHost host, nint editor, int pid, string run, List<string> checks)
    {
        var root = new WindowInteropHelper(owner).Handle;
        var foreground = GetForegroundWindow();
        // Only this off-screen fixture changes styles; never activate a modal
        // or its owner while the user's real application remains foreground.
        var previousStyle = GetStyle(root, -20);
        SetStyle(root, -20, previousStyle | (nint)0x08000000);
        string leasePath = Path.Combine(run, "work", "control-center", "window-hosts", $"{pid}.json");
        long Epoch()
        {
            using var data = JsonDocument.Parse(File.ReadAllText(leasePath));
            Require(data.RootElement.GetProperty("visible").GetBoolean(), "Modal hid the native compositor lease.");
            return data.RootElement.GetProperty("presentationEpoch").GetInt64();
        }
        long epoch = Epoch();
        void CheckBackdrop(string name)
        {
            Require(!IsWindowEnabled(root), "ShowDialog did not disable its native owner.");
            Require(host.HasLiveAttachment && IsWindowVisible(editor), "Modal hid or detached the real native editor.");
            Require(GetWindow(root, 2) == editor, "Modal left the native input surface above the disabled manager.");
            Require(CaptureProbe.Save(root, Path.Combine(run, name + ".png")) > 32,
                "Modal manager capture lost real Chromium content.");
            Require(Epoch() == epoch, "Modal restarted native presentation instead of preserving its surface.");
            Require(GetForegroundWindow() == foreground, "Off-screen modal fixture changed desktop focus.");
        }
        Window Popup(Window parent) => new()
        {
            Owner = parent, ShowActivated = false, ShowInTaskbar = false,
            Left = -29000, Top = -29000, Width = 350, Height = 220,
            Content = new System.Windows.Controls.TextBlock { Text = "Isolated settings modal fixture" }
        };
        void ShowModal(Window parent, Func<Task> check)
        {
            var dialog = Popup(parent);
            Exception? failure = null;
            dialog.SourceInitialized += (_, _) =>
            {
                var handle = new WindowInteropHelper(dialog).Handle;
                SetStyle(handle, -20, GetStyle(handle, -20) | (nint)0x08000000);
            };
            dialog.Loaded += async (_, _) =>
            {
                try { await Task.Delay(220); host.SynchronizeLayout(); await Task.Delay(120); await check(); }
                catch (Exception error) { failure = error; }
                finally { dialog.Close(); }
            };
            dialog.ShowDialog();
            if (failure is not null) throw failure;
        }
        try
        {
            ShowModal(owner, () =>
            {
                CheckBackdrop("manager-modal");
                var settings = Application.Current.Windows.OfType<Window>().Single(w => w.Owner == owner);
                ShowModal(settings, () => { CheckBackdrop("manager-modal-nested"); return Task.CompletedTask; });
                CheckBackdrop("manager-modal-nested-closed");
                return Task.CompletedTask;
            });
            host.SynchronizeLayout(); await Task.Delay(200);
            Require(IsWindowEnabled(root) && GetWindow(root, 3) == editor,
                "Closing the final modal did not restore native editor input order.");
            Require(Epoch() == epoch && CaptureProbe.Save(root, Path.Combine(run, "manager-modal-closed.png")) > 32,
                "Closing settings reloaded or blanked the editor.");
            Require(GetForegroundWindow() == foreground, "Closing off-screen settings changed desktop focus.");
            checks.Add("Real WPF ShowDialog and nested modal retain Chromium capture behind settings; final close restores editor order without a presentation reset or desktop focus change.");
        }
        finally { SetStyle(root, -20, previousStyle); }
    }

    private static void Require(bool condition, string message)
    { if (!condition) throw new InvalidOperationException(message); }
}
