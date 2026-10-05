using System.Windows;
using System.Windows.Controls;

namespace Codex.ControlCenter.Shell;

// A single horizontal line that shows whole children only: the first child
// that would not fit, and every child after it, is left out instead of being
// cut through the middle. The line height never changes with its content, so a
// header refresh cannot resize the native viewport below it.
// Public so the XAML card templates (XamlReader) can create it.
public sealed class FitPanel : Panel
{
    protected override Size MeasureOverride(Size availableSize)
    {
        double width = 0, height = 0;
        foreach (UIElement child in InternalChildren)
        {
            child.Measure(new Size(double.PositiveInfinity, availableSize.Height));
            width += child.DesiredSize.Width;
            height = Math.Max(height, child.DesiredSize.Height);
        }
        return new Size(double.IsInfinity(availableSize.Width) ? width : Math.Min(width, availableSize.Width), height);
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        double x = 0;
        var fits = true;
        foreach (UIElement child in InternalChildren)
        {
            var size = child.DesiredSize;
            fits &= x + size.Width <= finalSize.Width + 0.5;
            // A left-out child keeps no area, so it can neither render nor take input.
            child.Arrange(fits ? new Rect(x, 0, size.Width, finalSize.Height) : new Rect(x, 0, 0, 0));
            if (fits) x += size.Width;
        }
        return finalSize;
    }
}
