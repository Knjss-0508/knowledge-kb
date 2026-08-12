[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot ".env"),
    [string]$RuntimeRoot = "D:\knowledge-kb-training-runtime",
    [switch]$ValidateOnly
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName PresentationCore
Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName WindowsBase

$RunnerDirectory = $PSScriptRoot
$HostRunnerPath = Join-Path $RunnerDirectory "host-runner.ps1"
$PwshPath = (Get-Command "pwsh.exe" -ErrorAction Stop).Source
$NvidiaSmiCommand = Get-Command "nvidia-smi.exe" -ErrorAction SilentlyContinue
$NvidiaSmiPath = if ($NvidiaSmiCommand) {
    $NvidiaSmiCommand.Source
} else {
    ""
}
$script:ActionProcess = $null
$script:ActionName = ""
$script:ActionStdout = ""
$script:ActionStderr = ""
$script:ActionStartedAt = $null
$script:RunnerProcess = $null
$script:RunnerStdout = ""
$script:RunnerStderr = ""
$script:RunnerStartedAt = $null
$script:ServerProbeState = "unknown"
$script:FooterMessage = "控制台已就绪"
$script:OperationFeedbackState = "idle"
$script:OperationFeedbackMessage = "等待操作。环境检查首次加载 ms-swift 时可能需要 2–4 分钟。"
$script:RefreshErrorMessage = ""

