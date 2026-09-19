using System.Windows;
using System.Windows.Controls;
using System.Windows.Markup;

namespace Codex.ControlCenter.Shell;

// Concept A profile card. Identity and runtime state share the first line,
// quota keeps its own line with a 3-DIP meter, and reset/redeem/freshness wrap
// only between whole values, so a count or a percent is never split in half.
// Every value arrives through Choice.Card (ProfileCardData); this file never
// reads JSON, never measures text and never decides what a string means.
//
// Both templates are parsed once and shared; markup keeps column definitions
// and bindings declarative instead of rebuilding them on every render.
internal static class ProfileCards
{
    private static readonly DataTemplate Card = (DataTemplate)XamlReader.Parse(CardMarkup);
    private static readonly Style Row = RowStyle();

    internal static DataTemplate Create() => Card;

    // Selection, hover and focus live here, so the shared list item style used
    // by shortcuts and dialogs stays untouched. It is based on that style only
    // to inherit its tooltip and text defaults.
    internal static Style ContainerStyle() => Row;

    private static Style RowStyle()
    {
        var style = (Style)XamlReader.Parse(RowMarkup);
        if (Application.Current?.TryFindResource(typeof(ListBoxItem)) is Style inherited) style.BasedOn = inherited;
        return style;
    }

    private const string CardMarkup = """
        <DataTemplate xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
                      xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml">
          <Grid x:Name="ProfileCard">
            <Grid.ColumnDefinitions>
              <ColumnDefinition Width="16"/>
              <ColumnDefinition Width="*"/>
            </Grid.ColumnDefinitions>
            <!-- Full-height 16-DIP lane. ProfileOrdering finds it by Tag, so a press anywhere in
                 the lane starts a drag instead of selecting the row. -->
            <Border Tag="ProfileDragGrip" Width="16" HorizontalAlignment="Left" VerticalAlignment="Stretch"
                    CornerRadius="3" Cursor="SizeNS"
                    ToolTip="드래그하여 순서 변경 · 오른쪽 클릭으로 위/아래 이동">
              <Border.Style>
                <Style TargetType="Border">
                  <Setter Property="Background" Value="Transparent"/>
                  <Style.Triggers>
                    <Trigger Property="IsMouseOver" Value="True">
                      <Setter Property="Background" Value="#30363F"/>
                    </Trigger>
                  </Style.Triggers>
                </Style>
              </Border.Style>
              <TextBlock Text="⠿" FontSize="14" Foreground="#7F8899" HorizontalAlignment="Center"
                         VerticalAlignment="Center" TextWrapping="NoWrap" IsHitTestVisible="False"/>
            </Border>
            <StackPanel Grid.Column="1" Margin="6,0,0,0">
              <Grid>
                <Grid.ColumnDefinitions>
                  <ColumnDefinition Width="*"/>
                  <ColumnDefinition Width="Auto"/>
                </Grid.ColumnDefinitions>
                <TextBlock Text="{Binding Card.Name}" FontSize="15" FontWeight="SemiBold" Foreground="#E9ECF2"
                           TextWrapping="NoWrap" TextTrimming="CharacterEllipsis" VerticalAlignment="Center"
                           ToolTip="{Binding Card.Name}"/>
                <StackPanel Grid.Column="1" Orientation="Horizontal" Margin="10,0,0,0" VerticalAlignment="Center">
                  <Ellipse Width="7" Height="7" Margin="0,0,6,0" VerticalAlignment="Center"
                           Fill="{Binding Card.StatusBrush}"/>
                  <TextBlock Text="{Binding Card.Status}" FontSize="12" Foreground="{Binding Card.StatusBrush}"
                             TextWrapping="NoWrap" TextTrimming="CharacterEllipsis" MaxWidth="110"
                             VerticalAlignment="Center"/>
                </StackPanel>
              </Grid>
              <Grid Margin="0,5,0,0" Visibility="{Binding Card.NativeVisibility}">
                <Grid.ColumnDefinitions>
                  <ColumnDefinition Width="Auto"/>
                  <ColumnDefinition Width="Auto"/>
                  <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>
                <TextBlock Text="{Binding Card.QuotaLabel}" FontSize="12" Foreground="#9FA7B7"
                           TextWrapping="NoWrap" VerticalAlignment="Center"/>
                <TextBlock Grid.Column="1" Text="{Binding Card.QuotaText}" FontSize="12" FontWeight="SemiBold"
                           Foreground="{Binding Card.QuotaBrush}" TextWrapping="NoWrap" Margin="6,0,0,0"
                           VerticalAlignment="Center"/>
                <!-- 3-DIP meter. Both part names are required: ProgressBar sizes PART_Indicator
                     against PART_Track, and the track stays visible when the bar is empty. -->
                <ProgressBar Grid.Column="2" Height="3" MinWidth="24" Margin="10,0,0,0" VerticalAlignment="Center"
                             IsHitTestVisible="False" Background="#2A2E37" Foreground="{Binding Card.QuotaBrush}"
                             BorderThickness="0" Minimum="0" Maximum="100" Value="{Binding Card.Remaining}"
                             Visibility="{Binding Card.QuotaVisibility}">
                  <ProgressBar.Template>
                    <ControlTemplate TargetType="ProgressBar">
                      <Grid UseLayoutRounding="True">
                        <Border x:Name="PART_Track" Background="{TemplateBinding Background}" CornerRadius="1.5"/>
                        <Border x:Name="PART_Indicator" Background="{TemplateBinding Foreground}"
                                CornerRadius="1.5" HorizontalAlignment="Left"/>
                      </Grid>
                    </ControlTemplate>
                  </ProgressBar.Template>
                </ProgressBar>
              </Grid>
              <!-- A WrapPanel breaks between values only, so the reset stamp and the redeem count
                   each stay whole and simply move to a second line when the width is short. -->
              <WrapPanel Margin="0,4,0,0" Visibility="{Binding Card.NativeVisibility}">
                <TextBlock Text="{Binding Card.ResetText}" FontSize="11" Foreground="#8E9BB2"
                           TextWrapping="NoWrap" Margin="0,0,12,0"/>
                <TextBlock Text="{Binding Card.RedeemText}" FontSize="11" Foreground="#8E9BB2"
                           TextWrapping="NoWrap" Margin="0,0,12,0"
                           ToolTip="사용량 한도를 초기화할 수 있는 남은 리딤 횟수입니다. 확인되지 않은 값은 0회로 표시하지 않습니다."/>
                <TextBlock Text="{Binding Card.Freshness}" FontSize="11" Foreground="#8E9BB2"
                           TextWrapping="NoWrap" Margin="0,0,12,0"
                           Visibility="{Binding Card.FreshnessVisibility}"/>
              </WrapPanel>
              <!-- Star column so a long model name trims instead of overflowing the row. -->
              <Grid Margin="0,5,0,0" Visibility="{Binding Card.ExternalVisibility}">
                <Grid.ColumnDefinitions>
                  <ColumnDefinition Width="Auto"/>
                  <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>
                <Border Background="#33291F" CornerRadius="3" Padding="5,1,5,1" Margin="0,0,7,0"
                        VerticalAlignment="Center">
                  <TextBlock Text="API" FontSize="10" FontWeight="SemiBold" Foreground="#E5B773"
                             TextWrapping="NoWrap"/>
                </Border>
                <TextBlock Grid.Column="1" Text="{Binding Card.Model}" FontSize="12" Foreground="#9FA7B7"
                           TextWrapping="NoWrap" TextTrimming="CharacterEllipsis" VerticalAlignment="Center"
                           ToolTip="{Binding Card.Model}"/>
              </Grid>
              <TextBlock Text="{Binding Card.Notice}" FontSize="11" Foreground="#E5B773" TextWrapping="Wrap"
                         Margin="0,5,0,0" Visibility="{Binding Card.NoticeVisibility}"/>
              <Border Visibility="{Binding AgentBadgeVisibility}" ToolTip="{Binding AgentHint}"
                      Background="#2C3044" CornerRadius="4" Padding="6,2,6,2" Margin="0,6,0,1"
                      HorizontalAlignment="Left">
                <TextBlock Text="{Binding AgentBadge}" FontSize="11" Foreground="#C6CFFF" TextWrapping="Wrap"/>
              </Border>
            </StackPanel>
          </Grid>
        </DataTemplate>
        """;

