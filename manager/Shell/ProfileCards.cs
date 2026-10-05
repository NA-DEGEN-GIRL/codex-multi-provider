using System.Windows;
using System.Windows.Controls;
using System.Windows.Markup;

namespace Codex.ControlCenter.Shell;

// Profile presentations. Every value arrives through Choice.Card
// (ProfileCardData); this file never reads JSON, never measures text and never
// decides what a string means.
//
//  * Rail: a 44-DIP avatar per profile (short label, provider tint, usage
//    rings) with a status dot beneath it. The profile list starts this way.
//  * Card: the expanded panel. Name and status pill, the two usage values,
//    one provider chip and at most one short warning.
//  * Summary: the selected profile in the workspace header: name, status and
//    provider, then labelled usage gauges.
// Everything longer (resets, redeem count, subscription, provider messages,
// SSH hosts, last check) lives in one rich tooltip shared by all three.
//
// Avatar rings, in a 48-DIP box: the outer ring is the weekly window (or the
// only known one), the inner ring the 5-hour window when both are known.
//
// The whole avatar or card is the drag handle (Tag "ProfileDragItem"):
// ProfileOrdering turns a press into a click or, past the drag threshold, a
// reorder. Templates are parsed once and shared.
internal static class ProfileCards
{
    // Parsed on first use: the markup strings below are built by static
    // initializers that run in declaration order.
    private static DataTemplate? card, railItem, summary, tip;
    private static Style? row, profileRow, railRow;

    internal static DataTemplate Create() => card ??= Parse<DataTemplate>(CardMarkup);
    internal static DataTemplate Rail() => railItem ??= Parse<DataTemplate>(RailMarkup);
    internal static DataTemplate Summary() => summary ??= Parse<DataTemplate>(SummaryMarkup);
    // The rich profile tooltip on its own, for self-tests that lay it out offscreen.
    internal static DataTemplate Tip() => tip ??= Parse<DataTemplate>(TipOnlyMarkup);

    // Card chrome shared by expanded profiles and task shortcuts: an 8-DIP
    // card whose selection is an accent border. It is based on the shared list
    // item style only to inherit its tooltip and text defaults.
    internal static Style ContainerStyle() => row ??= Based(Parse<Style>(RowMarkup));
    // Profile cards carry the rich tooltip in their template instead.
    internal static Style ProfileContainerStyle()
    {
        if (profileRow is not null) return profileRow;
        var style = new Style(typeof(ListBoxItem), ContainerStyle());
        style.Setters.Add(new Setter(FrameworkElement.ToolTipProperty, null));
        style.Seal();
        return profileRow = style;
    }
    internal static Style RailContainerStyle() => railRow ??= Based(Parse<Style>(RailRowMarkup));

    private static T Parse<T>(string markup) => (T)XamlReader.Parse(markup);
    private static Style Based(Style style)
    {
        if (Application.Current?.TryFindResource(typeof(ListBoxItem)) is Style inherited) style.BasedOn = inherited;
        return style;
    }

    private const string Namespaces = """
        xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        xmlns:local="clr-namespace:Codex.ControlCenter.Shell;assembly=Codex.ControlCenter"
        """;

