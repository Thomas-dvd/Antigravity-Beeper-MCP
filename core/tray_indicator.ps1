param(
    [int]$ParentPid = 0,
    [string]$Tooltip = "Antigravity Beeper Hub",
    [string]$IconPath = ""
)

try {
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing

    $script:ParentPid = $ParentPid
    $script:shouldExit = $false
    $script:secretFile = Join-Path (Split-Path $PSScriptRoot -Parent) "data\.hub_secret"

    $notify = New-Object System.Windows.Forms.NotifyIcon
    $script:notify = $notify

    if ($IconPath -and (Test-Path $IconPath)) {
        try {
            $notify.Icon = New-Object System.Drawing.Icon($IconPath)
        } catch {
            $notify.Icon = [System.Drawing.SystemIcons]::Information
        }
    } else {
        $notify.Icon = [System.Drawing.SystemIcons]::Information
    }

    $notify.Text = $Tooltip

    # Right-click Context Menu
    $menu = New-Object System.Windows.Forms.ContextMenuStrip

    $header = $menu.Items.Add("Antigravity Beeper Hub")
    $header.Enabled = $false
    $header.Font = New-Object System.Drawing.Font($menu.Font, [System.Drawing.FontStyle]::Bold)

    $menu.Items.Add("-") | Out-Null

    $exitItem = $menu.Items.Add("Quit Hub")
    $exitItem.add_Click({
        $script:shouldExit = $true
        if ($script:ParentPid -gt 0) {
            try {
                if (Test-Path $script:secretFile) {
                    $secret = (Get-Content $script:secretFile -Raw).Trim()
                    $headers = @{ "X-Hub-Token" = $secret }
                    Invoke-RestMethod -Uri "http://127.0.0.1:24680/shutdown" -Method Post -Headers $headers -TimeoutSec 2 -ErrorAction SilentlyContinue | Out-Null
                }
            } catch {}

            Start-Sleep -Milliseconds 400
            $p = Get-Process -Id $script:ParentPid -ErrorAction SilentlyContinue
            if ($p -and -not $p.HasExited) {
                Stop-Process -Id $script:ParentPid -Force -ErrorAction SilentlyContinue
            }
        }
        $script:notify.Visible = $false
        $script:notify.Dispose()
    })

    $notify.ContextMenuStrip = $menu
    $notify.Visible = $true

    if ($ParentPid -gt 0) {
        while (-not $script:shouldExit) {
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 400
            $proc = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
            if (-not $proc -or $proc.HasExited) {
                break
            }
        }
    }

    $notify.Visible = $false
    $notify.Dispose()
} catch {
    # Suppress errors on exit
}
