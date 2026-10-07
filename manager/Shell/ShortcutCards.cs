using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Markup;

namespace Codex.ControlCenter.Shell;

// Task shortcut card, two lines. The title wraps to two lines; the second line
// says whether the task is working or waiting right now, which account opens
// it, the account's subagent models and when it was last used. The SSH host is
// in the card tooltip (and the search), not on every card.
// The leading ring is the owning account's remaining usage (outer: weekly,
// inner: a shorter window when reported) around its short label. The whole
// card opens the task, or reorders the list when dragged (ListOrdering takes
// the press on the "open" button); ⋯ holds move/rename/delete. Values come
// from Choice.Shortcut (ShortcutCardData); this file only lays them out.
internal static class ShortcutCards
{
    private static readonly DataTemplate Card = (DataTemplate)XamlReader.Parse(CardMarkup);

    internal static DataTemplate Create() => Card;

    // Buttons inside the template raise Click with their Tag ("open" or "menu").
    internal static void Attach(ListBox list, Func<Choice, string, FrameworkElement, Task> click) =>
        list.AddHandler(ButtonBase.ClickEvent, new RoutedEventHandler(async (_, e) =>
        {
            if (e.OriginalSource is not Button { DataContext: Choice choice, Tag: string action } button) return;
            e.Handled = true;
            await click(choice, action, button);
        }));

    // Working dots animate only while the list's Tag says so. The window turns
    // it off while the list is not on screen or the window is minimized.
    internal static void SetPulse(ListBox list, bool on) => list.Tag = on ? "pulse" : null;

