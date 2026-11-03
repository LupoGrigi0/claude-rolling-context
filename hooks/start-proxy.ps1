# Ensure THIS mind's Ferry proxy is running, on THIS mind's port, AS THIS USER.
# Windows twin of hooks/start-proxy.sh. Claude Code's SessionStart hook runs it
# (hooks/hooks.json tries this first, then start-proxy.sh); install.ps1 runs it
# too. No service, no admin, nothing outside the user's profile.
#
# Config: %USERPROFILE%\preferences.json "ferry" via hooks/ferry_config.py --
# the only source. Port REQUIRED; upstream defaults to https://api.anthropic.com.
#   1. The declared port is already held by a server.py of this user -> reuse it.
#   2. Otherwise start one, and VERIFY the process we launched holds the port.
#   Only then point settings.json's ANTHROPIC_BASE_URL at it. On failure, take
#   back a base URL Ferry wrote (never anyone else's) and fail loud.

$ErrorActionPreference = 'Continue'
# pwsh on Linux/macOS: not ours -- exit non-zero so hooks.json falls through to
# start-proxy.sh, which owns those platforms.
if ($PSVersionTable.PSEdition -eq 'Core' -and -not $IsWindows) { exit 3 }

$ScriptDir = $PSScriptRoot
. (Join-Path $ScriptDir 'ferry_common.ps1')
$ProxyDir = Join-Path (Split-Path -Parent $ScriptDir) 'proxy'
$Script:HookLog = Join-Path (Get-FerryDefaultConfigDir) 'rolling-context-hook.log'

