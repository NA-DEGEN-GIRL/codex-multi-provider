using System.Windows;
using System.Windows.Controls;
using System.Windows.Markup;

namespace Codex.ControlCenter.Shell;

// Profile presentations. Every value arrives through Choice.Card
// (ProfileCardData); this file never reads JSON, never measures text and never
// decides what a string means.
//
//  * Rail: a 44-DIP avatar per profile (short label, provider tint, weekly ring,
//    status dot) with a rich tooltip. The profile list starts in this form.
//  * Card: the expanded panel. The name wraps to two lines, the status pill
//    sits top-right, each usage window has its own meter line, chips follow.
//  * Summary and Usage: the selected profile in the workspace header.
//
// The whole avatar or card is the drag handle (Tag "ProfileDragItem"):
// ProfileOrdering turns a press into a click or, past the drag threshold, a
// reorder. Templates are parsed once and shared.
internal static class ProfileCards
{
    // Parsed on first use: the markup strings below are built by static
    // initializers that run in declaration order.
    private static DataTemplate? card, railItem, summary, usage;
    private static Style? row, railRow;

    internal static DataTemplate Create() => card ??= Parse<DataTemplate>(CardMarkup);
    internal static DataTemplate Rail() => railItem ??= Parse<DataTemplate>(RailMarkup);
    internal static DataTemplate Summary() => summary ??= Parse<DataTemplate>(SummaryMarkup);
    internal static DataTemplate Usage() => usage ??= Parse<DataTemplate>(UsageMarkup);

    // Card chrome shared by expanded profiles and task shortcuts: an 8-DIP
    // card whose selection is an accent border. It is based on the shared list
    // item style only to inherit its tooltip and text defaults.
    internal static Style ContainerStyle() => row ??= Based(Parse<Style>(RowMarkup));
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

    // A 4-DIP rounded meter. Both part names are required: ProgressBar sizes
    // PART_Indicator against PART_Track, and the track stays visible when empty.
    private const string MeterStyle = """
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
          <Setter Property="Padding" Value="6,2,6,2"/>
          <Setter Property="Margin" Value="0,6,6,0"/>
          <Setter Property="Background" Value="#2C3044"/>
          <Setter Property="VerticalAlignment" Value="Center"/>
        </Style>
        <!-- One usage window: label, value and bar on one line, its reset below. -->
        <DataTemplate x:Key="Meter">
          <StackPanel Margin="0,6,0,0" ToolTip="{Binding Detail}">
            <Grid>
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="*"/>
              </Grid.ColumnDefinitions>
              <TextBlock Text="{Binding Label}" FontSize="12" Foreground="#9FA7B7" TextWrapping="NoWrap" VerticalAlignment="Center"/>
              <TextBlock Grid.Column="1" Text="{Binding Text}" FontSize="12" FontWeight="SemiBold" Foreground="{Binding ValueBrush}"
                         TextWrapping="NoWrap" Margin="6,0,0,0" VerticalAlignment="Center"/>
              <ProgressBar Grid.Column="2" Style="{StaticResource MeterBar}" MinWidth="24" Margin="10,0,0,0"
                           Foreground="{Binding ValueBrush}" Value="{Binding Remaining}" Visibility="{Binding MeterVisibility}"/>
            </Grid>
            <TextBlock Text="{Binding ResetText}" FontSize="12" Foreground="#8E9BB2" TextWrapping="NoWrap" Margin="0,2,0,0"
                       Visibility="{Binding ResetVisibility}"/>
          </StackPanel>
        </DataTemplate>
        <!-- The header's single-line form: a fixed 64-DIP bar keeps the row height stable. -->
        <DataTemplate x:Key="CompactMeter">
          <StackPanel Orientation="Horizontal" Margin="0,0,16,0" ToolTip="{Binding Detail}">
            <TextBlock Text="{Binding Label}" FontSize="12" Foreground="#9FA7B7" TextWrapping="NoWrap" VerticalAlignment="Center"/>
            <TextBlock Text="{Binding Text}" FontSize="12" FontWeight="SemiBold" Foreground="{Binding ValueBrush}"
                       TextWrapping="NoWrap" Margin="6,0,0,0" VerticalAlignment="Center"/>
            <ProgressBar Style="{StaticResource MeterBar}" Width="64" Margin="8,0,0,0"
                         Foreground="{Binding ValueBrush}" Value="{Binding Remaining}" Visibility="{Binding MeterVisibility}"/>
            <TextBlock Text="{Binding ResetText}" FontSize="12" Foreground="#8E9BB2" TextWrapping="NoWrap" Margin="8,0,0,0"
                       VerticalAlignment="Center" Visibility="{Binding ResetVisibility}"/>
          </StackPanel>
        </DataTemplate>
        """;