[xml]$Xaml = @'
<Window
    xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    Title="知识库模型训练控制台"
    Width="1120"
    Height="760"
    MinWidth="920"
    MinHeight="640"
    WindowStartupLocation="CenterScreen"
    Background="#F4F7F8"
    FontFamily="Segoe UI, Microsoft YaHei UI"
    TextOptions.TextFormattingMode="Display">
  <Window.Resources>
    <SolidColorBrush x:Key="PrimaryBrush" Color="#087F6B"/>
    <SolidColorBrush x:Key="PrimaryDarkBrush" Color="#123B3A"/>
    <SolidColorBrush x:Key="BorderBrush" Color="#DDE5E8"/>
    <Style x:Key="PrimaryButton" TargetType="Button">
      <Setter Property="MinHeight" Value="40"/>
      <Setter Property="Padding" Value="16,8"/>
      <Setter Property="Margin" Value="0,0,8,8"/>
      <Setter Property="Background" Value="{StaticResource PrimaryBrush}"/>
      <Setter Property="Foreground" Value="White"/>
      <Setter Property="BorderThickness" Value="0"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Cursor" Value="Hand"/>
    </Style>
    <Style x:Key="SecondaryButton" TargetType="Button">
      <Setter Property="MinHeight" Value="40"/>
      <Setter Property="Padding" Value="16,8"/>
      <Setter Property="Margin" Value="0,0,8,8"/>
      <Setter Property="Background" Value="White"/>
      <Setter Property="Foreground" Value="#344054"/>
      <Setter Property="BorderBrush" Value="{StaticResource BorderBrush}"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="Cursor" Value="Hand"/>
    </Style>
    <Style x:Key="DangerButton" TargetType="Button" BasedOn="{StaticResource SecondaryButton}">
      <Setter Property="Foreground" Value="#B42318"/>
      <Setter Property="BorderBrush" Value="#F4B7B2"/>
      <Setter Property="Background" Value="#FFF7F6"/>
    </Style>
    <Style TargetType="TextBox">
      <Setter Property="MinHeight" Value="36"/>
      <Setter Property="Padding" Value="9,6"/>
      <Setter Property="BorderBrush" Value="{StaticResource BorderBrush}"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Background" Value="White"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
    </Style>
    <Style TargetType="PasswordBox">
      <Setter Property="MinHeight" Value="36"/>
      <Setter Property="Padding" Value="9,6"/>
      <Setter Property="BorderBrush" Value="{StaticResource BorderBrush}"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Background" Value="White"/>
      <Setter Property="VerticalContentAlignment" Value="Center"/>
    </Style>
  </Window.Resources>

  <Grid>
    <Grid.RowDefinitions>
      <RowDefinition Height="84"/>
      <RowDefinition Height="116"/>
      <RowDefinition Height="*"/>
      <RowDefinition Height="42"/>
    </Grid.RowDefinitions>

    <Border Grid.Row="0" Background="{StaticResource PrimaryDarkBrush}">
      <Grid Margin="24,0">
        <Grid.ColumnDefinitions>
          <ColumnDefinition Width="*"/>
          <ColumnDefinition Width="Auto"/>
        </Grid.ColumnDefinitions>
        <StackPanel VerticalAlignment="Center">
          <TextBlock Text="知识库模型训练控制台" Foreground="White" FontSize="23" FontWeight="Bold"/>
          <TextBlock Text="本机只承担 GPU 训练，不启动知识库容器或业务服务" Foreground="#B9D8D5" FontSize="12" Margin="0,6,0,0"/>
        </StackPanel>
        <Border Grid.Column="1" VerticalAlignment="Center" Background="#1D504D" BorderBrush="#39716D" BorderThickness="1" CornerRadius="6" Padding="12,8">
          <StackPanel>
            <TextBlock Text="发布权限" Foreground="#B9D8D5" FontSize="11"/>
            <TextBlock Text="上传与替换均锁定" Foreground="White" FontSize="13" FontWeight="SemiBold" Margin="0,3,0,0"/>
          </StackPanel>
        </Border>
      </Grid>
    </Border>

    <UniformGrid Grid.Row="1" Columns="4" Margin="20,14,20,8">
      <Border Margin="0,0,10,0" Padding="14,12" Background="White" BorderBrush="{StaticResource BorderBrush}" BorderThickness="1" CornerRadius="8">
        <Grid>
          <Grid.ColumnDefinitions><ColumnDefinition Width="Auto"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
          <Ellipse x:Name="ConfigDot" Width="11" Height="11" Fill="#98A2B3" VerticalAlignment="Top" Margin="0,4,10,0"/>
          <StackPanel Grid.Column="1"><TextBlock Text="1. 连接配置" FontWeight="SemiBold" Foreground="#344054"/><TextBlock x:Name="ConfigStatus" Text="待检查" Margin="0,6,0,0" Foreground="#667085" TextWrapping="Wrap"/></StackPanel>
        </Grid>
      </Border>
      <Border Margin="0,0,10,0" Padding="14,12" Background="White" BorderBrush="{StaticResource BorderBrush}" BorderThickness="1" CornerRadius="8">
        <Grid>
          <Grid.ColumnDefinitions><ColumnDefinition Width="Auto"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
          <Ellipse x:Name="EnvironmentDot" Width="11" Height="11" Fill="#98A2B3" VerticalAlignment="Top" Margin="0,4,10,0"/>
          <StackPanel Grid.Column="1"><TextBlock Text="2. 训练环境" FontWeight="SemiBold" Foreground="#344054"/><TextBlock x:Name="EnvironmentStatus" Text="待检查" Margin="0,6,0,0" Foreground="#667085" TextWrapping="Wrap"/></StackPanel>
        </Grid>
      </Border>
      <Border Margin="0,0,10,0" Padding="14,12" Background="White" BorderBrush="{StaticResource BorderBrush}" BorderThickness="1" CornerRadius="8">
        <Grid>
          <Grid.ColumnDefinitions><ColumnDefinition Width="Auto"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
          <Ellipse x:Name="RunnerDot" Width="11" Height="11" Fill="#98A2B3" VerticalAlignment="Top" Margin="0,4,10,0"/>
          <StackPanel Grid.Column="1"><TextBlock Text="3. 本机 Runner" FontWeight="SemiBold" Foreground="#344054"/><TextBlock x:Name="RunnerStatus" Text="未运行" Margin="0,6,0,0" Foreground="#667085" TextWrapping="Wrap"/></StackPanel>
        </Grid>
      </Border>
      <Border Padding="14,12" Background="White" BorderBrush="{StaticResource BorderBrush}" BorderThickness="1" CornerRadius="8">
        <Grid>
          <Grid.ColumnDefinitions><ColumnDefinition Width="Auto"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
          <Ellipse x:Name="ServerDot" Width="11" Height="11" Fill="#98A2B3" VerticalAlignment="Top" Margin="0,4,10,0"/>
          <StackPanel Grid.Column="1"><TextBlock Text="4. 服务器连接" FontWeight="SemiBold" Foreground="#344054"/><TextBlock x:Name="ServerStatus" Text="尚未检测" Margin="0,6,0,0" Foreground="#667085" TextWrapping="Wrap"/></StackPanel>
        </Grid>
      </Border>
    </UniformGrid>

    <Grid Grid.Row="2" Margin="20,8,20,16">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="390"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>

      <Grid Grid.Column="0">
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="*"/>
        </Grid.RowDefinitions>

        <Border Grid.Row="0" Padding="16" Background="White" BorderBrush="{StaticResource BorderBrush}" BorderThickness="1" CornerRadius="8">
          <StackPanel>
            <TextBlock Text="快速操作" FontSize="16" FontWeight="Bold" Foreground="#1D2939"/>
            <TextBlock Text="从工作台创建 LoRA 任务，粘贴任务 URL 和密钥后即可在本机开始。" Margin="0,5,0,12" Foreground="#667085" FontSize="11" TextWrapping="Wrap"/>
            <WrapPanel>
              <Button x:Name="QuickStartButton" Content="保存并开始此任务" Style="{StaticResource PrimaryButton}"/>
              <Button x:Name="QuickStopButton" Content="停止当前训练" Style="{StaticResource DangerButton}"/>
              <Button x:Name="QuickCheckButton" Content="环境检查" Style="{StaticResource SecondaryButton}"/>
              <Button x:Name="QuickProbeButton" Content="检测任务连接" Style="{StaticResource SecondaryButton}"/>
            </WrapPanel>
            <TextBlock x:Name="QuickFeedbackText" Text="等待操作。环境检查首次加载 ms-swift 时可能需要 2–4 分钟。" Margin="0,2,0,0" Foreground="#667085" FontSize="11" TextWrapping="Wrap"/>
          </StackPanel>
        </Border>

        <ScrollViewer Grid.Row="1" Margin="0,14,0,0" VerticalScrollBarVisibility="Auto">
          <StackPanel>
          <Border Padding="18" Background="White" BorderBrush="{StaticResource BorderBrush}" BorderThickness="1" CornerRadius="8">
            <StackPanel>
              <TextBlock Text="LoRA 任务接入" FontSize="16" FontWeight="Bold" Foreground="#1D2939"/>
              <TextBlock Text="任务 URL 和密钥由工作台在创建任务时生成。密钥只保存到本机 training-runner\.env，不显示在日志中。" Margin="0,5,0,14" Foreground="#667085" FontSize="11" TextWrapping="Wrap"/>

              <TextBlock Text="任务接入 URL" FontWeight="SemiBold" Foreground="#475467"/>
              <TextBox x:Name="ServerUrlInput" Margin="0,5,0,11" ToolTip="粘贴工作台生成的完整 HTTP(S) 任务 URL"/>

              <TextBlock Text="Runner 标识" FontWeight="SemiBold" Foreground="#475467"/>
              <TextBox x:Name="RunnerIdInput" Margin="0,5,0,11"/>

              <TextBlock Text="显示名称" FontWeight="SemiBold" Foreground="#475467"/>
              <TextBox x:Name="RunnerNameInput" Margin="0,5,0,11"/>

              <TextBlock Text="任务密钥" FontWeight="SemiBold" Foreground="#475467"/>
              <PasswordBox x:Name="TokenInput" Margin="0,5,0,11"/>

              <TextBlock Text="独立运行目录" FontWeight="SemiBold" Foreground="#475467"/>
              <TextBox x:Name="RuntimeRootInput" Margin="0,5,0,14"/>

              <Button x:Name="SaveConfigButton" Content="仅保存任务信息" Style="{StaticResource SecondaryButton}" HorizontalAlignment="Left"/>
            </StackPanel>
          </Border>

          <Border Margin="0,14,0,0" Padding="18" Background="White" BorderBrush="{StaticResource BorderBrush}" BorderThickness="1" CornerRadius="8">
            <StackPanel>
              <TextBlock Text="训练操作" FontSize="16" FontWeight="Bold" Foreground="#1D2939"/>
              <TextBlock Text="安装、检查和 1 Step 实训不会连接服务器；检测任务连接和开始任务才会使用任务密钥。" Margin="0,5,0,14" Foreground="#667085" FontSize="11" TextWrapping="Wrap"/>
              <WrapPanel>
                <Button x:Name="InstallButton" Content="安装环境" Style="{StaticResource SecondaryButton}"/>
                <Button x:Name="CheckButton" Content="环境检查" Style="{StaticResource SecondaryButton}"/>
                <Button x:Name="SmokeButton" Content="1 Step 实训" Style="{StaticResource SecondaryButton}"/>
                <Button x:Name="ProbeButton" Content="检测任务连接" Style="{StaticResource SecondaryButton}"/>
                <Button x:Name="StartButton" Content="保存并开始此任务" Style="{StaticResource PrimaryButton}"/>
                <Button x:Name="StopButton" Content="停止当前训练" Style="{StaticResource DangerButton}"/>
                <Button x:Name="OpenArtifactsButton" Content="打开训练产物" Style="{StaticResource SecondaryButton}"/>
              </WrapPanel>
              <Border Margin="0,4,0,0" Padding="10,8" Background="#F7F9FC" BorderBrush="#E4E7EC" BorderThickness="1" CornerRadius="5">
                <TextBlock x:Name="OperationFeedbackText" Text="等待操作。环境检查首次加载 ms-swift 时可能需要 2–4 分钟。" Foreground="#667085" FontSize="11" TextWrapping="Wrap"/>
              </Border>
            </StackPanel>
          </Border>

          <Border Margin="0,14,0,0" Padding="16" Background="#FFF9ED" BorderBrush="#F2D18A" BorderThickness="1" CornerRadius="8">
            <StackPanel>
              <TextBlock Text="高风险发布操作已锁定" FontWeight="Bold" Foreground="#7A5200"/>
              <TextBlock Text="模型上传、替换生产模型、全量向量重建不在本控制台执行。每一步都必须取得你的单独授权。" Margin="0,6,0,0" Foreground="#8A6300" FontSize="12" TextWrapping="Wrap" LineHeight="18"/>
            </StackPanel>
          </Border>
        </StackPanel>
        </ScrollViewer>
      </Grid>

      <Grid Grid.Column="1" Margin="16,0,0,0">
        <Grid.RowDefinitions>
          <RowDefinition Height="Auto"/>
          <RowDefinition Height="*"/>
        </Grid.RowDefinitions>

        <Border Grid.Row="0" Padding="18" Background="White" BorderBrush="{StaticResource BorderBrush}" BorderThickness="1" CornerRadius="8">
          <Grid>
            <Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/></Grid.RowDefinitions>
            <Grid.ColumnDefinitions><ColumnDefinition Width="*"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
            <StackPanel Grid.Row="0" Grid.Column="0" Margin="0,0,18,0">
              <TextBlock Text="GPU 状态" FontWeight="SemiBold" Foreground="#475467"/>
              <TextBlock x:Name="GpuSummaryText" Text="正在读取..." Margin="0,6,0,0" FontSize="14" FontWeight="SemiBold" Foreground="#1D2939" TextWrapping="Wrap"/>
            </StackPanel>
            <StackPanel Grid.Row="0" Grid.Column="1">
              <TextBlock Text="当前操作" FontWeight="SemiBold" Foreground="#475467"/>
              <TextBlock x:Name="CurrentActionText" Text="空闲" Margin="0,6,0,0" FontSize="14" FontWeight="SemiBold" Foreground="#1D2939" TextWrapping="Wrap"/>
            </StackPanel>
            <ProgressBar x:Name="ActionProgress" Grid.Row="1" Grid.ColumnSpan="2" Height="6" Margin="0,16,0,0" IsIndeterminate="False" Value="0"/>
          </Grid>
        </Border>

        <Border Grid.Row="1" Margin="0,14,0,0" Padding="0" Background="#101828" BorderBrush="#243247" BorderThickness="1" CornerRadius="8">
          <Grid>
            <Grid.RowDefinitions><RowDefinition Height="48"/><RowDefinition Height="*"/></Grid.RowDefinitions>
            <Border Grid.Row="0" Background="#182230" CornerRadius="8,8,0,0">
              <Grid Margin="16,0">
                <TextBlock Text="运行日志" Foreground="White" FontWeight="SemiBold" VerticalAlignment="Center"/>
                <TextBlock Text="仅展示状态与日志，不显示任务密钥" Foreground="#98A2B3" FontSize="11" VerticalAlignment="Center" HorizontalAlignment="Right"/>
              </Grid>
            </Border>
            <TextBox x:Name="LogBox" Grid.Row="1" Margin="0" Padding="16,12" Background="#101828" Foreground="#D0D5DD" BorderThickness="0" FontFamily="Consolas" FontSize="12" IsReadOnly="True" AcceptsReturn="True" TextWrapping="NoWrap" HorizontalScrollBarVisibility="Auto" VerticalScrollBarVisibility="Auto"/>
          </Grid>
        </Border>
      </Grid>
    </Grid>

    <Border Grid.Row="3" Background="White" BorderBrush="{StaticResource BorderBrush}" BorderThickness="0,1,0,0">
      <Grid Margin="20,0">
        <TextBlock x:Name="FooterText" Text="控制台已就绪" Foreground="#667085" VerticalAlignment="Center"/>
        <TextBlock Text="单任务本机训练 · 不启动项目容器" Foreground="#98A2B3" VerticalAlignment="Center" HorizontalAlignment="Right"/>
      </Grid>
    </Border>
  </Grid>