    // Shared resources: the 4-DIP meter bar (both part names are required:
    // ProgressBar sizes PART_Indicator against PART_Track), chips, the usage
    // value forms and the rich tooltip.
    private const string Resources = """
        <Style x:Key="MeterBar" TargetType="ProgressBar">
          <Setter Property="Height" Value="4"/>
          <Setter Property="Minimum" Value="0"/>
          <Setter Property="Maximum" Value="100"/>
          <Setter Property="IsHitTestVisible" Value="False"/>
          <Setter Property="Background" Value="#2A2E37"/>
          <Setter Property="BorderThickness" Value="0"/>
          <Setter Property="VerticalAlignment" Value="Center"/>
          <Setter Property="Template">
            <Setter.Value>
              <ControlTemplate TargetType="ProgressBar">
                <Grid UseLayoutRounding="True">
                  <Border x:Name="PART_Track" Background="{TemplateBinding Background}" CornerRadius="2"/>
                  <Border x:Name="PART_Indicator" Background="{TemplateBinding Foreground}" CornerRadius="2" HorizontalAlignment="Left"/>
                </Grid>
              </ControlTemplate>
            </Setter.Value>
          </Setter>
        </Style>
        <Style x:Key="Chip" TargetType="Border">
          <Setter Property="CornerRadius" Value="4"/>
          <Setter Property="Padding" Value="6,1,6,2"/>
          <Setter Property="Margin" Value="0,6,6,0"/>
          <Setter Property="Background" Value="#2C3044"/>
          <Setter Property="VerticalAlignment" Value="Center"/>
        </Style>
        <!-- Card usage: "주간 97%" over a thin bar; an old value is dimmed with a clock icon. -->
        <DataTemplate x:Key="CardMeter">
          <StackPanel Margin="0,0,12,0" Background="Transparent" ToolTip="{Binding Detail}">
            <StackPanel Orientation="Horizontal">
              <TextBlock Text="{Binding Short}" FontSize="12" Foreground="#9FA7B7" TextWrapping="NoWrap" VerticalAlignment="Center"/>
              <TextBlock Text="{Binding Value}" FontSize="13" FontWeight="SemiBold" Foreground="{Binding ValueBrush}"
                         TextWrapping="NoWrap" Margin="5,0,0,0" VerticalAlignment="Center"/>
              <TextBlock Text="&#xE823;" FontFamily="Segoe Fluent Icons, Segoe MDL2 Assets" FontSize="10" Foreground="#7F8899"
                         Margin="4,1,0,0" VerticalAlignment="Center" Visibility="{Binding StaleVisibility}"/>
            </StackPanel>
            <ProgressBar Style="{StaticResource MeterBar}" Height="3" Margin="0,4,0,0" Foreground="{Binding ValueBrush}"
                         Value="{Binding Remaining}" Visibility="{Binding MeterVisibility}"/>
          </StackPanel>
        </DataTemplate>
        <!-- Header usage: a ring gauge with the window name and a large percentage. -->
        <DataTemplate x:Key="Gauge">
          <StackPanel Orientation="Horizontal" Margin="0,0,20,0" Background="Transparent" ToolTip="{Binding Detail}">
            <Grid Width="26" Height="26" VerticalAlignment="Center">
              <Viewbox Stretch="Uniform">
                <Grid Width="48" Height="48">
                  <Ellipse Stroke="#2A2E37" StrokeThickness="5"/>
                  <Path Data="{Binding GaugeArc}" Stroke="{Binding RingBrush}" StrokeThickness="5"
                        StrokeStartLineCap="Round" StrokeEndLineCap="Round"/>
                </Grid>
              </Viewbox>
            </Grid>
            <TextBlock Text="{Binding Short}" FontSize="12" Foreground="#9FA7B7" TextWrapping="NoWrap" Margin="8,0,0,0" VerticalAlignment="Center"/>
            <TextBlock Text="{Binding Value}" FontSize="17" FontWeight="SemiBold" Foreground="{Binding ValueBrush}"
                       TextWrapping="NoWrap" Margin="5,0,0,0" VerticalAlignment="Center"/>
            <TextBlock Text="&#xE823;" FontFamily="Segoe Fluent Icons, Segoe MDL2 Assets" FontSize="11" Foreground="#7F8899"
                       Margin="5,2,0,0" VerticalAlignment="Center" Visibility="{Binding StaleVisibility}"/>
          </StackPanel>
        </DataTemplate>
        <!-- Tooltip usage: the full value, a bar and the window's reset time. -->
        <DataTemplate x:Key="TipMeter">
          <StackPanel Margin="0,6,0,0">
            <Grid>
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="*"/>
              </Grid.ColumnDefinitions>
              <TextBlock Text="{Binding Label}" FontSize="12" Foreground="#9FA7B7" TextWrapping="NoWrap" VerticalAlignment="Center"/>
              <TextBlock Grid.Column="1" Text="{Binding Text}" FontSize="12" FontWeight="SemiBold" Foreground="{Binding ValueBrush}"
                         TextWrapping="NoWrap" Margin="6,0,0,0" VerticalAlignment="Center"/>
              <ProgressBar Grid.Column="2" Style="{StaticResource MeterBar}" MinWidth="40" Margin="10,0,0,0"
                           Foreground="{Binding ValueBrush}" Value="{Binding Remaining}" Visibility="{Binding MeterVisibility}"/>
            </Grid>
            <TextBlock Text="{Binding ResetText}" FontSize="12" Foreground="#8E9BB2" TextWrapping="NoWrap" Margin="0,2,0,0"
                       Visibility="{Binding ResetVisibility}"/>
          </StackPanel>
        </DataTemplate>
        <DataTemplate x:Key="ProfileTip">
          <StackPanel MinWidth="240" MaxWidth="340">
            <TextBlock Text="{Binding Card.Name}" FontSize="13" FontWeight="SemiBold" TextWrapping="Wrap"/>
            <StackPanel Orientation="Horizontal" Margin="0,6,0,0">
              <Ellipse Width="7" Height="7" Margin="0,0,6,0" VerticalAlignment="Center" Fill="{Binding Card.PillBrush}"/>
              <TextBlock Text="{Binding Card.Pill}" FontSize="12" Foreground="{Binding Card.PillBrush}" VerticalAlignment="Center"/>
              <TextBlock Text=" · " FontSize="12" Foreground="#8E9BB2" VerticalAlignment="Center"/>
              <TextBlock Text="{Binding Card.ProviderModel}" FontSize="12" Foreground="{Binding Card.TintTextBrush}" VerticalAlignment="Center"/>
            </StackPanel>
            <TextBlock Text="{Binding Card.NoticeDetail}" FontSize="12" Foreground="{Binding Card.NoticeDetailBrush}" TextWrapping="Wrap"
                       Margin="0,6,0,0" Visibility="{Binding Card.NoticeDetailVisibility}"/>
            <ContentControl Content="{Binding Card.Primary}" ContentTemplate="{StaticResource TipMeter}" Visibility="{Binding Card.PrimaryVisibility}"/>
            <ContentControl Content="{Binding Card.Secondary}" ContentTemplate="{StaticResource TipMeter}" Visibility="{Binding Card.SecondaryVisibility}"/>
            <TextBlock Text="{Binding Card.RedeemLine}" FontSize="12" Foreground="#8E9BB2" Margin="0,4,0,0"
                       ToolTip="사용량 한도를 초기화할 수 있는 남은 리딤 횟수입니다. 확인되지 않은 값은 0회로 표시하지 않습니다."
                       Visibility="{Binding Card.RedeemLineVisibility}"/>
            <TextBlock Text="{Binding Card.UsageHint}" FontSize="12" Foreground="#8E9BB2" Margin="0,4,0,0" TextWrapping="Wrap"
                       Visibility="{Binding Card.UsageHintTipVisibility}"/>
            <Border Height="1" Background="#3A404C" Margin="0,8,0,2"/>
            <TextBlock Text="{Binding AgentBadge}" FontSize="12" Foreground="#C6CFFF" Margin="0,4,0,0" Visibility="{Binding AgentBadgeVisibility}"/>
            <TextBlock Text="{Binding Card.SubscriptionText}" FontSize="12" Foreground="#AEB6C5" Margin="0,4,0,0" Visibility="{Binding Card.SubscriptionVisibility}"/>
            <TextBlock Text="{Binding Card.Email}" FontSize="12" Foreground="#9FA7B7" Margin="0,4,0,0" Visibility="{Binding Card.EmailVisibility}"/>
            <TextBlock Text="{Binding Card.SshDetail}" FontSize="12" Foreground="#AEB6C5" Margin="0,4,0,0" TextWrapping="Wrap" Visibility="{Binding Card.SshVisibility}"/>
            <TextBlock Text="{Binding Card.Cache}" FontSize="12" Foreground="{Binding Card.CacheBrush}" Margin="0,4,0,0" TextWrapping="Wrap" Visibility="{Binding Card.CacheVisibility}"/>
          </StackPanel>
        </DataTemplate>
        """;