    // Tinted disc with the short label, the weekly ring around it (only when
    // known) and the status dot cut out of the given background.
    private static string Avatar(double size, double font, double dot, string cutout) => $$"""
        <Grid Width="{{size}}" Height="{{size}}" VerticalAlignment="Top" HorizontalAlignment="Center">
          <Viewbox Stretch="Uniform">
            <Grid Width="48" Height="48">
              <Ellipse Stroke="#2A2E37" StrokeThickness="3" Visibility="{Binding Card.RingVisibility}"/>
              <Path Data="{Binding Card.RingArc}" Stroke="{Binding Card.RingBrush}" StrokeThickness="3"
                    StrokeStartLineCap="Round" StrokeEndLineCap="Round"/>
              <Ellipse Width="38" Height="38" Fill="{Binding Card.TintBrush}"/>
            </Grid>
          </Viewbox>
          <TextBlock Text="{Binding Card.Short}" FontSize="{{font}}" FontWeight="Bold" Foreground="{Binding Card.TintTextBrush}"
                     HorizontalAlignment="Center" VerticalAlignment="Center" TextWrapping="NoWrap"/>
          <Grid Width="{{dot}}" Height="{{dot}}" HorizontalAlignment="Right" VerticalAlignment="Bottom" IsHitTestVisible="False">
            <Ellipse Fill="{{cutout}}"/>
            <Ellipse Margin="2.5" Fill="{Binding Card.DotFill}" Stroke="{Binding Card.DotBrush}" StrokeThickness="1.5"/>
          </Grid>
        </Grid>
        """;

    private const string StatusPill = """
        <Border CornerRadius="10" Padding="7,2,8,2" Background="{Binding Card.StatusPillBrush}" VerticalAlignment="Top">
          <StackPanel Orientation="Horizontal">
            <Ellipse Width="7" Height="7" Margin="0,0,5,0" VerticalAlignment="Center" Fill="{Binding Card.StatusBrush}"/>
            <TextBlock Text="{Binding Card.Status}" FontSize="12" Foreground="{Binding Card.StatusBrush}"
                       TextWrapping="NoWrap" VerticalAlignment="Center"/>
          </StackPanel>
        </Border>
        """;

    // Provider/model, subagent policy, SSH hosts and the profile notice.
    private const string Chips = """
        <Border Style="{StaticResource Chip}" Background="{Binding Card.TintBrush}" Visibility="{Binding Card.ExternalVisibility}">
          <TextBlock Text="{Binding Card.ProviderLabel}" FontSize="12" FontWeight="SemiBold" Foreground="{Binding Card.TintTextBrush}" TextWrapping="NoWrap"/>
        </Border>
        <Border Style="{StaticResource Chip}" Background="#262B34" Visibility="{Binding Card.ModelVisibility}" ToolTip="{Binding Card.Model}">
          <TextBlock Text="{Binding Card.Model}" FontSize="12" Foreground="#C9D0DC" TextWrapping="NoWrap" TextTrimming="CharacterEllipsis" MaxWidth="180"/>
        </Border>
        <Border Style="{StaticResource Chip}" Visibility="{Binding AgentBadgeVisibility}" ToolTip="{Binding AgentHint}">
          <TextBlock Text="{Binding AgentBadge}" FontSize="12" Foreground="#C6CFFF" TextWrapping="NoWrap"/>
        </Border>
        <Border Style="{StaticResource Chip}" Background="#262B34" Visibility="{Binding Card.SshVisibility}" ToolTip="{Binding Card.SshDetail}">
          <TextBlock Text="{Binding Card.Ssh}" FontSize="12" Foreground="#AEB6C5" TextWrapping="NoWrap"/>
        </Border>
        <Border Style="{StaticResource Chip}" Background="{Binding Card.NoticePillBrush}" Visibility="{Binding Card.NoticeVisibility}">
          <TextBlock Text="{Binding Card.Notice}" FontSize="12" Foreground="{Binding Card.NoticeBrush}" TextWrapping="NoWrap"/>
        </Border>
        """;