</Window>
'@

$Reader = New-Object System.Xml.XmlNodeReader $Xaml
$Window = [Windows.Markup.XamlReader]::Load($Reader)

$ControlNames = @(
    "ConfigDot", "ConfigStatus", "EnvironmentDot", "EnvironmentStatus",
    "RunnerDot", "RunnerStatus", "ServerDot", "ServerStatus",
    "ServerUrlInput", "RunnerIdInput", "RunnerNameInput", "TokenInput",
    "RuntimeRootInput", "SaveConfigButton", "InstallButton", "CheckButton",
    "SmokeButton", "ProbeButton", "StartButton", "StopButton",
    "QuickStartButton", "QuickStopButton", "QuickCheckButton",
    "QuickProbeButton", "OpenArtifactsButton", "QuickFeedbackText",
    "OperationFeedbackText", "GpuSummaryText", "CurrentActionText",
    "ActionProgress", "LogBox", "FooterText"
)
$Controls = @{}
foreach ($Name in $ControlNames) {
    $Controls[$Name] = $Window.FindName($Name)
}

$StateColors = @{
    good = "#12A87A"
    warn = "#D98A00"
    bad = "#D92D20"
    idle = "#98A2B3"
    busy = "#456FE8"
}

$ActionDefinitions = @{
    install = @{
        label = "安装训练环境"
        running = "正在安装训练环境；首次安装需要下载依赖，可能耗时较长"
        button = "安装中…"
        success = "训练环境安装完成"
    }
    check = @{
        label = "环境检查"
        running = "正在检查 CUDA、4-bit NF4 与 ms-swift；首次加载通常需要 2–4 分钟"
        button = "检查中…"
        success = "环境检查通过"
    }
    smoke = @{
        label = "1 Step 实训"
        running = "正在执行 1 Step 真实 QLoRA；期间会占用本机 GPU"
        button = "实训中…"
        success = "1 Step 实训完成"
    }
    probe = @{
        label = "任务连接检测"
        running = "正在验证任务 URL、密钥与服务器连接"
        button = "检测中…"
        success = "任务连接检测通过"
    }
}

function Get-ActionDefinition {
    param([string]$Action)
    if ($ActionDefinitions.ContainsKey($Action)) {
        return $ActionDefinitions[$Action]
    }
    return @{
        label = $Action
        running = "正在执行：$Action"
        button = "执行中…"
        success = "操作完成：$Action"
    }
}