    // The rich tooltip with the list gesture hint, for avatars and cards.
    private const string ListTip = """
        <ToolTip DataContext="{Binding PlacementTarget.DataContext, RelativeSource={RelativeSource Self} }"
                 Placement="Right" HorizontalOffset="10">
          <StackPanel>
            <ContentControl Content="{Binding}" ContentTemplate="{StaticResource ProfileTip}"/>
            <TextBlock Text="클릭해 열기 · 끌어서 순서 변경 · 오른쪽 클릭으로 관리" FontSize="11" Foreground="#8E9BB2" Margin="0,8,0,0"/>
          </StackPanel>
        </ToolTip>
        """;

    // Tinted disc with the short label inside the usage rings.
    private static string Avatar(double size, double font, string valign) => $$"""
        <Grid Width="{{size}}" Height="{{size}}" VerticalAlignment="{{valign}}" HorizontalAlignment="Center">
          <Viewbox Stretch="Uniform">
            <Grid Width="48" Height="48">
              <Ellipse Stroke="#2A2E37" StrokeThickness="3" Visibility="{Binding Card.RingVisibility}"/>
              <Path Data="{Binding Card.RingArc}" Stroke="{Binding Card.RingBrush}" StrokeThickness="3"
                    StrokeStartLineCap="Round" StrokeEndLineCap="Round"/>
              <Ellipse Width="39" Height="39" Stroke="#2A2E37" StrokeThickness="2.5" Visibility="{Binding Card.InnerRingVisibility}"/>
              <Path Data="{Binding Card.InnerRingArc}" Stroke="{Binding Card.InnerRingBrush}" StrokeThickness="2.5"
                    StrokeStartLineCap="Round" StrokeEndLineCap="Round" Visibility="{Binding Card.InnerRingVisibility}"/>
              <Ellipse Width="{Binding Card.DiscSize}" Height="{Binding Card.DiscSize}" Fill="{Binding Card.TintBrush}"/>
            </Grid>
          </Viewbox>
          <TextBlock Text="{Binding Card.Short}" FontSize="{{font}}" FontWeight="Bold" Foreground="{Binding Card.TintTextBrush}"
                     HorizontalAlignment="Center" VerticalAlignment="Center" TextWrapping="NoWrap"/>
        </Grid>
        """;

