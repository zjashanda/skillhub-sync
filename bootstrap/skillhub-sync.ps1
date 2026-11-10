<#
.SYNOPSIS
    Bootstrap skill_sync.py from the skillhub-sync repository and run it.

.DESCRIPTION
    Fetches the skillhub-sync source over HTTPS (GitHub tarball API) so the tool
    still works on networks where cloning github.com is blocked, then runs
    skill_sync.py with the requested mode.

    An access token is read from the named environment variable and used only in
    an HTTP header. It is never written to disk and never echoed.

    Exit codes: 0 ok, 2 attention needed, 3 config error, 4 apply failure
    (2-4 come from skill_sync.py; this wrapper emits 3 itself).

.PARAMETER Profile
    Manifest profile to sync. Required unless -Check is used.

.PARAMETER Yes
    Allow 'apply' to write changes. Without it apply is a dry run.

.PARAMETER Check
    Print the resolved environment and exit 0.

.EXAMPLE
    .\skillhub-sync.ps1 -Profile ark -Mode plan

.EXAMPLE
    .\skillhub-sync.ps1 -Check
#>
[CmdletBinding()]
param(
    [string]$RepoUrl = 'https://github.com/zjashanda/skillhub-sync.git',

    [string]$Ref = 'main',

    [string]$Dest = "$env:LOCALAPPDATA\ark\skillhub-sync",

    [string]$Profile,

    [string]$Manifest,

    [ValidateSet('status', 'plan', 'apply', 'verify')]
    [string]$Mode = 'plan',

    [string]$InstallRoot,

    [ValidateNotNullOrEmpty()]
    [string]$TokenEnv = 'GITHUB_TOKEN',

    [ValidateSet('auto', 'http', 'git')]
    [string]$Via = 'auto',

    [switch]$Force,

    [switch]$Check,

    [switch]$Yes,

    [ValidateNotNullOrEmpty()]
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function Write-Info {
    param([string]$Message)
    Write-Host $Message
}

function Fail {
    param([string]$Message)
    Write-Host "skillhub-sync: $Message" -ForegroundColor Red
    exit 3
}

# Older PowerShell hosts default to TLS 1.0/1.1, which GitHub rejects.
try {
    $current = [System.Net.ServicePointManager]::SecurityProtocol
    [System.Net.ServicePointManager]::SecurityProtocol = $current -bor [System.Net.SecurityProtocolType]::Tls12
}
catch {
    Write-Verbose "Could not raise TLS to 1.2: $($_.Exception.Message)"
}

# %LOCALAPPDATA% is always set for an interactive user, but a service context
# may not have it. Fall back only when the caller did not supply -Dest.
if (-not $PSBoundParameters.ContainsKey('Dest')) {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        $Dest = Join-Path $env:LOCALAPPDATA 'ark\skillhub-sync'
    }
    elseif (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
        $Dest = Join-Path (Join-Path $env:USERPROFILE 'AppData\Local') 'ark\skillhub-sync'
    }
    else {
        $Dest = Join-Path ([System.IO.Path]::GetTempPath()) 'ark\skillhub-sync'
    }
}

$token = [Environment]::GetEnvironmentVariable($TokenEnv)
$tokenPresent = -not [string]::IsNullOrEmpty($token)
$tokenState = if ($tokenPresent) { 'present' } else { 'absent' }

# Resolve the interpreter once: honour -Python, then fall back to the usual
# names so this also works under pwsh on Linux/macOS.
function Resolve-Python {
    param([string]$Requested)
    foreach ($candidate in @($Requested, 'python3', 'py')) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        if (Get-Command $candidate -ErrorAction SilentlyContinue) { return $candidate }
    }
    return $null
}

$Python = Resolve-Python -Requested $Python

if ($Check) {
    if (-not $Python) { Fail "python not found: $($PSBoundParameters['Python'])" }
    $pythonVersion = (& $Python --version 2>&1 | Out-String).Trim()

    Write-Info 'skillhub-sync bootstrap check'
    Write-Info "  python:   $Python ($pythonVersion)"
    foreach ($tool in @('git', 'tar.exe', 'curl.exe')) {
        $resolved = Get-Command $tool -ErrorAction SilentlyContinue
        $label = "$tool" + ':'
        if ($resolved) {
            Write-Info ("  {0,-9} {1}" -f $label, $resolved.Source)
        }
        else {
            Write-Info ("  {0,-9} {1}" -f $label, 'missing')
        }
    }
    Write-Info "  dest:     $Dest"
    Write-Info "  ref:      $Ref"
    Write-Info "  mode:     $Mode"
    Write-Info "  via:      $Via"
    $manifestDisplay = if ($Manifest) { $Manifest } else { Join-Path $Dest 'manifests\canonical.json' }
    Write-Info "  manifest: $manifestDisplay"
    Write-Info "  token:    $TokenEnv=$tokenState"
    exit 0
}

if (-not $Profile) { Fail '-Profile is required (except with -Check)' }

if (-not $Python) { Fail 'python not found (tried -Python value, python3, py)' }

if (-not $Manifest) {
    $Manifest = Join-Path $Dest 'manifests\canonical.json'
}

$scriptPath = Join-Path $Dest 'skill_sync.py'
$needsFetch = $Force.IsPresent -or -not (Test-Path -LiteralPath $scriptPath)

# Never destroy an existing git checkout that carries uncommitted work.
function Assert-Removable {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath (Join-Path $Path '.git'))) { return }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Fail "refusing to replace ${Path}: it is a git checkout and git is unavailable to verify it is clean"
    }
    $dirty = @(& git -C $Path status --porcelain 2>$null)
    if ($dirty.Count -gt 0) {
        Fail "refusing to replace ${Path}: it has local modifications (commit or stash them first)"
    }
}