function Format-Elapsed {
    param([DateTime]$StartedAt)
    if ($StartedAt -eq [DateTime]::MinValue) {
        return "00:00"
    }
    $Elapsed = [DateTime]::Now - $StartedAt
    $Hours = [Math]::Floor($Elapsed.TotalHours)
    if ($Hours -gt 0) {
        return "{0}:{1:00}:{2:00}" -f $Hours, $Elapsed.Minutes, $Elapsed.Seconds
    }
    return "{0:00}:{1:00}" -f [Math]::Floor($Elapsed.TotalMinutes), $Elapsed.Seconds
}

function Set-OperationFeedback {
    param(
        [ValidateSet("good", "warn", "bad", "idle", "busy")]
        [string]$State,
        [Parameter(Mandatory = $true)][string]$Message
    )
    $script:OperationFeedbackState = $State
    $script:OperationFeedbackMessage = $Message
    $Color = switch ($State) {
        "good" { "#027A48" }
        "warn" { "#B54708" }
        "bad" { "#B42318" }
        "busy" { "#175CD3" }
        default { "#667085" }
    }
    $Brush = New-Object Windows.Media.SolidColorBrush (
        [Windows.Media.ColorConverter]::ConvertFromString($Color)
    )
    foreach ($TextControl in @(
        $Controls.QuickFeedbackText,
        $Controls.OperationFeedbackText
    )) {
        if ($TextControl) {
            $TextControl.Text = $Message
            $TextControl.Foreground = $Brush
        }
    }
}

function Set-ActionButtonContent {
    param(
        [string]$Action = "",
        [bool]$Running = $false
    )
    $Controls.InstallButton.Content = "安装环境"
    $Controls.CheckButton.Content = "环境检查"
    $Controls.QuickCheckButton.Content = "环境检查"
    $Controls.SmokeButton.Content = "1 Step 实训"
    $Controls.ProbeButton.Content = "检测任务连接"
    $Controls.QuickProbeButton.Content = "检测任务连接"
    if (-not $Running) {
        return
    }
    $ButtonText = [string](Get-ActionDefinition $Action).button
    switch ($Action) {
        "install" { $Controls.InstallButton.Content = $ButtonText }
        "check" {
            $Controls.CheckButton.Content = $ButtonText
            $Controls.QuickCheckButton.Content = $ButtonText
        }
        "smoke" { $Controls.SmokeButton.Content = $ButtonText }
        "probe" {
            $Controls.ProbeButton.Content = $ButtonText
            $Controls.QuickProbeButton.Content = $ButtonText
        }
    }
}

function Set-Indicator {
    param(
        [Parameter(Mandatory = $true)]$Dot,
        [Parameter(Mandatory = $true)]$Text,
        [Parameter(Mandatory = $true)][string]$State,
        [Parameter(Mandatory = $true)][string]$Message
    )
    $Color = $StateColors[$State]
    if (-not $Color) {
        $Color = $StateColors.idle
    }
    $Dot.Fill = New-Object Windows.Media.SolidColorBrush (
        [Windows.Media.ColorConverter]::ConvertFromString($Color)
    )
    $Text.Text = $Message
}

function Show-Message {
    param(
        [string]$Message,
        [string]$Title = "知识库模型训练控制台",
        [Windows.MessageBoxImage]$Icon = [Windows.MessageBoxImage]::Information
    )
    [void][Windows.MessageBox]::Show(
        $Window,
        $Message,
        $Title,
        [Windows.MessageBoxButton]::OK,
        $Icon
    )
}

function Read-RunnerConfig {
    $Values = @{}
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        return $Values
    }
    foreach ($RawLine in Get-Content -LiteralPath $ConfigPath -Encoding UTF8) {
        $Line = $RawLine.Trim()
        if (-not $Line -or $Line.StartsWith("#")) {
            continue
        }
        $Separator = $Line.IndexOf("=")
        if ($Separator -le 0) {
            continue
        }
        $Name = $Line.Substring(0, $Separator).Trim()
        $Value = $Line.Substring($Separator + 1).Trim()
        $Values[$Name] = $Value
    }
    return $Values
}

function Current-RuntimeRoot {
    $Value = $Controls.RuntimeRootInput.Text.Trim()
    if (-not $Value) {
        $Value = $RuntimeRoot
    }
    return [IO.Path]::GetFullPath($Value)
}

function Test-ConfigComplete {
    $Url = $Controls.ServerUrlInput.Text.Trim()
    $RunnerId = $Controls.RunnerIdInput.Text.Trim()
    $Token = $Controls.TokenInput.Password
    $Uri = $null
    $ValidUrl = (
        [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$Uri) -and
        $Uri.Scheme -in @("http", "https") -and
        -not $Uri.UserInfo -and
        -not $Uri.Query -and
        -not $Uri.Fragment -and
        $Uri.AbsolutePath -match "^/api/v1/embedding-model/runner/tasks/etj-[A-Za-z0-9._-]+/?$"
    )
    return (
        $ValidUrl -and
        $RunnerId -match "^[A-Za-z0-9._-]+$" -and
        $Token.Length -ge 24
    )
}

function Save-RunnerConfig {
    param([switch]$Silent)
    try {
        $Url = $Controls.ServerUrlInput.Text.Trim().TrimEnd("/")
        $RunnerId = $Controls.RunnerIdInput.Text.Trim()
        $RunnerName = $Controls.RunnerNameInput.Text.Trim()
        $Token = $Controls.TokenInput.Password
        $ResolvedRoot = Current-RuntimeRoot
        $TaskUri = $null
        if (
            -not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$TaskUri) -or
            $TaskUri.Scheme -notin @("http", "https") -or
            $TaskUri.UserInfo -or
            $TaskUri.Query -or
            $TaskUri.Fragment -or
            $TaskUri.AbsolutePath -notmatch "^/api/v1/embedding-model/runner/tasks/etj-[A-Za-z0-9._-]+/?$"
        ) {
            throw "请粘贴工作台生成的完整 LoRA 任务接入 URL"
        }
        if ($RunnerId -and $RunnerId -notmatch "^[A-Za-z0-9._-]+$") {
            throw "Runner 标识只允许字母、数字、点、横线和下划线"
        }
        if ($Token -and $Token.Length -lt 24) {
            throw "Runner 密钥至少需要 24 位"
        }
        foreach ($Value in @($Url, $RunnerId, $RunnerName, $Token, $ResolvedRoot)) {
            if ($Value -match "[`r`n]") {
                throw "配置值不能包含换行"
            }
        }
        if (-not $RunnerName) {
            $RunnerName = $RunnerId
        }
        $Lines = @(
            "# 由知识库模型训练控制台保存。请勿提交此文件。",
            "TRAINING_JOB_URL=$Url",
            "TRAINING_JOB_TOKEN=$Token",
            "TRAINING_RUNNER_ID=$RunnerId",
            "TRAINING_RUNNER_NAME=$RunnerName",
            "TRAINING_POLL_SECONDS=10",
            "CUDA_VISIBLE_DEVICES=0",
            "TRAINING_RUNTIME_ROOT=$ResolvedRoot"
        )
        [IO.File]::WriteAllLines(
            $ConfigPath,
            $Lines,
            [Text.UTF8Encoding]::new($false)
        )
        $Controls.RuntimeRootInput.Text = $ResolvedRoot
        $script:FooterMessage = "任务接入信息已保存"
        if (-not $Silent) {
            Show-Message "任务 URL 和密钥已保存到本机。任务密钥不会写入运行日志。"
        }
        return $true
    } catch {
        Show-Message $_.Exception.Message "配置保存失败" ([Windows.MessageBoxImage]::Error)
        return $false
    }
}