    private const string StatusPill = """
        <Border CornerRadius="10" Padding="7,1,8,2" Background="{Binding Card.PillBackground}" VerticalAlignment="Top">
          <StackPanel Orientation="Horizontal">
            <Ellipse Width="6" Height="6" Margin="0,0,5,0" VerticalAlignment="Center" Fill="{Binding Card.PillBrush}"/>
            <TextBlock Text="{Binding Card.Pill}" FontSize="12" Foreground="{Binding Card.PillBrush}"
                       TextWrapping="NoWrap" VerticalAlignment="Center"/>
          </StackPanel>
        </Border>
        """;

    private const string WarningChip = """
        <Border Style="{StaticResource Chip}" Background="#3A3020" Visibility="{Binding Card.WarningVisibility}"
                ToolTip="{Binding Card.WarningDetail}" ToolTipService.ShowDuration="30000">
          <StackPanel Orientation="Horizontal">
            <TextBlock Text="&#xE7BA;" FontFamily="Segoe Fluent Icons, Segoe MDL2 Assets" FontSize="10" Foreground="#E5B773"
                       Margin="0,1,5,0" VerticalAlignment="Center"/>
            <TextBlock Text="{Binding Card.Warning}" FontSize="12" Foreground="#E5B773" TextWrapping="NoWrap" VerticalAlignment="Center"/>
          </StackPanel>
        </Border>
        """;

