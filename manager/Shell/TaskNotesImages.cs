using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using Codex.ControlCenter.Shared;

namespace Codex.ControlCenter.Shell;

internal sealed partial class TaskNotesPanel
{
    private NoteImageStore imageStore = null!;
    private bool imageAttachmentsSupported, importingImage;
    private long imageRender;
    private readonly WrapPanel imageTiles = new() { Margin = new Thickness(0, 8, 0, 0) };
    private readonly TextBlock imageCount = new() { Foreground = WorkspaceAppearance.Muted, FontSize = 12, VerticalAlignment = VerticalAlignment.Center };
    private readonly TextBlock imageHint = new() { Text = "Ctrl+V로 붙여넣기 · 클릭하여 확대", FontSize = 11, Foreground = WorkspaceAppearance.Muted, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 4, 0, 0) };
    private readonly Button pasteImage = new() { Name = "PasteNoteImage", Content = "붙여넣기", ToolTip = "클립보드의 사진을 메모에 첨부 (Ctrl+V)" };
    private readonly Button addImage = new() { Name = "AddNoteImage", Content = "+ 이미지", ToolTip = "이미지 파일 선택" };

    private void InitializeImages(string root, StackPanel document)
    {
        imageStore = new(root);
        var section = new StackPanel();
        var heading = new DockPanel();
        var actions = new StackPanel { Orientation = Orientation.Horizontal };
        foreach (var button in new[] { pasteImage, addImage })
        { WorkspaceAppearance.Tool(button, quiet: true); button.Height = 28; button.Padding = new Thickness(6, 0, 6, 0); button.Margin = new Thickness(3, 0, 0, 0); actions.Children.Add(button); }
        DockPanel.SetDock(actions, Dock.Right); heading.Children.Add(actions); heading.Children.Add(imageCount);
        section.Children.Add(heading); section.Children.Add(imageHint); section.Children.Add(imageTiles);
        document.Children.Add(new Border { BorderBrush = WorkspaceAppearance.Line, BorderThickness = new Thickness(0, 1, 0, 0),
            Padding = new Thickness(0, 10, 0, 0), Margin = new Thickness(0, 14, 0, 0), Child = section });
        pasteImage.Click += async (_, _) => await PasteImageAsync();
        addImage.Click += async (_, _) =>
        {
            if (!CanAttachImage()) return;
            var dialog = new Microsoft.Win32.OpenFileDialog { Title = "메모에 이미지 추가", Filter = "이미지|*.png;*.jpg;*.jpeg;*.bmp;*.gif", CheckFileExists = true };
            var draft = editing!;
            if (dialog.ShowDialog(Window.GetWindow(this)) == true)
                await ImportImageAsync(draft, () => NoteImageStore.ReadFile(dialog.FileName));
        };
        PreviewKeyDown += async (_, e) =>
        {
            if (e.Key != Key.V || Keyboard.Modifiers != ModifierKeys.Control || editing is null) return;
            try
            {
                if (!Clipboard.ContainsImage()) return; // Ordinary text paste stays with its text box.
                e.Handled = true; await PasteImageAsync();
            }
            catch (Exception error) { status.Text = "이미지 붙여넣기 실패 · " + error.Message; }
        };
    }
    private void SetImageCapabilities(JsonElement value)
    {
        imageAttachmentsSupported = value.TryGetProperty("image_attachments_version", out var version) && version.TryGetInt32(out var number) && number >= 1;
        UpdateImageActions();
    }
    private bool CanAttachImage()
    {
        if (editing is null || importingImage || splitting || loading || loadFailed) return false;
        if (!imageAttachmentsSupported)
        { status.Text = "이미지 첨부는 새 관리 서비스가 필요합니다. 작업을 마친 뒤 완전 종료하고 다시 열어 주세요."; return false; }
        if (editing.Note.Images.Count >= NoteImageStore.MaximumImages)
        { status.Text = "한 메모에는 이미지를 24개까지 첨부할 수 있습니다."; return false; }
        return true;
    }
    private void UpdateImageActions()
    {
        addImage.IsEnabled = pasteImage.IsEnabled = editing is not null && imageAttachmentsSupported && !importingImage && !splitting;
        imageHint.Text = imageAttachmentsSupported ? "Ctrl+V로 붙여넣기 · 클릭하여 확대" : "이미지 첨부 준비 중 · 새 관리 서비스 적용 후 사용 가능";
    }
    private async Task PasteImageAsync()
    {
        if (!CanAttachImage()) return;
        try
        {
            var bitmap = Clipboard.GetImage();
            if (bitmap is null) { status.Text = "복사한 이미지가 없습니다. 사진을 복사한 뒤 붙여넣어 주세요."; return; }
            bitmap.Freeze(); await ImportImageAsync(editing!, () => bitmap);
        }
        catch (Exception error) { status.Text = "이미지 붙여넣기 실패 · " + error.Message; }
    }
    internal async Task AttachImageAsync(BitmapSource bitmap)
    {
        if (!CanAttachImage()) return;
        bitmap.Freeze(); await ImportImageAsync(editing!, () => bitmap);
    }
    private async Task ImportImageAsync(NoteDraft draft, Func<BitmapSource> read)
    {
        importingImage = true; pendingMutations++; UpdateImageActions(); UpdateSharing();
        try
        {
            var image = await Task.Run(() => imageStore.Save(read()));
            if (draft.Note.Deleted) return;
            if (draft.Note.Images.Any(i => i.Id == image.Id))
            { if (editing == draft) status.Text = "이미 첨부된 사진입니다."; return; }
            draft.Note.Images.Add(image); Dirty(draft);
            // Persist the attachment draft before returning, so close/crash can
            // recover the reference as well as the already durable PNG.
            recovery.Write(draft.Task, draft.Note, draft.Changed);
            if (editing == draft) RenderImages();
            await SaveAsync(draft);
        }
        catch (Exception error) { if (editing == draft) status.Text = "이미지 첨부 실패 · " + error.Message; }
        finally { importingImage = false; pendingMutations--; UpdateImageActions(); UpdateSharing(); }
    }
    private void RenderImages()
    {
        var ticket = ++imageRender; imageTiles.Children.Clear(); UpdateImageActions();
        imageCount.Text = $"사진 {editing?.Note.Images.Count ?? 0}";
        if (editing is not { } draft) return;
        for (int index = 0; index < draft.Note.Images.Count; index++)
        {
            var attachment = draft.Note.Images[index]; var label = $"이미지 {index + 1}";
            var preview = new Image { Width = 102, Height = 74, Stretch = Stretch.Uniform };
            var content = new StackPanel(); content.Children.Add(preview);
            content.Children.Add(new TextBlock { Text = label, FontSize = 11, Foreground = WorkspaceAppearance.Muted, Margin = new Thickness(0, 5, 0, 0), TextAlignment = TextAlignment.Center });
            var tile = new Button { Name = "NoteImageThumbnail", Content = content, Width = 114, Height = 108, Padding = new Thickness(5), Margin = new Thickness(0, 0, 6, 6),
                ToolTip = $"{label} · {attachment.Width} × {attachment.Height}\n클릭하여 확대 · 오른쪽 클릭으로 복사 또는 제거" };
            System.Windows.Automation.AutomationProperties.SetName(tile, label + " 확대");
            tile.Click += async (_, _) => await OpenImageAsync(attachment, label);
            var menu = new ContextMenu();
            var copy = new MenuItem { Header = "이미지 복사" }; copy.Click += async (_, _) => await CopyImageAsync(attachment);
            var remove = new MenuItem { Header = "이 메모에서 이미지 제거" };
            remove.Click += (_, _) => { draft.Note.Images.Remove(attachment); Dirty(draft); if (editing == draft) RenderImages(); };
            menu.Items.Add(copy); menu.Items.Add(remove); tile.ContextMenu = menu;
            imageTiles.Children.Add(tile);
            _ = LoadThumbnailAsync(attachment, preview, tile, ticket);
        }
    }
    private async Task LoadThumbnailAsync(NoteImage attachment, Image preview, Button tile, long ticket)
    {
        try { var bitmap = await Task.Run(() => imageStore.Load(attachment, 240)); if (ticket == imageRender) preview.Source = bitmap; }
        catch { if (ticket == imageRender) { tile.Content = new TextBlock { Text = "원본을 찾을 수\n없습니다", TextAlignment = TextAlignment.Center }; tile.ToolTip = "이미지 파일을 읽지 못했습니다. 메모의 첨부 정보는 보존됩니다."; } }
    }
    private async Task CopyImageAsync(NoteImage attachment, TextBlock? message = null)
    {
        try
        {
            // Construct WPF's clipboard data on the STA after background decoding.
            var image = await Task.Run(() => imageStore.ReadClipboardImage(attachment));
            Clipboard.SetDataObject(NoteImageStore.CopyData(image), true);
            (message ?? status).Text = "이미지를 복사했습니다. 채팅창에 붙여넣을 수 있습니다.";
        }
        catch (Exception error) { (message ?? status).Text = "이미지 복사 실패 · " + error.Message; }
    }
    private async Task OpenImageAsync(NoteImage attachment, string title)
    {
        try
        {
            var bitmap = await Task.Run(() => imageStore.Load(attachment));
            var owner = Window.GetWindow(this);
            if (owner is null || !owner.IsVisible) return;
            CreateImageViewer(owner, bitmap, attachment, title).ShowDialog();
        }
        catch (Exception error) { status.Text = "이미지를 열지 못했습니다 · " + error.Message; }
    }
    internal Window CreateImageViewer(Window owner, BitmapSource bitmap, NoteImage attachment, string title)
    {
            var window = new Window { Owner = owner, Title = title, Width = 960, Height = 720, MinWidth = 440, MinHeight = 320,
                WindowStartupLocation = WindowStartupLocation.CenterOwner, ShowInTaskbar = false, Background = WorkspaceAppearance.Surface };
            var layout = new DockPanel { Margin = new Thickness(16) };
            var message = new TextBlock { Text = $"{attachment.Width} × {attachment.Height} · Esc로 닫기", Foreground = WorkspaceAppearance.Muted, FontSize = 12,
                TextWrapping = TextWrapping.Wrap, VerticalAlignment = VerticalAlignment.Center };
            var bar = new DockPanel { Margin = new Thickness(0, 0, 0, 12) };
            var actions = new StackPanel { Orientation = Orientation.Horizontal };
            var copy = WorkspaceAppearance.Tool(new Button { Content = "이미지 복사", Margin = new Thickness(0, 0, 8, 0) });
            var close = WorkspaceAppearance.Tool(new Button { Content = "닫기", IsCancel = true });
            copy.Click += async (_, _) => await CopyImageAsync(attachment, message); close.Click += (_, _) => window.Close();
            var full = new CheckBox { Content = "원본 크기", Foreground = WorkspaceAppearance.Muted, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 14, 0) };
            actions.Children.Add(full); actions.Children.Add(copy); actions.Children.Add(close); DockPanel.SetDock(actions, Dock.Right); bar.Children.Add(actions); bar.Children.Add(message);
            DockPanel.SetDock(bar, Dock.Top); layout.Children.Add(bar);
            var image = new Image { Source = bitmap, Stretch = Stretch.Uniform };
            var scroll = new ScrollViewer { Content = image, HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled, VerticalScrollBarVisibility = ScrollBarVisibility.Disabled };
            full.Click += (_, _) =>
            {
                bool original = full.IsChecked == true;
                image.Width = original ? attachment.Width : double.NaN; image.Height = original ? attachment.Height : double.NaN;
                scroll.HorizontalScrollBarVisibility = scroll.VerticalScrollBarVisibility = original ? ScrollBarVisibility.Auto : ScrollBarVisibility.Disabled;
            };
            layout.Children.Add(scroll); window.Content = layout;
            window.PreviewKeyDown += async (_, e) => { if (e.Key == Key.C && Keyboard.Modifiers == ModifierKeys.Control) { e.Handled = true; await CopyImageAsync(attachment, message); } };
            return window;
    }
}
