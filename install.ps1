# Install Ferry (rolling context, curation=ferry) for ONE mind, AS THAT USER.
# Windows twin of install.sh.
#
# Run: powershell -ExecutionPolicy Bypass -File install.ps1
#
# Config: %USERPROFILE%\preferences.json "ferry" block, the ONE source -- see
# hooks/ferry_config.py. A port is REQUIRED; there is no shared default:
#     {"ferry": {"port": 5701}}
# Honours CLAUDE_CONFIG_DIR for settings.json and the plugin link.
# No admin, no service: Claude Code's SessionStart hook starts the proxy.
# Requires Python 3.8+ (pure stdlib, no pip install).

$ErrorActionPreference = 'Stop'

$ScriptDir = $PSScriptRoot
. (Join-Path $ScriptDir 'hooks\ferry_common.ps1')

Write-Host '=== Ferry Installer (Windows) ==='
Write-Host ''

# 1. Python
Write-Host '[1/4] Checking Python...'
if (-not (Initialize-FerryPython)) {
    Write-Host '  ERROR: Python 3.8+ not found (tried: py -3, python, python3). Install it and try again.'
    exit 1
}
Write-Host "  Found $($Script:FerryPy) (pure stdlib, no pip install needed)"

# 2. Resolve config from the one declared source. Refuses (writes nothing) with
# no port, or if settings.json already routes this mind away from Anthropic and
# preferences.json does not name ferry "upstream".
Write-Host '[2/4] Resolving Ferry config (~\preferences.json "ferry")...'
$r = Invoke-FerryConf @('install')
$r.Output | ForEach-Object { Write-Host $_ }
if ($r.Code -ne 0) { Write-Host '  Install NOT complete.'; exit 1 }
$cfg = Get-FerryEnv
if ($null -eq $cfg) { Write-Host '  ERROR: ferry_config.py env failed. Install NOT complete.'; exit 1 }
$ClaudeDir = $cfg['FERRY_CONFIG_DIR']

# 3. Register plugin (a junction: no admin or Developer Mode needed)
Write-Host '[3/4] Registering Claude Code plugin...'
$PluginsDir = Join-Path $ClaudeDir 'plugins'
$PluginLink = Join-Path $PluginsDir 'rolling-context'
New-Item -ItemType Directory -Path $PluginsDir -Force | Out-Null
Remove-FerryLink $PluginLink | Out-Null
New-Item -ItemType Junction -Path $PluginLink -Value $ScriptDir | Out-Null
Write-Host "  Plugin linked at $PluginLink"

# Every curated prefix tells the mind to run a bare `ferry-fetch`. Two shims in
# ~\.local\bin: .cmd for cmd/PowerShell, extensionless sh for Git Bash (which
# is what Claude Code's Bash tool runs on Windows).
$shim = Get-FerryShimPaths
New-Item -ItemType Directory -Path $shim.Dir -Force | Out-Null
$fetch = Join-Path $ScriptDir 'bin\ferry-fetch'
$cmdText = "@echo off`r`nrem $($Script:FerryShimMarker) for $fetch`r`n`"$($Script:FerryPy)`" `"$fetch`" %*`r`n"
[IO.File]::WriteAllText($shim.Cmd, $cmdText, (New-Object Text.ASCIIEncoding))
$shQuote = { param($s) "'" + ($s -replace '\\', '/' -replace "'", "'\''") + "'" }
$shText = "#!/bin/sh`n# $($Script:FerryShimMarker) for $fetch`nexec $(& $shQuote $Script:FerryPy) $(& $shQuote $fetch) `"`$@`"`n"
[IO.File]::WriteAllText($shim.Sh, $shText, (New-Object Text.UTF8Encoding $false))
Write-Host "  ferry-fetch shims written to $($shim.Dir)"
$onPath = @($env:PATH -split ';' | ForEach-Object { $_.TrimEnd('\') }) -contains $shim.Dir.TrimEnd('\')
if (-not $onPath) {
    Write-Host "  WARNING: $($shim.Dir) is not on PATH -- add it (and to Git Bash's PATH),"
    Write-Host "           or the mind's ferry-fetch (named in every curated prefix) is 'command not found'."
}

# 4. Start + verify the proxy, through the ONE launcher (the SessionStart hook),
# in a child PowerShell so its exit and env changes stay its own. It points
# ANTHROPIC_BASE_URL at the port only after its own process holds it.
Write-Host "[4/4] Starting proxy on port $($cfg['ROLLING_CONTEXT_PORT'])..."
$psExe = (Get-Process -Id $PID).Path
& $psExe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ScriptDir 'hooks\start-proxy.ps1')
if ($LASTEXITCODE -ne 0) { Write-Host '  Install NOT complete.'; exit 1 }
Write-Host "  Proxy verified on port $($cfg['ROLLING_CONTEXT_PORT']); ANTHROPIC_BASE_URL points at it."

Write-Host ''
Write-Host '=== Installation Complete ==='
Write-Host ''
Write-Host "  ROLLING_CONTEXT_PORT     = $($cfg['ROLLING_CONTEXT_PORT'])"
Write-Host "  ROLLING_CONTEXT_UPSTREAM = $($cfg['ROLLING_CONTEXT_UPSTREAM'])"
Write-Host "  FERRY_DATA               = $($cfg['FERRY_DATA'])"
Write-Host "  settings.json            = $(Join-Path $ClaudeDir 'settings.json')"
Write-Host ''
Write-Host "Prove it once:  `"$($Script:FerryPy)`" `"$(Join-Path $ScriptDir 'tools\ferry-canary')`""
Write-Host 'Start a new Claude Code session to route through Ferry.'