    private static readonly string CardMarkup = $$"""
        <DataTemplate {{Namespaces}}>
          <DataTemplate.Resources>
            {{Resources}}
          </DataTemplate.Resources>
          <Grid x:Name="ProfileCard" Tag="ProfileDragItem" Background="Transparent" ToolTipService.ShowDuration="30000">
            <Grid.ToolTip>
              {{ListTip}}
            </Grid.ToolTip>
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="Auto"/>
              <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            {{Avatar(40, 12, "Top")}}
            <StackPanel Grid.Column="1" Margin="10,0,0,0">
              <Grid>
                <Grid.ColumnDefinitions>
                  <ColumnDefinition Width="*"/>
                  <ColumnDefinition Width="Auto"/>
                </Grid.ColumnDefinitions>
                <!-- Two lines hold ordinary names whole; only a longer one ends in an ellipsis. -->
                <TextBlock x:Name="ProfileName" Text="{Binding Card.Name}" FontSize="14" FontWeight="SemiBold" Foreground="#E9ECF2"
                           TextWrapping="Wrap" TextTrimming="CharacterEllipsis" LineHeight="19" LineStackingStrategy="BlockLineHeight"
                           MaxHeight="38" VerticalAlignment="Top"/>
                <ContentControl Grid.Column="1" Margin="8,0,0,0" Focusable="False" IsTabStop="False" VerticalAlignment="Top">
                  {{StatusPill}}
                </ContentControl>
              </Grid>
              <TextBlock Text="{Binding Card.Email}" FontSize="12" Foreground="#9FA7B7" Margin="0,4,0,0"
                         TextWrapping="NoWrap" TextTrimming="CharacterEllipsis" ToolTip="{Binding Card.Email}"
                         Visibility="{Binding Card.EmailVisibility}"/>
              <UniformGrid Columns="2" Margin="0,8,0,0" Visibility="{Binding Card.UsageVisibility}">
                <ContentControl Content="{Binding Card.Primary}" ContentTemplate="{StaticResource CardMeter}"
                                Visibility="{Binding Card.PrimaryVisibility}" Focusable="False" IsTabStop="False"/>
                <ContentControl Content="{Binding Card.Secondary}" ContentTemplate="{StaticResource CardMeter}"
                                Visibility="{Binding Card.SecondaryVisibility}" Focusable="False" IsTabStop="False"/>
              </UniformGrid>
              <WrapPanel Margin="0,2,0,0">
                <Border Style="{StaticResource Chip}" Background="{Binding Card.TintBrush}">
                  <TextBlock Text="{Binding Card.ProviderModel}" FontSize="12" FontWeight="SemiBold" Foreground="{Binding Card.TintTextBrush}" TextWrapping="NoWrap"/>
                </Border>
                <Border Style="{StaticResource Chip}" Visibility="{Binding AgentBadgeVisibility}">
                  <TextBlock Text="{Binding AgentChip}" FontSize="12" Foreground="#C6CFFF" TextWrapping="NoWrap"/>
                </Border>
                <Border Style="{StaticResource Chip}" Background="#262B34" Visibility="{Binding Card.CacheChipVisibility}">
                  <TextBlock Text="{Binding Card.CacheChip}" FontSize="12" Foreground="{Binding Card.CacheChipBrush}" TextWrapping="NoWrap"/>
                </Border>
                {{WarningChip}}
              </WrapPanel>
            </StackPanel>
          </Grid>
        </DataTemplate>
        """;

    // Rail cell: the avatar with a concentric selection ring (same grid, same
    // centre), the selection bar on the rail edge aligned with that centre, and
    // the status dot beneath, clear of every ring.
    private static readonly string RailMarkup = $$"""
        <DataTemplate {{Namespaces}}>
          <DataTemplate.Resources>
            {{Resources}}
          </DataTemplate.Resources>
          <Grid x:Name="RailAvatar" Tag="ProfileDragItem" Background="Transparent" Height="66" ToolTipService.ShowDuration="30000">
            <Grid.ToolTip>
              {{ListTip}}
            </Grid.ToolTip>
            <Border x:Name="Indicator" Width="3" Height="28" CornerRadius="0,2,2,0" Background="#A3C1FF"
                    HorizontalAlignment="Left" VerticalAlignment="Top" Margin="0,14,0,0" Visibility="Hidden"/>
            <Grid x:Name="AvatarFrame" Width="54" Height="54" HorizontalAlignment="Center" VerticalAlignment="Top" Margin="0,1,0,0">
              <Ellipse x:Name="Halo" Fill="Transparent"/>
              <Ellipse x:Name="SelectionRing" Stroke="#A3C1FF" StrokeThickness="2" Visibility="Hidden"/>
              {{Avatar(44, 13, "Center")}}
            </Grid>
            <Ellipse x:Name="StatusDot" Width="8" Height="8" HorizontalAlignment="Center" VerticalAlignment="Bottom" Margin="0,0,0,1"
                     Fill="{Binding Card.DotFill}" Stroke="{Binding Card.DotBrush}" StrokeThickness="1.5"/>
          </Grid>
          <DataTemplate.Triggers>
            <DataTrigger Binding="{Binding IsMouseOver, RelativeSource={RelativeSource AncestorType=ListBoxItem} }" Value="True">
              <Setter TargetName="Halo" Property="Fill" Value="#232831"/>
            </DataTrigger>
            <DataTrigger Binding="{Binding IsSelected, RelativeSource={RelativeSource AncestorType=ListBoxItem} }" Value="True">
              <Setter TargetName="SelectionRing" Property="Visibility" Value="Visible"/>
              <Setter TargetName="Indicator" Property="Visibility" Value="Visible"/>
            </DataTrigger>
          </DataTemplate.Triggers>
        </DataTemplate>
        """;

