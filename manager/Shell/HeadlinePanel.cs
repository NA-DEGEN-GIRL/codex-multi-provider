using System.Windows;
using System.Windows.Controls;

namespace Codex.ControlCenter.Shell;

// One line led by a name: the first child takes whatever width the others
// leave and trims with its own ellipsis; the others keep their full size and
// are left out from the end once even MinLeadWidth would not leave room. Used
// for "name · status pill · provider chip" in the workspace header.
// Public so the XAML templates (XamlReader) can create it.
public sealed class HeadlinePanel : Panel
{
    public static readonly DependencyProperty MinLeadWidthProperty = DependencyProperty.Register(
        nameof(MinLeadWidth), typeof(double), typeof(HeadlinePanel),
        new FrameworkPropertyMetadata(140d, FrameworkPropertyMetadataOptions.AffectsMeasure));

    public double MinLeadWidth
    {
        get => (double)GetValue(MinLeadWidthProperty);
        set => SetValue(MinLeadWidthProperty, value);
    }

    private int _kept;
    private double _lead;

    protected override Size MeasureOverride(Size availableSize)
    {
        var children = InternalChildren;
        if (children.Count == 0) return default;
        var infinite = new Size(double.PositiveInfinity, availableSize.Height);
        double others = 0, height = 0;
        for (var index = 1; index < children.Count; index++)
        {
            children[index].Measure(infinite);
            others += children[index].DesiredSize.Width;
        }
        children[0].Measure(infinite);
        var lead = children[0].DesiredSize.Width;
        _kept = children.Count - 1;
        if (!double.IsInfinity(availableSize.Width))
        {
            while (_kept > 0 && Math.Min(lead, MinLeadWidth) + others > availableSize.Width)
                others -= children[_kept--].DesiredSize.Width;
            lead = Math.Max(0, Math.Min(lead, availableSize.Width - others));
            children[0].Measure(new Size(lead, availableSize.Height));
        }
        _lead = lead;
        for (var index = 0; index <= _kept; index++) height = Math.Max(height, children[index].DesiredSize.Height);
        return new Size(lead + others, height);
    }

    protected override Size ArrangeOverride(Size finalSize)
    {
        var children = InternalChildren;
        double x = 0;
        for (var index = 0; index < children.Count; index++)
        {
            var width = index == 0 ? _lead : children[index].DesiredSize.Width;
            // A left-out child keeps no area, so it can neither render nor take input.
            children[index].Arrange(index <= _kept ? new Rect(x, 0, width, finalSize.Height) : new Rect(x, 0, 0, 0));
            if (index <= _kept) x += width;
        }
        return finalSize;
    }
}