function Get-GpuSnapshot {
    try {
        if (-not $NvidiaSmiPath) {
            throw "nvidia-smi 不可用"
        }
        $Line = & $NvidiaSmiPath `
            --query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu `
            --format=csv,noheader,nounits 2>$null |
            Select-Object -First 1
        if (-not $Line) {
            throw "nvidia-smi 不可用"
        }
        $Parts = @($Line -split "," | ForEach-Object { $_.Trim() })
        return @{
            name = $Parts[0]
            total = [int][double]$Parts[1]
            used = [int][double]$Parts[2]
            free = [int][double]$Parts[3]
            utilization = [int][double]$Parts[4]
        }
    } catch {
        return $null
    }
}

function Test-EnvironmentReady {
    $Root = Current-RuntimeRoot
    return (
        (Test-Path -LiteralPath (Join-Path $Root ".venv\Scripts\python.exe")) -and
        (Test-Path -LiteralPath (Join-Path $Root ".venv\Scripts\swift.exe"))
    )
}

function Runner-PidFile {
    return Join-Path (Current-RuntimeRoot) "runner.pid"
}

function Get-RunnerPid {
    $PidFile = Runner-PidFile
    if (-not (Test-Path -LiteralPath $PidFile -PathType Leaf)) {
        return 0
    }
    $Value = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $Parsed = 0
    if (-not [int]::TryParse([string]$Value, [ref]$Parsed)) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return 0
    }
    if (-not (Get-Process -Id $Parsed -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return 0
    }
    $ProcessRecord = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $Parsed" `
        -ErrorAction SilentlyContinue
    $CommandLine = [string]$ProcessRecord.CommandLine
    if (
        -not $CommandLine.Contains(
            $HostRunnerPath,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not $CommandLine.Contains(
            "-Action run",
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
        return 0
    }
    return $Parsed
}

function Stop-ProcessTree {
    param([Parameter(Mandatory = $true)][int]$RootPid)
    $Processes = @(Get-CimInstance Win32_Process)
    $Targets = [Collections.Generic.HashSet[int]]::new()
    [void]$Targets.Add($RootPid)
    do {
        $Added = $false
        foreach ($Process in $Processes) {
            if (
                $Targets.Contains([int]$Process.ParentProcessId) -and
                -not $Targets.Contains([int]$Process.ProcessId)
            ) {
                [void]$Targets.Add([int]$Process.ProcessId)
                $Added = $true
            }
        }
    } while ($Added)
    $Targets |
        Sort-Object -Descending |
        ForEach-Object {
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
        }
}

function Start-HostProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Action,
        [Parameter(Mandatory = $true)][string]$LogPrefix
    )
    $Root = Current-RuntimeRoot
    New-Item -ItemType Directory -Force -Path $Root | Out-Null
    $Stdout = Join-Path $Root "$LogPrefix.stdout.log"
    $Stderr = Join-Path $Root "$LogPrefix.stderr.log"
    Remove-Item -LiteralPath $Stdout, $Stderr -Force -ErrorAction SilentlyContinue
    $Arguments = (
        "-NoProfile -File `"$HostRunnerPath`" " +
        "-Action $Action " +
        "-RuntimeRoot `"$Root`" " +
        "-ConfigPath `"$ConfigPath`""
    )
    $Process = Start-Process `
        -FilePath $PwshPath `
        -ArgumentList $Arguments `
        -WindowStyle Hidden `
        -RedirectStandardOutput $Stdout `
        -RedirectStandardError $Stderr `
        -PassThru
    return @{
        process = $Process
        stdout = $Stdout
        stderr = $Stderr
    }
}

function Start-HostAction {
    param([Parameter(Mandatory = $true)][string]$Action)
    if ($script:ActionProcess -and -not $script:ActionProcess.HasExited) {
        $CurrentDefinition = Get-ActionDefinition $script:ActionName
        $Message = "$($CurrentDefinition.label)正在执行，请等待当前操作结束。"
        Set-OperationFeedback "warn" $Message
        Show-Message $Message
        return
    }
    if ($Action -eq "probe") {
        if (-not (Save-RunnerConfig -Silent)) {
            Set-OperationFeedback "warn" "任务连接检测未开始：请先填写有效的任务 URL 和密钥。"
            return
        }
        if (-not (Test-ConfigComplete)) {
            Set-OperationFeedback "warn" "任务连接检测未开始：任务 URL、密钥或 Runner 标识不完整。"
            Show-Message "请先粘贴完整的任务 URL、任务密钥，并填写 Runner 标识。" "任务信息不完整" ([Windows.MessageBoxImage]::Warning)
            return
        }
    }
    $Definition = Get-ActionDefinition $Action
    try {
        $Started = Start-HostProcess -Action $Action -LogPrefix "console-action"
        $script:ActionProcess = $Started.process
        $script:ActionStdout = $Started.stdout
        $script:ActionStderr = $Started.stderr
        $script:ActionName = $Action
        $script:ActionStartedAt = [DateTime]::Now
        $script:FooterMessage = "$($Definition.label)已启动"
        Set-OperationFeedback "busy" "$($Definition.running)。已用时 00:00。"
    } catch {
        Set-OperationFeedback "bad" "$($Definition.label)未能启动：$($_.Exception.Message)"
        Show-Message $_.Exception.Message "无法启动操作" ([Windows.MessageBoxImage]::Error)
    }
}

function Start-Runner {
    if (Get-RunnerPid) {
        Set-OperationFeedback "warn" "当前 LoRA 任务已经在运行。"
        Show-Message "当前 LoRA 任务已经在运行。"
        return
    }
    if (-not (Save-RunnerConfig -Silent)) {
        Set-OperationFeedback "warn" "训练任务未开始：请先检查并保存任务接入信息。"
        return
    }
    if (-not (Test-ConfigComplete)) {
        Set-OperationFeedback "warn" "训练任务未开始：任务 URL、密钥或 Runner 标识不完整。"
        Show-Message "请先粘贴完整的任务 URL、任务密钥，并填写 Runner 标识。" "任务信息不完整" ([Windows.MessageBoxImage]::Warning)
        return
    }
    if (-not (Test-EnvironmentReady)) {
        Set-OperationFeedback "warn" "训练任务未开始：独立训练环境尚未就绪。"
        Show-Message "训练环境尚未安装，请先点击“安装环境”。" "环境未就绪" ([Windows.MessageBoxImage]::Warning)
        return
    }
    try {
        Set-OperationFeedback "busy" "正在启动本机 Runner 并领取指定 LoRA 任务。"
        $Started = Start-HostProcess -Action "run" -LogPrefix "runner"
        $script:RunnerProcess = $Started.process
        $script:RunnerStdout = $Started.stdout
        $script:RunnerStderr = $Started.stderr
        $script:RunnerStartedAt = [DateTime]::Now
        [IO.File]::WriteAllText(
            (Runner-PidFile),
            [string]$Started.process.Id,
            [Text.Encoding]::ASCII
        )
        $script:FooterMessage = "LoRA 任务已启动，正在领取指定任务"
    } catch {
        Set-OperationFeedback "bad" "Runner 启动失败：$($_.Exception.Message)"
        Show-Message $_.Exception.Message "Runner 启动失败" ([Windows.MessageBoxImage]::Error)
    }
}

function Stop-Runner {
    $RunnerPid = Get-RunnerPid
    if (-not $RunnerPid) {
        Set-OperationFeedback "idle" "Runner 当前没有运行。"
        Show-Message "Runner 当前没有运行。"
        return
    }
    $Choice = [Windows.MessageBox]::Show(
        $Window,
        "停止当前训练会中断 LoRA 任务。服务器会在租约过期后允许使用同一任务 URL 和密钥重新开始。是否继续？",
        "确认停止当前训练",
        [Windows.MessageBoxButton]::YesNo,
        [Windows.MessageBoxImage]::Warning
    )
    if ($Choice -ne [Windows.MessageBoxResult]::Yes) {
        return
    }
    Stop-ProcessTree -RootPid $RunnerPid
    Remove-Item -LiteralPath (Runner-PidFile) -Force -ErrorAction SilentlyContinue
    $script:RunnerProcess = $null
    $script:RunnerStartedAt = $null
    $script:FooterMessage = "当前训练已停止"
    Set-OperationFeedback "good" "当前训练已停止。"
}

function Get-LogEncoding {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Utf8 = [Text.UTF8Encoding]::new($false, $true)
    $Stream = $null
    try {
        $Stream = [IO.File]::Open(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite
        )
        $Length = [Math]::Min(4096, [int]$Stream.Length)
        if ($Length -le 0) {
            return [Text.UTF8Encoding]::new($false)
        }
        $Buffer = [byte[]]::new($Length)
        [void]$Stream.Read($Buffer, 0, $Length)
        try {
            [void]$Utf8.GetString($Buffer)
            return [Text.UTF8Encoding]::new($false)
        } catch {
            $CodePage = [Globalization.CultureInfo]::CurrentCulture.TextInfo.ANSICodePage
            return [Text.Encoding]::GetEncoding($CodePage)
        }
    } finally {
        if ($Stream) {
            $Stream.Dispose()
        }
    }
}

function Read-LogTailLines {
    param(
        [string]$Path,
        [int]$LineCount = 160
    )
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return @()
    }
    $Stream = $null
    try {
        $Encoding = Get-LogEncoding -Path $Path
        $Stream = [IO.File]::Open(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::ReadWrite
        )
        $MaxBytes = 262144
        $Start = [Math]::Max(0, $Stream.Length - $MaxBytes)
        [void]$Stream.Seek($Start, [IO.SeekOrigin]::Begin)
        $Buffer = [byte[]]::new([int]($Stream.Length - $Start))
        $Read = $Stream.Read($Buffer, 0, $Buffer.Length)
        $Text = $Encoding.GetString($Buffer, 0, $Read)
        if ($Start -gt 0) {
            $FirstLineBreak = $Text.IndexOf("`n")
            if ($FirstLineBreak -ge 0) {
                $Text = $Text.Substring($FirstLineBreak + 1)
            }
        }
        return @(
            $Text -split "\r?\n" |
                Select-Object -Last $LineCount
        )
    } catch {
        return @("[日志读取失败] $($_.Exception.Message)")
    } finally {
        if ($Stream) {
            $Stream.Dispose()
        }
    }
}

