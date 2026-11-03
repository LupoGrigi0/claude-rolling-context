# Shared helpers for Ferry's Windows scripts (install.ps1, uninstall.ps1,
# hooks/start-proxy.ps1). Dot-source it; it defines functions and sets nothing
# else. Windows PowerShell 5.1 compatible (no ternary, no ??, no &&).
#
# CONFIG is not decided here. hooks/ferry_config.py is Ferry's ONE resolver on
# every OS: ~/preferences.json "ferry" (port REQUIRED, upstream, data_dir), the
# route guard, and every settings.json write (point / withdraw / unpoint, with
# the marker so only what Ferry wrote is ever removed). Python is already a
# prerequisite (the proxy is Python), so Windows calls the same file.
#
# What IS here: the three probes ferry_config.py answers from /proc or lsof,
# which Windows has neither of -- who listens on a port, is a pid a server.py,
# is a pid this user's.

$Script:FerryHooksDir = $PSScriptRoot      # this file's dir, even when dot-sourced
$Script:FerryConf = Join-Path $Script:FerryHooksDir 'ferry_config.py'
$Script:FerryPy = $null

function Get-FerryUserProfile {
    [Environment]::GetFolderPath('UserProfile')
}

# Claude Code's config dir before ferry_config.py has answered (it gives the
# same answer: CLAUDE_CONFIG_DIR, else ~\.claude).
function Get-FerryDefaultConfigDir {
    if ($env:CLAUDE_CONFIG_DIR) { return $env:CLAUDE_CONFIG_DIR }
    Join-Path (Get-FerryUserProfile) '.claude'
}

# Find a real Python 3.8+ and remember the INTERPRETER's own path, not a
# launcher: `py -3 server.py` would make py.exe the pid we start while a child
# python.exe holds the port. 3.8+ because from 3.8 Python's "~" on Windows is
# USERPROFILE (3.7 preferred HOME, which Git Bash sets) -- and "~" is where
# ferry_config.py looks for preferences.json. The Microsoft Store stub
# `python` prints nothing and exits non-zero, so it is skipped.
function Initialize-FerryPython {
    $probe = "import sys; ok = sys.version_info >= (3, 8); print((getattr(sys, '_base_executable', None) or sys.executable) if ok else '')"
    foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
        $exe = $cand[0]
        $pre = @($cand | Select-Object -Skip 1)
        if (-not (Get-Command $exe -CommandType Application -ErrorAction SilentlyContinue)) { continue }
        $eap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $real = & $exe @pre -c $probe 2>$null | Select-Object -First 1
            $code = $LASTEXITCODE
        } catch {
            $real = $null; $code = 1
        } finally {
            $ErrorActionPreference = $eap
        }
        if ($code -eq 0 -and $real -and (Test-Path -LiteralPath "$real".Trim())) {
            $Script:FerryPy = "$real".Trim()
            return $true
        }
    }
    return $false
}

# Run ferry_config.py <args>. -> @{ Code = <exit>; Output = <string[]> }
# (stdout and stderr together, as the bash side's 2>&1 captures do).
function Invoke-FerryConf {
    param([string[]]$ConfArgs)
    $eap = $ErrorActionPreference
    # 5.1 turns a native command's stderr into ErrorRecords; under 'Stop' that
    # throws. ferry_config.py's exit code is the verdict, not its stderr.
    $ErrorActionPreference = 'Continue'
    $env:PYTHONIOENCODING = 'utf-8'
    try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
    try {
        $out = & $Script:FerryPy $Script:FerryConf @ConfArgs 2>&1
        $code = $LASTEXITCODE
    } catch {
        $out = @("$_"); $code = 1
    } finally {
        $ErrorActionPreference = $eap
    }
    @{ Code = $code; Output = @($out | ForEach-Object { "$_" }) }
}

# Undo shlex.quote: '' -> empty; '...' with '"'"' for each embedded quote.
function ConvertFrom-FerryShQuoted {
    param([string]$Value)
    if ($Value -eq "''") { return '' }
    if ($Value.Length -ge 2 -and $Value.StartsWith("'") -and $Value.EndsWith("'")) {
        $embedded = "'" + '"' + "'" + '"' + "'"
        return $Value.Substring(1, $Value.Length - 2).Replace($embedded, "'")
    }
    return $Value
}

