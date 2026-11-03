# Uninstall the Rolling Context (Ferry) plugin for ONE mind. Windows twin of
# uninstall.sh. Honours CLAUDE_CONFIG_DIR. Never touches FERRY_DATA (the
# archive) and never writes preferences.json.
#
# Run: powershell -ExecutionPolicy Bypass -File uninstall.ps1

$ErrorActionPreference = 'Continue'

$ScriptDir = $PSScriptRoot
. (Join-Path $ScriptDir 'hooks\ferry_common.ps1')

$HavePy = Initialize-FerryPython
$ClaudeDir = Get-FerryDefaultConfigDir
$Port = 0
if ($HavePy) {
    # A config error (e.g. no port) is fine here: we only lose the port fallback.
    $cfg = Get-FerryEnv
    if ($cfg) {
        $ClaudeDir = $cfg['FERRY_CONFIG_DIR']
        if ($cfg['ROLLING_CONTEXT_PORT'] -match '^\d+$') { $Port = [int]$cfg['ROLLING_CONTEXT_PORT'] }
    }
}
$PidFile = Join-Path $ClaudeDir 'rolling-context-proxy.pid'
$PluginLink = Join-Path $ClaudeDir 'plugins\rolling-context'
$MarketplaceCache = Join-Path $ClaudeDir 'plugins\cache\rolling-context-marketplace'
$MarketplaceDir = Join-Path $ClaudeDir 'plugins\marketplaces\rolling-context-marketplace'

Write-Host '=== Uninstalling Rolling Context ==='

# Stop proxy -- PID file first, then whoever of OURS holds the port. Only ever
# this user's server.py: a stale pidfile or a shared port may name something
# else entirely (another mind's proxy, or an unrelated program).
$stopped = $false
if (Test-Path -LiteralPath $PidFile) {
    $saved = "$(Get-Content -LiteralPath $PidFile -Raw)".Trim()
    if ($saved -match '^\d+$') {
        $info = Get-FerryProcess ([int]$saved)
        if ($info -and (Test-FerrySameUser $info) -and (Test-FerryIsProxy $info)) {
            Stop-Process -Id ([int]$saved) -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped proxy (PID $saved)"
            $stopped = $true
        }
    }
}
if (-not $stopped -and $Port -gt 0) {
    $holder = Get-FerryPortHolder $Port
    if ($holder) {
        $info = Get-FerryProcess $holder
        if (Test-FerryIsProxy $info) {
            Stop-Process -Id $holder -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped proxy on port $Port (PID $holder)"
        }
    }
}
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $ClaudeDir 'rolling-context-proxy.version') -Force -ErrorAction SilentlyContinue

# Remove all log files
foreach ($f in @('rolling-context-proxy.log', 'rolling-context-proxy.log.err',
                 'rolling-context-debug.log', 'rolling-context-hook.log')) {
    Remove-Item -LiteralPath (Join-Path $ClaudeDir $f) -Force -ErrorAction SilentlyContinue
}

# Remove plugin link (manual install) -- as a link, never following it into the repo
if (Remove-FerryLink $PluginLink) { Write-Host 'Removed plugin link' }

# Remove the ferry-fetch shims install.ps1 wrote -- only if they are still ours,
# for this repo
$shim = Get-FerryShimPaths
$fetch = Join-Path $ScriptDir 'bin\ferry-fetch'
foreach ($p in @($shim.Cmd, $shim.Sh)) {
    $text = Get-Content -LiteralPath $p -Raw -ErrorAction SilentlyContinue
    if ($text -and $text.Contains("$($Script:FerryShimMarker) for $fetch")) {
        Remove-Item -LiteralPath $p -Force
        Write-Host "Removed $p"
    }
}

# Remove marketplace-installed plugin cache / registration
if (Test-Path -LiteralPath $MarketplaceCache) {
    Remove-FerryLink $MarketplaceCache | Out-Null
    Write-Host 'Removed marketplace plugin cache'
}
if (Test-Path -LiteralPath $MarketplaceDir) {
    Remove-FerryLink $MarketplaceDir | Out-Null
    Write-Host 'Removed marketplace registration'
}

# Clean installed_plugins.json / known_marketplaces.json with Python's json,
# as uninstall.sh does (5.1's ConvertTo-Json rewrites what it does not touch).
# One line, single quotes only: 5.1 mangles embedded double quotes and drops
# empty-string arguments to native commands, hence '-' for "top level".
$jsonDrop = "import json,sys; p,sub,k,msg=sys.argv[1:5]; d=json.load(open(p,encoding='utf-8')); t=d if sub=='-' else d.get(sub,{}); hit=isinstance(t,dict) and t.pop(k,None) is not None; hit and open(p,'w',encoding='utf-8').write(json.dumps(d,indent=2)); print(msg if hit else '')"
if ($HavePy) {
    $InstalledFile = Join-Path $ClaudeDir 'plugins\installed_plugins.json'
    if (Test-Path -LiteralPath $InstalledFile) {
        & $Script:FerryPy -c $jsonDrop $InstalledFile 'plugins' 'rolling-context@rolling-context-marketplace' 'Removed from installed plugins' |
            Where-Object { $_ } | ForEach-Object { Write-Host $_ }
    }
    $MarketplacesFile = Join-Path $ClaudeDir 'plugins\known_marketplaces.json'
    if (Test-Path -LiteralPath $MarketplacesFile) {
        & $Script:FerryPy -c $jsonDrop $MarketplacesFile '-' 'rolling-context-marketplace' 'Removed marketplace' |
            Where-Object { $_ } | ForEach-Object { Write-Host $_ }
    }

    # Clean settings.json: ANTHROPIC_BASE_URL ONLY if Ferry wrote it (restoring
    # the mind's own route from preferences "upstream", if declared), then
    # Ferry's keys.
    $r = Invoke-FerryConf @('unpoint')
    $r.Output | ForEach-Object { Write-Host $_ }
    if ($r.Code -ne 0) { Write-Host 'WARNING: settings.json NOT cleaned (see above).' }
} else {
    Write-Host 'WARNING: Python not found, so settings.json was NOT cleaned. By hand: remove'
    Write-Host '  ANTHROPIC_BASE_URL (only if it is http://127.0.0.1:<your ferry port>) and every'
    Write-Host "  ROLLING_CONTEXT_* key from the env block of $(Join-Path $ClaudeDir 'settings.json'),"
    Write-Host '  then restart the session.'
}

Write-Host ''
Write-Host 'Uninstalled.'