    private const string RowMarkup = """
        <Style xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
               xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" TargetType="ListBoxItem">
          <Setter Property="Padding" Value="10,9,10,9"/>
          <Setter Property="Margin" Value="0,3,0,3"/>
          <Setter Property="HorizontalContentAlignment" Value="Stretch"/>
          <Setter Property="ToolTip" Value="{Binding Hint}"/>
          <Setter Property="Template">
            <Setter.Value>
              <ControlTemplate TargetType="ListBoxItem">
                <!-- The 2-DIP rail is always reserved, so text never shifts when a row is selected;
                     only its colour changes. -->
                <Border x:Name="Chrome" Background="Transparent" BorderBrush="Transparent"
                        BorderThickness="2,0,0,0" CornerRadius="6" UseLayoutRounding="True"
                        Padding="{TemplateBinding Padding}">
                  <ContentPresenter/>
                </Border>
                <ControlTemplate.Triggers>
                  <Trigger Property="IsMouseOver" Value="True">
                    <Setter TargetName="Chrome" Property="Background" Value="#262B34"/>
                  </Trigger>
                  <Trigger Property="IsSelected" Value="True">
                    <Setter TargetName="Chrome" Property="Background" Value="#2E3443"/>
                    <Setter TargetName="Chrome" Property="BorderBrush" Value="#A3C1FF"/>
                  </Trigger>
                  <!-- Declared after selection so keyboard focus stays visible on top of it. -->
                  <Trigger Property="IsKeyboardFocusWithin" Value="True">
                    <Setter TargetName="Chrome" Property="BorderBrush" Value="#CFE0FF"/>
                  </Trigger>
                  <Trigger Property="IsEnabled" Value="False">
                    <Setter TargetName="Chrome" Property="Opacity" Value="0.5"/>
                  </Trigger>
                </ControlTemplate.Triggers>
              </ControlTemplate>
            </Setter.Value>
          </Setter>
        </Style>
        """;
}