    private static readonly string CardMarkup = $$"""
        <DataTemplate {{Namespaces}}>
          <DataTemplate.Resources>
            {{MeterStyle}}
          </DataTemplate.Resources>
          <Grid x:Name="ProfileCard" Tag="ProfileDragItem" Background="Transparent">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="Auto"/>
              <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            {{Avatar(34, 12, 12, "{Binding Background, RelativeSource={RelativeSource AncestorType=ListBoxItem}}")}}
            <StackPanel Grid.Column="1" Margin="10,0,0,0">
              <Grid>
                <Grid.ColumnDefinitions>
                  <ColumnDefinition Width="*"/>
                  <ColumnDefinition Width="Auto"/>
                </Grid.ColumnDefinitions>
                <!-- Two lines hold ordinary names whole; only a longer one ends in an ellipsis. -->
                <TextBlock x:Name="ProfileName" Text="{Binding Card.Name}" FontSize="14" FontWeight="SemiBold" Foreground="#E9ECF2"
                           TextWrapping="Wrap" TextTrimming="CharacterEllipsis" LineHeight="19" LineStackingStrategy="BlockLineHeight"
                           MaxHeight="38" VerticalAlignment="Top" ToolTip="{Binding Card.Name}"/>
                <ContentControl Grid.Column="1" Margin="8,0,0,0" Focusable="False" IsTabStop="False" VerticalAlignment="Top">
                  {{StatusPill}}
                </ContentControl>
              </Grid>
              <TextBlock Text="{Binding Card.Email}" FontSize="12" Foreground="#9FA7B7" Margin="0,4,0,0"
                         TextWrapping="NoWrap" TextTrimming="CharacterEllipsis" ToolTip="{Binding Card.Email}"
                         Visibility="{Binding Card.EmailVisibility}"/>
              <ContentControl Content="{Binding Card.Primary}" ContentTemplate="{StaticResource Meter}"
                              Visibility="{Binding Card.PrimaryVisibility}" Focusable="False" IsTabStop="False"/>
              <ContentControl Content="{Binding Card.Secondary}" ContentTemplate="{StaticResource Meter}"
                              Visibility="{Binding Card.SecondaryVisibility}" Focusable="False" IsTabStop="False"/>
              <!-- Breaks between whole values only: a count or a time never splits. -->
              <WrapPanel Margin="0,4,0,0" Visibility="{Binding Card.ResetLineVisibility}">
                <TextBlock Text="{Binding Card.ResetText}" FontSize="12" Foreground="#8E9BB2" TextWrapping="NoWrap" Margin="0,0,12,0"/>
                <TextBlock Text="{Binding Card.RedeemText}" FontSize="12" Foreground="#8E9BB2" TextWrapping="NoWrap" Margin="0,0,12,0"
                           ToolTip="사용량 한도를 초기화할 수 있는 남은 리딤 횟수입니다. 확인되지 않은 값은 0회로 표시하지 않습니다."/>
                <TextBlock Text="{Binding Card.Freshness}" FontSize="12" Foreground="#8E9BB2" TextWrapping="NoWrap" Margin="0,0,12,0"
                           Visibility="{Binding Card.FreshnessVisibility}"/>
              </WrapPanel>
              <TextBlock Text="{Binding Card.Account}" FontSize="12" Foreground="#9FA7B7" TextWrapping="Wrap"
                         Margin="0,4,0,0" Visibility="{Binding Card.AccountVisibility}"/>
              <TextBlock Text="{Binding Card.UsageHint}" FontSize="12" Foreground="#8E9BB2" TextWrapping="Wrap"
                         Margin="0,4,0,0" Visibility="{Binding Card.UsageHintVisibility}"/>
              <!-- Selected task's prompt cache on this profile: warm or cold, minutes left and the
                   first request's rough cost. Wraps rather than trims so the cost stays whole. -->
              <TextBlock Text="{Binding Card.Cache}" FontSize="12" Foreground="{Binding Card.CacheBrush}"
                         TextWrapping="Wrap" Margin="0,4,0,0" Visibility="{Binding Card.CacheVisibility}"
                         ToolTip="선택한 작업의 프롬프트 캐시 추정입니다. 캐시는 계정·제공자마다 따로이며, 값은 요청 기록으로 계산한 근사치입니다."/>
              <WrapPanel Margin="0,2,0,0">
                {{Chips}}
              </WrapPanel>
            </StackPanel>
          </Grid>
        </DataTemplate>
        """;

