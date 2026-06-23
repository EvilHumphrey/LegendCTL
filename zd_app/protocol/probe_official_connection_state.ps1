param(
    [string]$AppPath = (Join-Path $env:USERPROFILE 'Downloads\ZD Game Zone 3.7.exe'),
    [int]$LaunchWaitMs = 6000,
    [switch]$LaunchIfMissing
)

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Get-WindowByNameLike {
    param(
        [string[]]$Patterns
    )

    $root = [System.Windows.Automation.AutomationElement]::RootElement
    $children = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Children,
        [System.Windows.Automation.Condition]::TrueCondition
    )

    for ($index = 0; $index -lt $children.Count; $index++) {
        $candidate = $children.Item($index)
        $name = $candidate.Current.Name
        if ([string]::IsNullOrWhiteSpace($name)) {
            continue
        }
        foreach ($pattern in $Patterns) {
            if ($name -eq $pattern -or $name -like $pattern) {
                return $candidate
            }
        }
    }
    return $null
}

function Wait-ForWindow {
    param(
        [string[]]$Patterns,
        [int]$TimeoutMs = 6000,
        [int]$PollMs = 200
    )

    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    while ((Get-Date) -lt $deadline) {
        $window = Get-WindowByNameLike -Patterns $Patterns
        if ($null -ne $window) {
            return $window
        }
        Start-Sleep -Milliseconds $PollMs
    }
    return $null
}

function Collect-VisibleNames {
    param(
        $Window,
        [int]$Limit = 200
    )

    if ($null -eq $Window) {
        return @()
    }

    $all = $Window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )

    $names = New-Object System.Collections.Generic.List[string]
    for ($index = 0; $index -lt $all.Count; $index++) {
        if ($names.Count -ge $Limit) {
            break
        }
        $name = $all.Item($index).Current.Name
        if ([string]::IsNullOrWhiteSpace($name)) {
            continue
        }
        if (-not $names.Contains($name)) {
            $names.Add($name)
        }
    }

    return @($names.ToArray())
}

$mainPatterns = @('ZDGame', '*ZDGame*')
$settingsPatterns = @('Controller Settings', '*Controller Settings*', '*Controller Setting*')

$launched = $false
$mainWindow = Get-WindowByNameLike -Patterns $mainPatterns
$settingsWindow = Get-WindowByNameLike -Patterns $settingsPatterns

if ($null -eq $mainWindow -and $null -eq $settingsWindow -and $LaunchIfMissing -and (Test-Path -LiteralPath $AppPath)) {
    Start-Process -FilePath $AppPath | Out-Null
    $launched = $true
    Start-Sleep -Milliseconds 800
    $mainWindow = Wait-ForWindow -Patterns $mainPatterns -TimeoutMs $LaunchWaitMs
    $settingsWindow = Get-WindowByNameLike -Patterns $settingsPatterns
}

$mainNames = Collect-VisibleNames -Window $mainWindow
$settingsNames = Collect-VisibleNames -Window $settingsWindow
$visibleNames = @($mainNames + $settingsNames | Sort-Object -Unique)

[pscustomobject]@{
    app_running = [bool]($mainWindow -or $settingsWindow)
    launched = [bool]$launched
    main_window = if ($null -ne $mainWindow) { $mainWindow.Current.Name } else { $null }
    settings_window = if ($null -ne $settingsWindow) { $settingsWindow.Current.Name } else { $null }
    device_settings_button = [bool]($mainNames -contains 'Device Settings')
    no_device_connected = [bool]($visibleNames -contains 'No ZD device connected')
    connect_via_usb = [bool]($visibleNames -contains 'Please select a device and connect via USB')
    visible_names = $visibleNames
} | ConvertTo-Json -Depth 4
