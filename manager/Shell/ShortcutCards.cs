using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;

namespace Codex.ControlCenter.Shell;

internal static class ShortcutCards
{
    public static DataTemplate Create(RoutedEventHandler click)
    {
        var card = new FrameworkElementFactory(typeof(StackPanel));
        var open = Button("open", click);
        open.SetValue(Control.HorizontalContentAlignmentProperty, HorizontalAlignment.Stretch);
        var label = new FrameworkElementFactory(typeof(TextBlock));
        label.SetBinding(TextBlock.TextProperty, new Binding(nameof(Choice.Label)));
        label.SetValue(TextBlock.TextWrappingProperty, TextWrapping.Wrap);
        open.AppendChild(label);
        card.AppendChild(open);
        var actions = new FrameworkElementFactory(typeof(WrapPanel));
        foreach (var (key, text) in new[] { ("move", "계정 이동"), ("rename", "별칭 변경"), ("delete", "링크 삭제") })
        {
            var button = Button(key, click);
            button.SetValue(ContentControl.ContentProperty, text);
            button.SetValue(Control.FontSizeProperty, 12d);
            button.SetValue(Control.PaddingProperty, new Thickness(8, 5, 8, 5));
            button.SetValue(FrameworkElement.MarginProperty, new Thickness(0, 2, 5, 2));
            actions.AppendChild(button);
        }
        card.AppendChild(actions);
        return new DataTemplate { VisualTree = card };
    }

    private static FrameworkElementFactory Button(string action, RoutedEventHandler click)
    {
        var button = new FrameworkElementFactory(typeof(Button));
        button.SetValue(FrameworkElement.TagProperty, action);
        button.AddHandler(System.Windows.Controls.Button.ClickEvent, click);
        return button;
    }
}