    private static readonly string RailMarkup = $$"""
        <DataTemplate {{Namespaces}}>
          <DataTemplate.Resources>
            {{MeterStyle}}
            <DataTemplate x:Key="RailTip">
              <StackPanel MinWidth="220">
                <TextBlock Text="{Binding Card.Name}" FontSize="13" FontWeight="SemiBold" TextWrapping="Wrap"/>
                <StackPanel Orientation="Horizontal" Margin="0,6,0,0">
                  <Ellipse Width="7" Height="7" Margin="0,0,6,0" VerticalAlignment="Center" Fill="{Binding Card.StatusBrush}"/>
                  <TextBlock Text="{Binding Card.Status}" FontSize="12" Foreground="{Binding Card.StatusBrush}" VerticalAlignment="Center"/>
                  <TextBlock Text=" · " FontSize="12" Foreground="#8E9BB2" VerticalAlignment="Center"/>
                  <TextBlock Text="{Binding Card.ProviderLabel}" FontSize="12" Foreground="{Binding Card.TintTextBrush}" VerticalAlignment="Center"/>
                </StackPanel>
                <TextBlock Text="{Binding Card.Email}" FontSize="12" Foreground="#9FA7B7" Margin="0,4,0,0" Visibility="{Binding Card.EmailVisibility}"/>
                <ContentControl Content="{Binding Card.Primary}" ContentTemplate="{StaticResource Meter}" Visibility="{Binding Card.PrimaryVisibility}"/>
                <ContentControl Content="{Binding Card.Secondary}" ContentTemplate="{StaticResource Meter}" Visibility="{Binding Card.SecondaryVisibility}"/>
                <WrapPanel Margin="0,4,0,0" Visibility="{Binding Card.ResetLineVisibility}">
                  <TextBlock Text="{Binding Card.ResetText}" FontSize="12" Foreground="#8E9BB2" Margin="0,0,12,0"/>
                  <TextBlock Text="{Binding Card.RedeemText}" FontSize="12" Foreground="#8E9BB2" Margin="0,0,12,0"/>
                  <TextBlock Text="{Binding Card.Freshness}" FontSize="12" Foreground="#8E9BB2" Visibility="{Binding Card.FreshnessVisibility}"/>
                </WrapPanel>
                <TextBlock Text="{Binding Card.Model}" FontSize="12" Foreground="#C9D0DC" Margin="0,6,0,0" TextWrapping="Wrap" Visibility="{Binding Card.ModelVisibility}"/>
                <TextBlock Text="{Binding AgentBadge}" FontSize="12" Foreground="#C6CFFF" Margin="0,6,0,0" Visibility="{Binding AgentBadgeVisibility}"/>
                <TextBlock Text="{Binding Card.SshDetail}" FontSize="12" Foreground="#AEB6C5" Margin="0,4,0,0" TextWrapping="Wrap" Visibility="{Binding Card.SshVisibility}"/>
                <TextBlock Text="{Binding Card.Notice}" FontSize="12" Foreground="{Binding Card.NoticeBrush}" Margin="0,4,0,0" TextWrapping="Wrap" Visibility="{Binding Card.NoticeVisibility}"/>
                <TextBlock Text="{Binding Card.UsageHint}" FontSize="12" Foreground="#8E9BB2" Margin="0,4,0,0" TextWrapping="Wrap" Visibility="{Binding Card.UsageHintVisibility}"/>
                <TextBlock Text="{Binding Card.Cache}" FontSize="12" Foreground="{Binding Card.CacheBrush}" Margin="0,4,0,0" TextWrapping="Wrap" Visibility="{Binding Card.CacheVisibility}"/>
                <TextBlock Text="클릭해 열기 · 끌어서 순서 변경 · 오른쪽 클릭으로 관리" FontSize="11" Foreground="#8E9BB2" Margin="0,8,0,0"/>
              </StackPanel>
            </DataTemplate>
          </DataTemplate.Resources>
          <Grid x:Name="RailAvatar" Tag="ProfileDragItem" Background="Transparent" Height="56">
            <Grid.ToolTip>
              <ToolTip DataContext="{Binding PlacementTarget.DataContext, RelativeSource={RelativeSource Self} }"
                       Content="{Binding}" ContentTemplate="{StaticResource RailTip}" Placement="Right" HorizontalOffset="10"/>
            </Grid.ToolTip>
            {{Avatar(44, 13, 14, "#1A1C21")}}
          </Grid>
        </DataTemplate>
        """;