    private const string CardMarkup = """
        <DataTemplate xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
                      xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">
          <DataTemplate.Resources>
            <Style x:Key="Chip" TargetType="Border">
              <Setter Property="CornerRadius" Value="4"/>
              <Setter Property="Padding" Value="5,1,5,2"/>
              <Setter Property="Margin" Value="0,5,5,0"/>
              <Setter Property="VerticalAlignment" Value="Center"/>
            </Style>
          </DataTemplate.Resources>
          <!-- ⋯ sits over the title's top-right corner, so the second line keeps the card's full width. -->
          <Grid>
            <!-- Card buttons never take keyboard focus: a click must not leave a focus
                 rail on one card while another is the selected one. Enter opens the
                 selected card; the menu key opens its menu. -->
            <Button Tag="open" Cursor="Hand" HorizontalContentAlignment="Stretch" Focusable="False"
                    AutomationProperties.Name="{Binding Shortcut.Title}" ToolTip="{Binding Shortcut.Detail}">
              <Button.Template>
                <ControlTemplate TargetType="Button">
                  <Border Background="Transparent"><ContentPresenter/></Border>
                </ControlTemplate>
              </Button.Template>
              <Grid>
                <Grid.ColumnDefinitions>
                  <ColumnDefinition Width="Auto"/>
                  <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>
                <!-- 32-DIP usage ring: track, remaining arc, optional inner window, account. -->
                <Grid Width="32" Height="32" VerticalAlignment="Top" Margin="0,1,0,0">
                  <Viewbox Stretch="Uniform">
                    <Grid Width="40" Height="40">
                      <!-- Ellipse strokes sit inside their bounds: size = 2 x arc radius + stroke. -->
                      <Ellipse Width="38.5" Height="38.5" Stroke="#2A2E37" StrokeThickness="3.5"/>
                      <Path Data="{Binding Shortcut.OuterArc}" Stroke="{Binding Shortcut.OuterBrush}"
                            StrokeThickness="3.5" StrokeStartLineCap="Round" StrokeEndLineCap="Round"/>
                      <Ellipse Width="27.5" Height="27.5" Stroke="#2A2E37" StrokeThickness="2.5"
                               Visibility="{Binding Shortcut.InnerVisibility}"/>
                      <Path Data="{Binding Shortcut.InnerArc}" Stroke="{Binding Shortcut.InnerBrush}"
                            StrokeThickness="2.5" StrokeStartLineCap="Round" StrokeEndLineCap="Round"
                            Visibility="{Binding Shortcut.InnerVisibility}"/>
                    </Grid>
                  </Viewbox>
                  <TextBlock Text="{Binding Shortcut.AccountShort}" FontSize="11" FontWeight="Bold"
                             Foreground="#E9ECF2" HorizontalAlignment="Center" VerticalAlignment="Center"
                             TextWrapping="NoWrap"/>
                </Grid>
                <StackPanel Grid.Column="1" Margin="8,0,0,0">
                  <TextBlock Text="{Binding Shortcut.Title}" FontSize="14" FontWeight="SemiBold" Foreground="#E9ECF2"
                             TextWrapping="Wrap" TextTrimming="CharacterEllipsis" LineHeight="19"
                             LineStackingStrategy="BlockLineHeight" MaxHeight="38" Margin="0,0,24,0"/>
                  <WrapPanel Margin="0,0,0,0">
                    <Border Style="{StaticResource Chip}" CornerRadius="10" Padding="6,1,7,2" Background="{Binding Shortcut.PillBrush}">
                      <StackPanel Orientation="Horizontal">
                        <Ellipse x:Name="Dot" Width="7" Height="7" Margin="0,0,5,0" VerticalAlignment="Center"
                                 Fill="{Binding Shortcut.StateBrush}"/>
                        <TextBlock Text="{Binding Shortcut.StateText}" FontSize="12" Foreground="{Binding Shortcut.StateBrush}"
                                   TextWrapping="NoWrap" VerticalAlignment="Center"/>
                      </StackPanel>
                    </Border>
                    <!-- The account that opens this task: number and short name. -->
                    <Border Style="{StaticResource Chip}" Background="{Binding Shortcut.BadgeBrush}" ToolTip="{Binding Shortcut.Account}">
                      <TextBlock Text="{Binding Shortcut.ProfileName}" FontSize="12" Foreground="{Binding Shortcut.BadgeTextBrush}" TextWrapping="NoWrap"/>
                    </Border>
                    <!-- Subagent models of the account, or the model of an API profile. -->
                    <Border Style="{StaticResource Chip}" Background="#2C3044" Visibility="{Binding Shortcut.AgentVisibility}">
                      <TextBlock Text="{Binding Shortcut.Agent}" FontSize="12" Foreground="#C6CFFF" TextWrapping="NoWrap"/>
                    </Border>
                    <TextBlock Text="{Binding Shortcut.When}" FontSize="12" Foreground="#8E9BB2" Margin="2,6,0,0"
                               VerticalAlignment="Center" TextWrapping="NoWrap" Visibility="{Binding Shortcut.WhenVisibility}"/>
                  </WrapPanel>
                </StackPanel>
              </Grid>
            </Button>
            <Button Tag="menu" Content="&#xE712;" Width="28" Height="28" Margin="0,-5,-6,0" Focusable="False"
                    HorizontalAlignment="Right" VerticalAlignment="Top" FontSize="14" FontFamily="Segoe Fluent Icons, Segoe MDL2 Assets" Cursor="Hand"
                    ToolTip="계정 이동 · 별칭 변경 · 링크 삭제" AutomationProperties.Name="바로가기 관리">
              <Button.Template>
                <ControlTemplate TargetType="Button">
                  <Border x:Name="Chrome" Background="Transparent" CornerRadius="6">
                    <TextBlock Text="{TemplateBinding Content}" Foreground="#9FA7B7" FontSize="{TemplateBinding FontSize}"
                               FontFamily="{TemplateBinding FontFamily}" HorizontalAlignment="Center" VerticalAlignment="Center"/>
                  </Border>
                  <ControlTemplate.Triggers>
                    <Trigger Property="IsMouseOver" Value="True">
                      <Setter TargetName="Chrome" Property="Background" Value="#353C4A"/>
                    </Trigger>
                  </ControlTemplate.Triggers>
                </ControlTemplate>
              </Button.Template>
            </Button>
          </Grid>
          <DataTemplate.Triggers>
            <!-- A working task's dot breathes; waiting and idle dots stay still. It
                 breathes at a few frames a second and only while the list is seen
                 (SetPulse): a forever animation otherwise ticks every card's clock
                 at full frame rate in a closed overlay or a minimized window. -->
            <MultiDataTrigger>
              <MultiDataTrigger.Conditions>
                <Condition Binding="{Binding Shortcut.State}" Value="working"/>
                <Condition Binding="{Binding Tag, RelativeSource={RelativeSource AncestorType=ListBox}}" Value="pulse"/>
              </MultiDataTrigger.Conditions>
              <MultiDataTrigger.EnterActions>
                <BeginStoryboard x:Name="Pulse">
                  <Storyboard Timeline.DesiredFrameRate="8">
                    <DoubleAnimation Storyboard.TargetName="Dot" Storyboard.TargetProperty="Opacity"
                                     From="1" To="0.25" Duration="0:0:0.9" AutoReverse="True" RepeatBehavior="Forever"/>
                  </Storyboard>
                </BeginStoryboard>
              </MultiDataTrigger.EnterActions>
              <MultiDataTrigger.ExitActions>
                <StopStoryboard BeginStoryboardName="Pulse"/>
              </MultiDataTrigger.ExitActions>
            </MultiDataTrigger>
          </DataTemplate.Triggers>
        </DataTemplate>
        """;
}