function Log-Tail {
    param([string]$Path, [string]$Title)
    $Lines = @(Read-LogTailLines -Path $Path)
    if ($Lines.Count -eq 0) {
        return @()
    }
    return @(
        "===== $Title ====="
        $Lines
    )
}

function Get-LogFailureSummary {
    param(
        [string]$StdoutPath,
        [string]$StderrPath
    )
    $Lines = @()
    $Lines += Read-LogTailLines -Path $StdoutPath -LineCount 40
    $Lines += Read-LogTailLines -Path $StderrPath -LineCount 40
    $Useful = @(
        $Lines |
            Where-Object {
                $_ -and
                $_.Trim() -and
                $_ -notmatch "(?i)(token|secret|password|key)\s*="
            } |
            Select-Object -Last 8
    )
    return ($Useful -join "`r`n")
}

function Complete-HostAction {
    $Process = $script:ActionProcess
    $Action = $script:ActionName
    $Definition = Get-ActionDefinition $Action
    $Process.WaitForExit()
    $ExitCode = $Process.ExitCode
    $Duration = Format-Elapsed $script:ActionStartedAt
    if ($Action -eq "probe") {
        $script:ServerProbeState = if ($ExitCode -eq 0) { "good" } else { "bad" }
    }
    $FailureSummary = if ($ExitCode -eq 0) {
        ""
    } else {
        Get-LogFailureSummary `
            -StdoutPath $script:ActionStdout `
            -StderrPath $script:ActionStderr
    }
    $script:ActionProcess = $null
    $script:ActionName = ""
    $script:ActionStartedAt = $null
    if ($ExitCode -eq 0) {
        $Message = "$($Definition.success)，用时 $Duration。"
        $script:FooterMessage = $Message
        Set-OperationFeedback "good" $Message
        Update-LogView
        return
    }
    $Message = "$($Definition.label)失败，退出码 $ExitCode，用时 $Duration。"
    $script:FooterMessage = $Message
    Set-OperationFeedback "bad" "$Message 请查看右侧错误日志。"
    Update-LogView
    $DialogMessage = if ($FailureSummary) {
        "$Message`r`n`r`n最近错误：`r`n$FailureSummary"
    } else {
        "$Message`r`n`r`n未捕获到更多错误输出，请查看右侧运行日志。"
    }
    Show-Message $DialogMessage "$($Definition.label)失败" ([Windows.MessageBoxImage]::Error)
}