    private static readonly string SummaryMarkup = $$"""
        <DataTemplate {{Namespaces}}>
          <DataTemplate.Resources>
            {{MeterStyle}}
          </DataTemplate.Resources>
          <Grid x:Name="ProfileSummary">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="Auto"/>
              <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            {{Avatar(40, 13, 13, "#17191E")}}
            <StackPanel Grid.Column="1" Margin="12,0,0,0">
              <TextBlock x:Name="SummaryName" Text="{Binding Card.Name}" FontSize="17" FontWeight="SemiBold" Foreground="#E9ECF2"
                         TextWrapping="NoWrap" TextTrimming="CharacterEllipsis" ToolTip="{Binding Card.Name}"/>
              <!-- Single lines that drop whole chips instead of wrapping: a state refresh never resizes the viewport. -->
              <local:FitPanel ClipToBounds="True" Margin="0,-2,0,0">
                <ContentControl Margin="0,6,6,0" Focusable="False" IsTabStop="False">
                  {{StatusPill}}
                </ContentControl>
                <Border Style="{StaticResource Chip}" Background="{Binding Card.TintBrush}">
                  <TextBlock Text="{Binding Card.ProviderLabel}" FontSize="12" FontWeight="SemiBold" Foreground="{Binding Card.TintTextBrush}" TextWrapping="NoWrap"/>
                </Border>
                <Border Style="{StaticResource Chip}" Background="#262B34" Visibility="{Binding Card.ModelVisibility}" ToolTip="{Binding Card.Model}">
                  <TextBlock Text="{Binding Card.Model}" FontSize="12" Foreground="#C9D0DC" TextWrapping="NoWrap" TextTrimming="CharacterEllipsis" MaxWidth="220"/>
                </Border>
                <Border Style="{StaticResource Chip}" Visibility="{Binding AgentBadgeVisibility}" ToolTip="{Binding AgentHint}">
                  <TextBlock Text="{Binding AgentBadge}" FontSize="12" Foreground="#C6CFFF" TextWrapping="NoWrap"/>
                </Border>
                <Border Style="{StaticResource Chip}" Background="#262B34" Visibility="{Binding Card.SshVisibility}" ToolTip="{Binding Card.SshDetail}">
                  <TextBlock Text="{Binding Card.Ssh}" FontSize="12" Foreground="#AEB6C5" TextWrapping="NoWrap"/>
                </Border>
                <Border Style="{StaticResource Chip}" Background="{Binding Card.NoticePillBrush}" Visibility="{Binding Card.NoticeVisibility}">
                  <TextBlock Text="{Binding Card.Notice}" FontSize="12" Foreground="{Binding Card.NoticeBrush}" TextWrapping="NoWrap"/>
                </Border>
              </local:FitPanel>
              <TextBlock Text="{Binding Card.Email}" FontSize="12" Foreground="#9FA7B7" Margin="0,6,0,0"
                         TextWrapping="NoWrap" TextTrimming="CharacterEllipsis" ToolTip="{Binding Card.Email}"
                         Visibility="{Binding Card.EmailVisibility}"/>
            </StackPanel>
          </Grid>
        </DataTemplate>
        """;

