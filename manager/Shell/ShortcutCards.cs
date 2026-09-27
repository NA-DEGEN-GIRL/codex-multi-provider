using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Markup;

namespace Codex.ControlCenter.Shell;

// Task shortcut card. The ring is the owning account's remaining usage (outer:
// weekly, inner: a shorter window when reported) with the account name in its
// centre; the pill says whether the task is working or waiting right now. The
// whole card opens the task; ⋯ holds move/rename/delete. Values come from
// Choice.Shortcut (ShortcutCardData); this file only lays them out.
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

    private const string CardMarkup = """
        <DataTemplate xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
                      xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">
          <Grid>
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="*"/>
              <ColumnDefinition Width="Auto"/>
            </Grid.ColumnDefinitions>
            <Button Tag="open" Cursor="Hand" HorizontalContentAlignment="Stretch"
                    AutomationProperties.Name="{Binding Shortcut.Title}" ToolTip="{Binding Shortcut.Detail}">
              <Button.Template>
                <ControlTemplate TargetType="Button">
                  <Border Background="Transparent"><ContentPresenter/></Border>
                </ControlTemplate>
              </Button.Template>
              <Grid>
                <Grid.ColumnDefinitions>
                  <ColumnDefinition Width="40"/>
                  <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>
                <!-- 40-DIP usage ring: track, remaining arc, optional inner window, account. -->
                <Grid Width="40" Height="40" VerticalAlignment="Center">
                  <!-- Ellipse strokes sit inside their bounds: size = 2 x arc radius + stroke. -->
                  <Ellipse Width="38.5" Height="38.5" Stroke="#2A2E37" StrokeThickness="3.5"/>
                  <Path Data="{Binding Shortcut.OuterArc}" Stroke="{Binding Shortcut.OuterBrush}"
                        StrokeThickness="3.5" StrokeStartLineCap="Round" StrokeEndLineCap="Round"/>
                  <Ellipse Width="27.5" Height="27.5" Stroke="#2A2E37" StrokeThickness="2.5"
                           Visibility="{Binding Shortcut.InnerVisibility}"/>
                  <Path Data="{Binding Shortcut.InnerArc}" Stroke="{Binding Shortcut.InnerBrush}"
                        StrokeThickness="2.5" StrokeStartLineCap="Round" StrokeEndLineCap="Round"
                        Visibility="{Binding Shortcut.InnerVisibility}"/>
                  <TextBlock Text="{Binding Shortcut.AccountShort}" FontSize="12" FontWeight="Bold"
                             Foreground="#E9ECF2" HorizontalAlignment="Center" VerticalAlignment="Center"
                             TextWrapping="NoWrap"/>
                </Grid>
                <StackPanel Grid.Column="1" Margin="10,0,0,0" VerticalAlignment="Center">
                  <TextBlock Text="{Binding Shortcut.Title}" FontSize="14" FontWeight="SemiBold" Foreground="#E9ECF2"
                             TextWrapping="NoWrap" TextTrimming="CharacterEllipsis"/>
                  <Grid Margin="0,5,0,0">
                    <Grid.ColumnDefinitions>
                      <ColumnDefinition Width="Auto"/>
                      <ColumnDefinition Width="Auto"/>
                      <ColumnDefinition Width="*"/>
                    </Grid.ColumnDefinitions>
                    <Border CornerRadius="9" Padding="7,2,8,2" Background="{Binding Shortcut.PillBrush}"
                            VerticalAlignment="Center">
                      <StackPanel Orientation="Horizontal">
                        <Ellipse x:Name="Dot" Width="7" Height="7" Margin="0,0,5,0" VerticalAlignment="Center"
                                 Fill="{Binding Shortcut.StateBrush}"/>
                        <TextBlock Text="{Binding Shortcut.StateText}" FontSize="11.5" Foreground="{Binding Shortcut.StateBrush}"
                                   TextWrapping="NoWrap" VerticalAlignment="Center"/>
                      </StackPanel>
                    </Border>
                    <!-- Subagent models of the account, or the model of an API profile. -->
                    <Border Grid.Column="1" CornerRadius="4" Padding="6,2,6,2" Margin="6,0,0,0" Background="#2C3044"
                            VerticalAlignment="Center" Visibility="{Binding Shortcut.AgentVisibility}">
                      <TextBlock Text="{Binding Shortcut.Agent}" FontSize="11" Foreground="#C6CFFF" TextWrapping="NoWrap"/>
                    </Border>
                    <TextBlock Grid.Column="2" Text="{Binding Shortcut.Host}" FontSize="11.5" Foreground="#8E9BB2"
                               Margin="8,0,0,0" VerticalAlignment="Center" TextWrapping="NoWrap"
                               TextTrimming="CharacterEllipsis"/>
                  </Grid>
                </StackPanel>
              </Grid>
            </Button>
            <Button Grid.Column="1" Tag="menu" Content="⋯" Width="28" Height="28" Margin="6,0,0,0"
                    VerticalAlignment="Center" FontSize="15" Cursor="Hand" ToolTip="계정 이동 · 별칭 변경 · 링크 삭제"
                    AutomationProperties.Name="바로가기 관리">
              <Button.Template>
                <ControlTemplate TargetType="Button">
                  <Border x:Name="Chrome" Background="Transparent" CornerRadius="6">
                    <TextBlock Text="{TemplateBinding Content}" Foreground="#9FA7B7" FontSize="{TemplateBinding FontSize}"
                               HorizontalAlignment="Center" VerticalAlignment="Center"/>
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
            <!-- A working task's dot breathes; waiting and idle dots stay still. -->
            <DataTrigger Binding="{Binding Shortcut.State}" Value="working">
              <DataTrigger.EnterActions>
                <BeginStoryboard x:Name="Pulse">
                  <Storyboard>
                    <DoubleAnimation Storyboard.TargetName="Dot" Storyboard.TargetProperty="Opacity"
                                     From="1" To="0.25" Duration="0:0:0.9" AutoReverse="True" RepeatBehavior="Forever"/>
                  </Storyboard>
                </BeginStoryboard>
              </DataTrigger.EnterActions>
              <DataTrigger.ExitActions>
                <StopStoryboard BeginStoryboardName="Pulse"/>
              </DataTrigger.ExitActions>
            </DataTrigger>
          </DataTemplate.Triggers>
        </DataTemplate>
        """;
}