function Complete-RunnerProcess {
    $script:RunnerProcess.WaitForExit()
    $ExitCode = $script:RunnerProcess.ExitCode
    $Duration = Format-Elapsed $script:RunnerStartedAt
    $FailureSummary = if ($ExitCode -eq 0) {
        ""
    } else {
        Get-LogFailureSummary `
            -StdoutPath $script:RunnerStdout `
            -StderrPath $script:RunnerStderr
    }
    Remove-Item -LiteralPath (Runner-PidFile) -Force -ErrorAction SilentlyContinue
    $script:RunnerProcess = $null
    $script:RunnerStartedAt = $null
    if ($ExitCode -eq 0) {
        $Message = "当前 LoRA 任务已结束，Runner 已退出，用时 $Duration。"
        $script:FooterMessage = $Message
        Set-OperationFeedback "good" $Message
        Update-LogView
        return
    }
    $Message = "当前 LoRA 任务执行失败，退出码 $ExitCode，用时 $Duration。"
    $script:FooterMessage = $Message
    Set-OperationFeedback "bad" "$Message 请查看右侧错误日志。"
    Update-LogView
    $DialogMessage = if ($FailureSummary) {
        "$Message`r`n`r`n最近错误：`r`n$FailureSummary"
    } else {
        "$Message`r`n`r`n未捕获到更多错误输出，请查看右侧运行日志。"
    }
    Show-Message $DialogMessage "训练任务失败" ([Windows.MessageBoxImage]::Error)
}

function Update-LogView {
    $Lines = @()
    $Lines += Log-Tail -Path $script:ActionStdout -Title "最近操作"
    $Lines += Log-Tail -Path $script:ActionStderr -Title "最近操作错误输出"
    $Lines += Log-Tail -Path $script:RunnerStdout -Title "Runner"
    $Lines += Log-Tail -Path $script:RunnerStderr -Title "Runner 错误输出"
    if ($Lines.Count -eq 0) {
        $Lines = @(
            "等待操作。",
            "",
            "建议顺序：粘贴任务 URL 和密钥 → 环境检查 → 保存并开始此任务。",
            "首次使用可先执行 1 Step 实训确认本机 GPU 训练能力。"
        )
    }
    $Text = $Lines -join "`r`n"
    if ($Controls.LogBox.Text -ne $Text) {
        $Controls.LogBox.Text = $Text
        $Controls.LogBox.ScrollToEnd()
    }
}

function Update-ConsoleState {
    if ($script:RunnerProcess -and $script:RunnerProcess.HasExited) {
        Complete-RunnerProcess
    }

    $ActionRunning = $false
    if ($script:ActionProcess) {
        if ($script:ActionProcess.HasExited) {
            Complete-HostAction
        } else {
            $ActionRunning = $true
        }
    }

    $ConfigComplete = Test-ConfigComplete
    if ($ConfigComplete) {
        Set-Indicator $Controls.ConfigDot $Controls.ConfigStatus "good" "LoRA 任务 URL 和密钥完整"
    } else {
        Set-Indicator $Controls.ConfigDot $Controls.ConfigStatus "warn" "可安装和实训；开始任务前需粘贴 URL 和密钥"
    }

    $EnvironmentReady = Test-EnvironmentReady
    if ($ActionRunning -and $script:ActionName -in @("install", "check", "smoke")) {
        $Definition = Get-ActionDefinition $script:ActionName
        Set-Indicator $Controls.EnvironmentDot $Controls.EnvironmentStatus "busy" "$($Definition.label)正在执行"
    } elseif ($EnvironmentReady) {
        Set-Indicator $Controls.EnvironmentDot $Controls.EnvironmentStatus "good" "Python、CUDA 与 ms-swift 已安装"
    } else {
        Set-Indicator $Controls.EnvironmentDot $Controls.EnvironmentStatus "warn" "尚未安装独立训练环境"
    }

    $RunnerPid = Get-RunnerPid
    if ($RunnerPid) {
        Set-Indicator $Controls.RunnerDot $Controls.RunnerStatus "good" "任务运行中，PID $RunnerPid"
    } else {
        Set-Indicator $Controls.RunnerDot $Controls.RunnerStatus "idle" "当前没有训练任务"
    }

    if ($ActionRunning -and $script:ActionName -eq "probe") {
        Set-Indicator $Controls.ServerDot $Controls.ServerStatus "busy" "正在检测任务连接"
    } else {
        switch ($script:ServerProbeState) {
            "good" { Set-Indicator $Controls.ServerDot $Controls.ServerStatus "good" "任务 URL 和密钥可用" }
            "bad" { Set-Indicator $Controls.ServerDot $Controls.ServerStatus "bad" "任务连接失败，请查看日志" }
            default { Set-Indicator $Controls.ServerDot $Controls.ServerStatus "idle" "尚未检测任务连接" }
        }
    }

    $Gpu = Get-GpuSnapshot
    if ($Gpu) {
        $Controls.GpuSummaryText.Text = (
            "{0}`n显存 {1} / {2} MB，空闲 {3} MB，利用率 {4}%" -f
            $Gpu.name, $Gpu.used, $Gpu.total, $Gpu.free, $Gpu.utilization
        )
    } else {
        $Controls.GpuSummaryText.Text = "未检测到 NVIDIA GPU 或 nvidia-smi 不可用"
    }

    if ($ActionRunning) {
        $Definition = Get-ActionDefinition $script:ActionName
        $Elapsed = Format-Elapsed $script:ActionStartedAt
        $Controls.CurrentActionText.Text = "$($Definition.label) · 已用时 $Elapsed"
        $Controls.ActionProgress.IsIndeterminate = $true
        $script:FooterMessage = "$($Definition.label)正在执行 · 已用时 $Elapsed"
        Set-OperationFeedback "busy" "$($Definition.running)。已用时 $Elapsed。右侧日志会持续刷新。"
    } elseif ($RunnerPid) {
        $Controls.CurrentActionText.Text = if ($script:RunnerStartedAt) {
            $Elapsed = Format-Elapsed $script:RunnerStartedAt
            "正在执行指定 LoRA 任务 · 已用时 $Elapsed"
        } else {
            "正在执行指定 LoRA 任务"
        }
        $Controls.ActionProgress.IsIndeterminate = $true
        if ($script:RunnerStartedAt) {
            Set-OperationFeedback "busy" "LoRA 任务正在运行，已用时 $Elapsed。右侧日志会持续刷新。"
        }
    } else {
        $Controls.CurrentActionText.Text = "空闲"
        $Controls.ActionProgress.IsIndeterminate = $false
        $Controls.ActionProgress.Value = 0
        Set-OperationFeedback `
            -State $script:OperationFeedbackState `
            -Message $script:OperationFeedbackMessage
    }

    Set-ActionButtonContent -Action $script:ActionName -Running $ActionRunning
    $Controls.FooterText.Text = $script:FooterMessage
    $Controls.SaveConfigButton.IsEnabled = -not $ActionRunning -and -not $RunnerPid
    $Controls.InstallButton.IsEnabled = -not $ActionRunning -and -not $RunnerPid
    $Controls.CheckButton.IsEnabled = -not $ActionRunning -and -not $RunnerPid
    $Controls.SmokeButton.IsEnabled = -not $ActionRunning -and -not $RunnerPid
    $Controls.ProbeButton.IsEnabled = -not $ActionRunning -and -not $RunnerPid
    $Controls.StartButton.IsEnabled = -not $ActionRunning -and -not $RunnerPid
    $Controls.StopButton.IsEnabled = [bool]$RunnerPid
    $Controls.QuickStartButton.IsEnabled = $Controls.StartButton.IsEnabled
    $Controls.QuickStopButton.IsEnabled = $Controls.StopButton.IsEnabled
    $Controls.QuickCheckButton.IsEnabled = $Controls.CheckButton.IsEnabled
    $Controls.QuickProbeButton.IsEnabled = $Controls.ProbeButton.IsEnabled
    Update-LogView
}