    // The selected profile's usage under the header row, aligned with its name
    // and as wide as the workspace, so the meters never compete with the tools.
    private static readonly string UsageMarkup = $$"""
        <DataTemplate {{Namespaces}}>
          <DataTemplate.Resources>
            {{MeterStyle}}
          </DataTemplate.Resources>
          <local:FitPanel x:Name="ProfileUsage" ClipToBounds="True" Visibility="{Binding Card.UsageVisibility}">
            <ContentControl Content="{Binding Card.Primary}" ContentTemplate="{StaticResource CompactMeter}"
                            Visibility="{Binding Card.PrimaryVisibility}" Focusable="False" IsTabStop="False"/>
            <ContentControl Content="{Binding Card.Secondary}" ContentTemplate="{StaticResource CompactMeter}"
                            Visibility="{Binding Card.SecondaryVisibility}" Focusable="False" IsTabStop="False"/>
            <TextBlock Text="{Binding Card.ResetText}" FontSize="12" Foreground="#8E9BB2" TextWrapping="NoWrap" Margin="0,0,12,0"
                       VerticalAlignment="Center" Visibility="{Binding Card.ResetLineVisibility}"/>
            <TextBlock Text="{Binding Card.RedeemText}" FontSize="12" Foreground="#8E9BB2" TextWrapping="NoWrap" Margin="0,0,12,0"
                       VerticalAlignment="Center" Visibility="{Binding Card.ResetLineVisibility}"
                       ToolTip="사용량 한도를 초기화할 수 있는 남은 리딤 횟수입니다. 확인되지 않은 값은 0회로 표시하지 않습니다."/>
            <TextBlock Text="{Binding Card.Freshness}" FontSize="12" Foreground="#8E9BB2" TextWrapping="NoWrap" Margin="0,0,12,0"
                       VerticalAlignment="Center" Visibility="{Binding Card.FreshnessVisibility}"/>
            <TextBlock Text="{Binding Card.UsageHint}" FontSize="12" Foreground="#8E9BB2" TextWrapping="NoWrap"
                       VerticalAlignment="Center" Visibility="{Binding Card.UsageHintVisibility}"/>
          </local:FitPanel>
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

    private static readonly string RailRowMarkup = $$"""
        <Style {{Namespaces}} TargetType="ListBoxItem">
          <Setter Property="Padding" Value="0"/>
          <Setter Property="Margin" Value="0,2,0,2"/>
          <Setter Property="HorizontalContentAlignment" Value="Stretch"/>
          <Setter Property="ToolTip" Value="{x:Null}"/>
          <Setter Property="AutomationProperties.Name" Value="{Binding Card.AccessibleName}"/>
          <!-- Keyboard focus only (a mouse click focuses the item without this ring). -->
          <Setter Property="FocusVisualStyle">
            <Setter.Value>
              <Style TargetType="Control">
                <Setter Property="Template">
                  <Setter.Value>
                    <ControlTemplate>
                      <Ellipse Width="56" Height="56" Stroke="#E2ECFF" StrokeThickness="1.5" StrokeDashArray="2 1.5"
                               HorizontalAlignment="Center" VerticalAlignment="Center"/>
                    </ControlTemplate>
                  </Setter.Value>
                </Setter>
              </Style>
            </Setter.Value>
          </Setter>
          <Setter Property="Template">
            <Setter.Value>
              <ControlTemplate TargetType="ListBoxItem">
                <Grid Background="Transparent">
                  <!-- Selection: a filled halo and accent ring with a 4-DIP gap from the usage ring. -->
                  <Ellipse x:Name="Halo" Width="56" Height="56" Fill="Transparent" HorizontalAlignment="Center" VerticalAlignment="Center"/>
                  <Ellipse x:Name="Ring" Width="56" Height="56" Stroke="Transparent" StrokeThickness="1.5"
                           HorizontalAlignment="Center" VerticalAlignment="Center"/>
                  <!-- Selection indicator on the rail edge. -->
                  <Border x:Name="Indicator" Width="3" Height="28" CornerRadius="0,2,2,0" Background="#A3C1FF"
                          HorizontalAlignment="Left" VerticalAlignment="Center" Visibility="Hidden"/>
                  <ContentPresenter/>
                </Grid>
                <ControlTemplate.Triggers>
                  <Trigger Property="IsMouseOver" Value="True">
                    <Setter TargetName="Halo" Property="Fill" Value="#23272F"/>
                  </Trigger>
                  <Trigger Property="IsSelected" Value="True">
                    <Setter TargetName="Halo" Property="Fill" Value="#252C3B"/>
                    <Setter TargetName="Ring" Property="Stroke" Value="#A3C1FF"/>
                    <Setter TargetName="Indicator" Property="Visibility" Value="Visible"/>
                  </Trigger>
                  <Trigger Property="IsEnabled" Value="False">
                    <Setter Property="Opacity" Value="0.5"/>
                  </Trigger>
                </ControlTemplate.Triggers>
              </ControlTemplate>
            </Setter.Value>
          </Setter>
        </Style>
        """;
}
