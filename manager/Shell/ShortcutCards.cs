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
        open.SetValue(Control.PaddingProperty, new Thickness(9, 8, 9, 8));
        open.SetBinding(FrameworkElement.ToolTipProperty, new Binding(nameof(Choice.Label)));
        var content = new FrameworkElementFactory(typeof(StackPanel));
        var label = new FrameworkElementFactory(typeof(TextBlock));
        label.SetBinding(TextBlock.TextProperty, new Binding(nameof(Choice.ShortcutName)));
        label.SetValue(TextBlock.FontSizeProperty, 13d);
        label.SetValue(TextBlock.FontWeightProperty, FontWeights.SemiBold);
        label.SetValue(TextBlock.TextTrimmingProperty, TextTrimming.CharacterEllipsis);
        content.AppendChild(label);
        var target = new FrameworkElementFactory(typeof(TextBlock));
        target.SetBinding(TextBlock.TextProperty, new Binding(nameof(Choice.ShortcutTarget)));
        target.SetValue(TextBlock.FontSizeProperty, 11d);
        target.SetValue(TextBlock.ForegroundProperty, new System.Windows.Media.SolidColorBrush(System.Windows.Media.Color.FromRgb(159, 167, 183)));
        target.SetValue(TextBlock.TextTrimmingProperty, TextTrimming.CharacterEllipsis);
        target.SetValue(FrameworkElement.MarginProperty, new Thickness(0, 4, 0, 0));
        content.AppendChild(target);
        open.AppendChild(content);
        card.AppendChild(open);
        var actions = new FrameworkElementFactory(typeof(WrapPanel));
        foreach (var (key, text) in new[] { ("move", "계정 이동"), ("rename", "별칭 변경"), ("delete", "링크 삭제") })
        {
            var button = Button(key, click);
            button.SetValue(ContentControl.ContentProperty, text);
            button.SetValue(Control.FontSizeProperty, 11d);
            button.SetValue(Control.PaddingProperty, new Thickness(8, 4, 8, 4));
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