# `ferry_config.py env` -> hashtable of VAR -> value, or $null if it failed to
# run. FERRY_CONFIG_ERROR non-empty means the config is unusable (no port, bad
# upstream, or the route guard refused).
function Get-FerryEnv {
    $r = Invoke-FerryConf @('env')
    if ($r.Code -ne 0) { return $null }
    $vals = @{}
    $name = $null
    foreach ($line in $r.Output) {
        if ($line -match '^export ([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $name = $Matches[1]
            $vals[$name] = $Matches[2]
        } elseif ($name) {
            $vals[$name] += "`n" + $line      # a quoted value spanning lines
        }
    }
    $out = @{}
    foreach ($k in $vals.Keys) { $out[$k] = ConvertFrom-FerryShQuoted $vals[$k] }
    if (-not $out.ContainsKey('FERRY_CONFIG_DIR')) { return $null }
    return $out
}

# PIDs listening on TCP PORT (any user). Get-NetTCPConnection where it exists;
# else netstat, matched on the 0.0.0.0:0 / [::]:0 foreign address because the
# LISTENING column is localized.
function Get-FerryListenerPids {
    param([int]$Port)
    $found = @()
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        $found = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
                   ForEach-Object { [int]$_.OwningProcess })
    } else {
        foreach ($line in (& netstat -ano 2>$null)) {
            if ($line -match '^\s*TCP\s+\S+:(\d+)\s+(0\.0\.0\.0|\[::\]):0\s+\S+\s+(\d+)\s*$' -and
                [int]$Matches[1] -eq $Port) {
                $found += [int]$Matches[3]
            }
        }
    }
    @($found | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
}

function Get-FerryProcess {
    param([int]$ProcId)
    Get-CimInstance Win32_Process -Filter "ProcessId=$ProcId" -ErrorAction SilentlyContinue
}

# Is this Win32_Process this user's? (GetOwner fails -> not ours.)
function Test-FerrySameUser {
    param($Proc)
    if (-not $Proc) { return $false }
    $o = Invoke-CimMethod -InputObject $Proc -MethodName GetOwner -ErrorAction SilentlyContinue
    if (-not $o -or $o.ReturnValue -ne 0) { return $false }
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    return ("$($o.Domain)\$($o.User)" -eq $me)
}

# Is this Win32_Process a server.py? (Same test as ferry_config.py is-proxy:
# an argument whose basename is server.py.)
function Test-FerryIsProxy {
    param($Proc)
    if (-not $Proc -or -not $Proc.CommandLine) { return $false }
    return ($Proc.CommandLine -match '(^|[\s"\\/])server\.py("|\s|$)')
}

function Test-FerryHoldsPort {
    param([int]$ProcId, [int]$Port)
    return ((Get-FerryListenerPids $Port) -contains $ProcId)
}

# The PID listening on PORT -- only if it is THIS user's process (the only kind
# the hook may reuse or uninstall may stop). Mirrors ferry_config.py who-holds.
function Get-FerryPortHolder {
    param([int]$Port)
    $first = Get-FerryListenerPids $Port | Select-Object -First 1
    if ($first -and (Test-FerrySameUser (Get-FerryProcess $first))) { return $first }
    return $null
}

# Remove a plugin link WITHOUT following it. Windows PowerShell 5.1's
# `Remove-Item -Recurse` on a junction can recurse into the target -- here,
# the Ferry repo itself. A reparse point is removed as a link; only a real
# directory (an old copy) is removed recursively.
function Remove-FerryLink {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return $false }
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        if ($item.PSIsContainer) { [IO.Directory]::Delete($Path, $false) }
        else { [IO.File]::Delete($Path) }
    } else {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    return $true
}

# ferry-fetch shims install.ps1 writes (symlinks need Developer Mode on
# Windows, so these stand in for install.sh's ~/.local/bin link). The marker
# lets uninstall remove only shims Ferry wrote, for THIS repo.
$Script:FerryShimMarker = 'ferry-fetch shim written by Ferry install.ps1'
function Get-FerryShimPaths {
    $bin = Join-Path (Get-FerryUserProfile) '.local\bin'
    @{ Dir = $bin; Cmd = (Join-Path $bin 'ferry-fetch.cmd'); Sh = (Join-Path $bin 'ferry-fetch') }
}