function Move-IntoPlace {
    param([string]$Source)

    if (-not (Test-Path -LiteralPath (Join-Path $Source 'skill_sync.py'))) {
        Fail "downloaded source has no skill_sync.py: $Source"
    }
    if (Test-Path -LiteralPath $Dest) {
        Assert-Removable -Path $Dest
        Remove-Item -LiteralPath $Dest -Recurse -Force
    }
    $parent = Split-Path -Parent $Dest
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    Move-Item -LiteralPath $Source -Destination $Dest -Force
}

function Get-GitHubSlug {
    param([string]$Url)

    if ($Url -notmatch '^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$') { return $null }
    return "$($Matches[1])/$($Matches[2])"
}

# Fail before downloading anything if -Force would clobber local work.
if ($Force.IsPresent -and (Test-Path -LiteralPath $Dest)) {
    Assert-Removable -Path $Dest
}

Write-Info 'skillhub-sync bootstrap'
Write-Info "  dest:    $Dest"
Write-Info "  ref:     $Ref"
Write-Info "  mode:    $Mode"
Write-Info "  via:     $Via"
Write-Info "  token:   $TokenEnv=$tokenState"

if ($needsFetch) {
    $tmp = Join-Path $env:TEMP ('skillhub-sync-' + [guid]::NewGuid().ToString('N'))
    try {
        New-Item -ItemType Directory -Path $tmp -Force | Out-Null

        $isLocal = $RepoUrl -match '^(file://|/|[A-Za-z]:[\\/]|\.{1,2}[\\/])'
        $slug = Get-GitHubSlug -Url $RepoUrl
        $useHttp = $false

        if ($isLocal) {
            if ($Via -eq 'http') { Fail '--Via http cannot fetch a local path; use auto or git' }
        }
        elseif ($Via -eq 'git') {
            $useHttp = $false
        }
        elseif ($Via -eq 'http') {
            if (-not $slug) { Fail "cannot derive <owner>/<repo> from -RepoUrl '$RepoUrl'; use -Via git" }
            $useHttp = $true
        }
        else {
            if (-not $slug) {
                Fail "cannot derive <owner>/<repo> from -RepoUrl '$RepoUrl'; use -Via git"
            }
            $useHttp = $true
        }

        $source = $null

        if ($useHttp) {
            $url = "https://api.github.com/repos/$slug/tarball/$Ref"
            $tgz = Join-Path $tmp 'source.tar.gz'
            $headers = @{}
            if ($tokenPresent) { $headers['Authorization'] = "Bearer $token" }

            Write-Info "  fetching $url"
            $httpError = $null
            try {
                Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tgz `
                    -Headers $headers -UserAgent 'ark-skillhub-sync' | Out-Null
            }
            catch {
                $httpError = $_.Exception.Message
            }

            if (-not $httpError) {
                $extract = Join-Path $tmp 'x'
                New-Item -ItemType Directory -Path $extract -Force | Out-Null
                $tar = Get-Command tar.exe -ErrorAction SilentlyContinue

                if ($tar) {
                    & $tar.Source -xzf $tgz -C $extract
                    if ($LASTEXITCODE -ne 0) {
                        Fail "tar.exe failed to extract the archive (exit $LASTEXITCODE)"
                    }
                }
                else {
                    try {
                        Add-Type -AssemblyName System.IO.Compression.FileSystem
                        [System.IO.Compression.ZipFile]::ExtractToDirectory($tgz, $extract)
                    }
                    catch {
                        Fail ('extraction failed: tar.exe is missing and the download is not a ZIP archive. ' +
                            'Install tar (Windows 10 1803+) or rerun with -Via git.')
                    }
                }

                $children = @(Get-ChildItem -LiteralPath $extract -Force)
                if ($children.Count -ne 1 -or -not $children[0].PSIsContainer) {
                    Fail "unexpected archive layout: want one top-level directory under $extract"
                }
                $source = $children[0].FullName
            }
            elseif ($Via -eq 'http') {
                Fail "HTTPS tarball download failed: $httpError (retry with -Via git)"
            }
            else {
                Write-Info "  HTTPS tarball failed; falling back to git clone"
                $useHttp = $false
            }
        }

        if (-not $useHttp) {
            if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
                Fail 'git is required for the git transport but was not found'
            }
            $cloneDest = Join-Path $tmp 'clone'
            Write-Info "  cloning $RepoUrl ($Ref)"
            & git clone --depth 1 --branch $Ref -- $RepoUrl $cloneDest
            if ($LASTEXITCODE -ne 0) {
                Fail "git clone failed (exit $LASTEXITCODE)"
            }
            $source = $cloneDest
        }

        Move-IntoPlace -Source $source
    }
    finally {
        if ($tmp -and (Test-Path -LiteralPath $tmp)) {
            Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}
else {
    Write-Info '  using existing checkout'
}

if (-not (Test-Path -LiteralPath $scriptPath)) {
    Fail "bootstrap incomplete: $scriptPath is missing"
}

# $args is an automatic variable, so the forwarded argument list gets its own name.
$syncArgs = @($scriptPath, '--manifest', $Manifest, '--profile', $Profile, '--transport', $Via, $Mode)
if ($InstallRoot) { $syncArgs += @('--install-root', $InstallRoot) }
if ($Mode -eq 'apply' -and $Yes.IsPresent) { $syncArgs += '--yes' }

Write-Info "  running: $Python $scriptPath ..."
& $Python @syncArgs
exit $LASTEXITCODE