    // Header: line 1 name, status pill and provider chip (the name shortens
    // first); line 2 the usage gauges and at most one warning chip.
    private static readonly string SummaryMarkup = $$"""
        <DataTemplate {{Namespaces}}>
          <DataTemplate.Resources>
            {{Resources}}
          </DataTemplate.Resources>
          <Grid x:Name="ProfileSummary">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="Auto"/>
              <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            <Grid Background="Transparent" VerticalAlignment="Center" ToolTipService.ShowDuration="30000">
              <Grid.ToolTip>
                <ToolTip DataContext="{Binding PlacementTarget.DataContext, RelativeSource={RelativeSource Self} }"
                         Content="{Binding}" ContentTemplate="{StaticResource ProfileTip}"/>
              </Grid.ToolTip>
              {{Avatar(46, 13, "Center")}}
            </Grid>
            <StackPanel Grid.Column="1" Margin="12,0,0,0" VerticalAlignment="Center">
              <local:HeadlinePanel>
                <TextBlock x:Name="SummaryName" Text="{Binding Card.Name}" FontSize="19" FontWeight="SemiBold" Foreground="#E9ECF2"
                           TextWrapping="NoWrap" TextTrimming="CharacterEllipsis" ToolTip="{Binding Card.Name}" VerticalAlignment="Center"/>
                <ContentControl Margin="10,1,0,0" Focusable="False" IsTabStop="False" VerticalAlignment="Center">
                  {{StatusPill}}
                </ContentControl>
                <Border CornerRadius="4" Padding="7,1,7,2" Margin="6,1,0,0" Background="{Binding Card.TintBrush}" VerticalAlignment="Center">
                  <TextBlock Text="{Binding Card.ProviderModel}" FontSize="12" FontWeight="SemiBold" Foreground="{Binding Card.TintTextBrush}" TextWrapping="NoWrap"/>
                </Border>
              </local:HeadlinePanel>
              <!-- Whole gauges only: a refresh never wraps this line or resizes the viewport. -->
              <local:FitPanel Margin="0,8,0,0" ClipToBounds="True" Visibility="{Binding Card.UsageVisibility}">
                <ContentControl Content="{Binding Card.Primary}" ContentTemplate="{StaticResource Gauge}"
                                Visibility="{Binding Card.PrimaryVisibility}" Focusable="False" IsTabStop="False"/>
                <ContentControl Content="{Binding Card.Secondary}" ContentTemplate="{StaticResource Gauge}"
                                Visibility="{Binding Card.SecondaryVisibility}" Focusable="False" IsTabStop="False"/>
                <ContentControl Focusable="False" IsTabStop="False" VerticalAlignment="Center" Margin="0,-6,0,0">
                  {{WarningChip}}
                </ContentControl>
              </local:FitPanel>
              <ContentControl Focusable="False" IsTabStop="False" HorizontalAlignment="Left" Margin="0,2,0,0"
                              Visibility="{Binding Card.WarningOnlyVisibility}">
                {{WarningChip}}
              </ContentControl>
              <TextBlock Text="{Binding Card.Email}" FontSize="12" Foreground="#9FA7B7" Margin="0,6,0,0"
                         TextWrapping="NoWrap" TextTrimming="CharacterEllipsis" ToolTip="{Binding Card.Email}"
                         Visibility="{Binding Card.EmailVisibility}"/>
            </StackPanel>
          </Grid>
        </DataTemplate>
        """;