function Write-FerryLog([string]$Msg) {
    try {
        Add-Content -LiteralPath $Script:HookLog -Encoding UTF8 `
            -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $Msg" -ErrorAction Stop
    } catch { }
}

# Loud: the hook log, stdout (SessionStart adds it to the mind's context) and
# stderr, then non-zero.
function Stop-FerryHook([string]$Msg) {
    Write-FerryLog "ERROR: $Msg"
    Write-Output "[FERRY] ERROR: $Msg"
    [Console]::Error.WriteLine("[FERRY] ERROR: $Msg")
    exit 1
}

# No proxy of ours holds the port: an earlier run's pointer would send the NEXT
# session to a dead port or someone else's proxy. Take it back (only if Ferry
# wrote it), then fail loud.
function Stop-FerryHookWithdraw([string]$Msg) {
    $w = (Invoke-FerryConf @('withdraw')).Output -join ' '
    if (-not $w) { $w = 'no Ferry pointer to withdraw' }
    Stop-FerryHook "$Msg settings.json: $w"
}

function Set-FerryPointer {
    $r = Invoke-FerryConf @('point')
    if ($r.Code -ne 0) { Stop-FerryHook "settings.json NOT pointed at port ${Port}: $($r.Output -join ' ')" }
    Write-FerryLog "settings.json: $($r.Output -join ' ') (ANTHROPIC_BASE_URL=http://127.0.0.1:$Port)"
}

if (-not (Initialize-FerryPython)) {
    Stop-FerryHook 'Python 3.8+ not found (tried: py -3, python, python3). Install Python, then start a new session.'
}

# The values below are the TRANSPORT to the proxy we start; they replace
# whatever this process inherited (e.g. settings.json env Claude Code injected).
$cfg = Get-FerryEnv
if ($null -eq $cfg) { Stop-FerryHook 'ferry_config.py failed to run' }
foreach ($k in $cfg.Keys) {
    [Environment]::SetEnvironmentVariable($k, $cfg[$k], 'Process')   # '' unsets
}
# A config error must not leave an earlier run's pointer aimed at a port no
# proxy will hold: take it back (only if Ferry wrote it).
if ($cfg['FERRY_CONFIG_ERROR']) { Stop-FerryHookWithdraw $cfg['FERRY_CONFIG_ERROR'] }

$ClaudeDir = $cfg['FERRY_CONFIG_DIR']
New-Item -ItemType Directory -Path $ClaudeDir -Force -ErrorAction SilentlyContinue | Out-Null
$Script:HookLog = Join-Path $ClaudeDir 'rolling-context-hook.log'
$PidFile = Join-Path $ClaudeDir 'rolling-context-proxy.pid'
$ProxyLog = Join-Path $ClaudeDir 'rolling-context-proxy.log'
$Port = [int]$cfg['ROLLING_CONTEXT_PORT']

Write-FerryLog "Hook started. PORT=$Port UPSTREAM=$($cfg['ROLLING_CONTEXT_UPSTREAM']) FERRY_DATA=$($cfg['FERRY_DATA'])"

# 1. Reuse: whoever of THIS user holds the port, else the pidfile's pid --
# either way it must be this user's server.py, holding the declared port.
$cands = @()
$holder = Get-FerryPortHolder $Port
if ($holder) { $cands += $holder }
if (Test-Path -LiteralPath $PidFile) {
    $saved = "$(Get-Content -LiteralPath $PidFile -Raw -ErrorAction SilentlyContinue)".Trim()
    if ($saved -match '^\d+$') { $cands += [int]$saved }
}
foreach ($c in $cands) {
    $info = Get-FerryProcess $c
    if ($info -and (Test-FerrySameUser $info) -and (Test-FerryIsProxy $info) -and (Test-FerryHoldsPort $c $Port)) {
        # Ours -- but it keeps the upstream it was STARTED with. If preferences
        # changed since, reusing it would make it a second source of the route.
        $m = Invoke-FerryConf @('proxy-matches', "$Port")
        if ($m.Code -ne 0) {
            Stop-FerryHookWithdraw "proxy PID $c on port $Port does not match preferences.json: $($m.Output -join ' '). Stop that PID (docs/RUNBOOK.md section 3) and start a new session."
        }
        Set-Content -LiteralPath $PidFile -Value $c -NoNewline
        Write-FerryLog "Reusing proxy PID $c on port $Port"
        Set-FerryPointer
        exit 0
    }
}
Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue

# 2. Start one, as this user, hidden. The interpreter itself (not a launcher),
# so the pid we get is normally the one that binds.
Write-FerryLog "Starting proxy on port $Port..."
try {
    $proc = Start-Process -FilePath $Script:FerryPy -ArgumentList 'server.py' `
        -WorkingDirectory $ProxyDir `
        -RedirectStandardOutput $ProxyLog -RedirectStandardError "$ProxyLog.err" `
        -WindowStyle Hidden -PassThru -ErrorAction Stop
} catch {
    Stop-FerryHookWithdraw "could not start $($Script:FerryPy) server.py: $_."
}

# Verify: the process WE started (or a child it spawned, if the interpreter
# path was a redirector) must hold the port. Up to ~15s.
$deadline = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    foreach ($l in (Get-FerryListenerPids $Port)) {
        $ok = ($l -eq $proc.Id)
        if (-not $ok) {
            $li = Get-FerryProcess $l
            $ok = ($li -and [int]$li.ParentProcessId -eq $proc.Id)
        }
        if ($ok) {
            Set-Content -LiteralPath $PidFile -Value $l -NoNewline
            Write-FerryLog "Proxy started with PID $l, verified on port $Port"
            Set-FerryPointer
            exit 0
        }
    }
    if ($proc.HasExited) { break }
    Start-Sleep -Milliseconds 250
}

$held = (Get-FerryListenerPids $Port) -join ','
if (-not $proc.HasExited) {
    # The process we just started (and its children), never another.
    & taskkill.exe /PID $proc.Id /T /F 2>&1 | Out-Null
    if (-not $proc.HasExited) { try { $proc.Kill() } catch { } }
}
$tail = @()
foreach ($f in @($ProxyLog, "$ProxyLog.err")) {
    $tail += @(Get-Content -LiteralPath $f -Tail 3 -ErrorAction SilentlyContinue)
}
$heldMsg = ''
if ($held) { $heldMsg = " (held by PID $held)" }
Stop-FerryHookWithdraw "proxy PID $($proc.Id) did not take port $Port$heldMsg. Log: $($tail -join ' ')."