function Report-RefreshFailure {
    param([Parameter(Mandatory = $true)][Exception]$Exception)
    $Message = "界面状态刷新失败：$($Exception.Message)"
    $script:RefreshErrorMessage = $Message
    $script:FooterMessage = $Message
    Set-OperationFeedback "bad" "$Message。后台操作可能仍在运行，请查看右侧日志。"
    $Controls.FooterText.Text = $script:FooterMessage
    $Controls.CurrentActionText.Text = "状态刷新异常"
    $Controls.ActionProgress.IsIndeterminate = $false
}

function Invoke-ConsoleRefresh {
    try {
        Update-ConsoleState
        $script:RefreshErrorMessage = ""
        return $true
    } catch {
        Report-RefreshFailure -Exception $_.Exception
        return $false
    }
}

function Invoke-UiCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )
    try {
        & $Command
    } catch {
        $Message = $_.Exception.Message
        $script:FooterMessage = "$Title失败：$Message"
        Set-OperationFeedback "bad" "$Title失败：$Message"
        Show-Message $Message "$Title失败" ([Windows.MessageBoxImage]::Error)
    } finally {
        if (-not (Invoke-ConsoleRefresh) -and $script:RefreshErrorMessage) {
            Show-Message `
                $script:RefreshErrorMessage `
                "界面刷新失败" `
                ([Windows.MessageBoxImage]::Error)
        }
    }
}

function Open-TrainingArtifacts {
    $Path = Join-Path (Current-RuntimeRoot) "artifacts"
    New-Item -ItemType Directory -Force -Path $Path | Out-Null
    Start-Process -FilePath "explorer.exe" -ArgumentList @($Path) -ErrorAction Stop
    $script:FooterMessage = "已打开训练产物目录"
    Set-OperationFeedback "good" "已打开训练产物目录：$Path"
}

$Existing = Read-RunnerConfig
$Controls.ServerUrlInput.Text = [string]($Existing["TRAINING_JOB_URL"])
$Controls.RunnerIdInput.Text = if ($Existing["TRAINING_RUNNER_ID"]) {
    [string]$Existing["TRAINING_RUNNER_ID"]
} else {
    "local-rtx4070"
}
$Controls.RunnerNameInput.Text = if ($Existing["TRAINING_RUNNER_NAME"]) {
    [string]$Existing["TRAINING_RUNNER_NAME"]
} else {
    "本地 RTX 4070"
}
$Controls.TokenInput.Password = [string]($Existing["TRAINING_JOB_TOKEN"])
$Controls.RuntimeRootInput.Text = if ($Existing["TRAINING_RUNTIME_ROOT"]) {
    [string]$Existing["TRAINING_RUNTIME_ROOT"]
} else {
    $RuntimeRoot
}

$Controls.SaveConfigButton.Add_Click({
    Invoke-UiCommand "保存任务信息" {
        if (Save-RunnerConfig) {
            Set-OperationFeedback "good" "任务 URL、密钥与 Runner 信息已保存到本机。"
        }
    }
})
$Controls.InstallButton.Add_Click({
    Invoke-UiCommand "安装训练环境" { Start-HostAction "install" }
})
$Controls.CheckButton.Add_Click({
    Invoke-UiCommand "环境检查" { Start-HostAction "check" }
})
$Controls.SmokeButton.Add_Click({
    Invoke-UiCommand "1 Step 实训" {
        $Choice = [Windows.MessageBox]::Show(
            $Window,
            "将执行 1 Step 真实 QLoRA，并占用本机 GPU。是否继续？",
            "确认实训",
            [Windows.MessageBoxButton]::YesNo,
            [Windows.MessageBoxImage]::Question
        )
        if ($Choice -eq [Windows.MessageBoxResult]::Yes) {
            Start-HostAction "smoke"
        } else {
            Set-OperationFeedback "idle" "已取消 1 Step 实训。"
        }
    }
})
$Controls.ProbeButton.Add_Click({
    Invoke-UiCommand "任务连接检测" { Start-HostAction "probe" }
})
$Controls.StartButton.Add_Click({
    Invoke-UiCommand "启动训练任务" { Start-Runner }
})
$Controls.TokenInput.Add_KeyDown({
    if ($_.Key -eq [Windows.Input.Key]::Enter) {
        Invoke-UiCommand "启动训练任务" { Start-Runner }
    }
})
$Controls.StopButton.Add_Click({
    Invoke-UiCommand "停止训练任务" { Stop-Runner }
})
$Controls.QuickProbeButton.Add_Click({
    Invoke-UiCommand "任务连接检测" { Start-HostAction "probe" }
})
$Controls.QuickStartButton.Add_Click({
    Invoke-UiCommand "启动训练任务" { Start-Runner }
})
$Controls.QuickStopButton.Add_Click({
    Invoke-UiCommand "停止训练任务" { Stop-Runner }
})
$Controls.QuickCheckButton.Add_Click({
    Invoke-UiCommand "环境检查" { Start-HostAction "check" }
})
$Controls.OpenArtifactsButton.Add_Click({
    Invoke-UiCommand "打开训练产物" { Open-TrainingArtifacts }
})

$Timer = New-Object Windows.Threading.DispatcherTimer
$Timer.Interval = [TimeSpan]::FromSeconds(1.5)
$Timer.Add_Tick({ [void](Invoke-ConsoleRefresh) })
$Timer.Start()

$Window.Add_Closed({
    $Timer.Stop()
})

[void](Invoke-ConsoleRefresh)
if ($ValidateOnly) {
    $Timer.Stop()
    if ($script:RefreshErrorMessage) {
        throw $script:RefreshErrorMessage
    }
    Write-Output "Runner console validation passed"
    exit 0
}
[void]$Window.ShowDialog()