    private static readonly string TipOnlyMarkup = $$"""
        <DataTemplate {{Namespaces}}>
          <DataTemplate.Resources>
            {{Resources}}
          </DataTemplate.Resources>
          <ContentControl Content="{Binding}" ContentTemplate="{StaticResource ProfileTip}"/>
        </DataTemplate>
        """;

    private static readonly string RowMarkup = $$"""
        <Style {{Namespaces}} TargetType="ListBoxItem">
          <Setter Property="Padding" Value="10,10,8,10"/>
          <Setter Property="Margin" Value="0,0,0,6"/>
          <Setter Property="Background" Value="#23262D"/>
          <Setter Property="BorderBrush" Value="#23262D"/>
          <Setter Property="HorizontalContentAlignment" Value="Stretch"/>
          <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
          <Setter Property="ToolTip" Value="{Binding Hint}"/>
          <Setter Property="Template">
            <Setter.Value>
              <ControlTemplate TargetType="ListBoxItem">
                <!-- The 1.5-DIP border is always reserved, so text never shifts when a card is selected.
                     The tag makes the whole profile card, padding included, a drag handle; the
                     shortcut list has no ProfileOrdering and ignores it. -->
                <Border x:Name="Chrome" Tag="ProfileDragItem" Background="{TemplateBinding Background}" BorderBrush="{TemplateBinding BorderBrush}"
                        BorderThickness="1.5" CornerRadius="8" UseLayoutRounding="True" Padding="{TemplateBinding Padding}">
                  <ContentPresenter/>
                </Border>
                <ControlTemplate.Triggers>
                  <Trigger Property="IsEnabled" Value="False">
                    <Setter TargetName="Chrome" Property="Opacity" Value="0.5"/>
                  </Trigger>
                </ControlTemplate.Triggers>
              </ControlTemplate>
            </Setter.Value>
          </Setter>
          <Style.Triggers>
            <Trigger Property="IsMouseOver" Value="True">
              <Setter Property="Background" Value="#292D36"/>
              <Setter Property="BorderBrush" Value="#292D36"/>
            </Trigger>
            <Trigger Property="IsSelected" Value="True">
              <Setter Property="Background" Value="#29303F"/>
              <Setter Property="BorderBrush" Value="#A3C1FF"/>
            </Trigger>
            <!-- Declared after selection so keyboard focus stays visible on top of it. -->
            <Trigger Property="IsKeyboardFocused" Value="True">
              <Setter Property="BorderBrush" Value="#E2ECFF"/>
            </Trigger>
          </Style.Triggers>
        </Style>
        """;

    // The rail cell draws its own selection (RailMarkup); the container adds
    // nothing but a keyboard-only focus outline around the whole cell.
    private static readonly string RailRowMarkup = $$"""
        <Style {{Namespaces}} TargetType="ListBoxItem">
          <Setter Property="Padding" Value="0"/>
          <Setter Property="Margin" Value="0,1,0,1"/>
          <Setter Property="HorizontalContentAlignment" Value="Stretch"/>
          <Setter Property="ToolTip" Value="{x:Null}"/>
          <Setter Property="AutomationProperties.Name" Value="{Binding Card.AccessibleName}"/>
          <Setter Property="FocusVisualStyle">
            <Setter.Value>
              <Style TargetType="Control">
                <Setter Property="Template">
                  <Setter.Value>
                    <ControlTemplate>
                      <Rectangle Margin="4,0,4,0" RadiusX="10" RadiusY="10" Stroke="#E2ECFF" StrokeThickness="1.5" StrokeDashArray="2 1.5"/>
                    </ControlTemplate>
                  </Setter.Value>
                </Setter>
              </Style>
            </Setter.Value>
          </Setter>
          <Setter Property="Template">
            <Setter.Value>
              <ControlTemplate TargetType="ListBoxItem">
                <ContentPresenter x:Name="Content"/>
                <ControlTemplate.Triggers>
                  <Trigger Property="IsEnabled" Value="False">
                    <Setter TargetName="Content" Property="Opacity" Value="0.5"/>
                  </Trigger>
                </ControlTemplate.Triggers>
              </ControlTemplate>
            </Setter.Value>
          </Setter>
        </Style>
        """;
}
