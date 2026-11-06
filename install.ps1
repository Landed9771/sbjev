<#
.SYNOPSIS
  Sets up sbjev on Windows: Python packages, optional tools, config.json, start-at-login, and the
  sbjev:// link the userscript uses to start the server. Safe to run again (it only fills gaps).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1
  powershell -ExecutionPolicy Bypass -File install.ps1 -NoAudio   # skip local Whisper (no GPU needed)
#>
param(
    [switch]$NoAudio,   # don't install faster-whisper; audio edge snapping is turned off in config.json
    [switch]$Yes,       # answer "yes" to optional installs (ffmpeg, Deno) without asking
    [switch]$NoPip      # skip pip entirely (you manage the Python packages yourself)
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }
function Ask($question) {
    if ($Yes) { return $true }
    $a = Read-Host "$question [Y/n]"
    return ($a -eq '' -or $a -match '^[yY]')
}
function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

Say "== sbjev installer ==" 'Cyan'

# --- 1. Python 3.10+ -----------------------------------------------------------------------------
$python = $null
foreach ($candidate in @(@('python'), @('py', '-3'), @('python3'))) {
    if (-not (Have $candidate[0])) { continue }
    try {
        $exe = & $candidate[0] $candidate[1..9] -c "import sys; print(sys.executable if sys.version_info >= (3, 10) else '')" 2>$null
        if ($exe) { $python = $exe.Trim(); break }
    } catch { }
}
if (-not $python) {
    Say "Python 3.10 or newer is required. Install it from https://www.python.org/downloads/ (tick 'Add to PATH'), then run this again." 'Red'
    exit 1
}
Say "Python: $python"
Set-Content -Path (Join-Path $here 'python.txt') -Value $python -Encoding ASCII

# --- 2. Python packages --------------------------------------------------------------------------
$req = Join-Path $here 'requirements.txt'
if ($NoPip) {
    Say "Skipping pip (-NoPip)."
} else {
    Say "Installing Python packages (requests, numpy, yt-dlp)..."
    & $python -m pip install --upgrade --quiet -r $req
    if ($LASTEXITCODE -ne 0) { Say "pip failed; see the messages above." 'Red'; exit 1 }
    if (-not $NoAudio) {
        Say "Installing faster-whisper for audio edge snapping (uses an NVIDIA GPU if you have one, else the CPU)..."
        & $python -m pip install --upgrade --quiet faster-whisper
        if ($LASTEXITCODE -ne 0) { Say "faster-whisper failed to install; audio snapping will be turned off." 'Yellow'; $NoAudio = $true }
    }
}
if (-not $NoAudio) {
    & $python -c "import faster_whisper" 2>$null
    if ($LASTEXITCODE -ne 0) { Say "faster-whisper isn't importable; audio snapping will be turned off." 'Yellow'; $NoAudio = $true }
}

# --- 3. Optional tools ---------------------------------------------------------------------------
function Install-Tool($name, $exe, $wingetId, $why) {
    if (Have $exe) { Say "${name}: found"; return }
    Say "$name is not installed. $why" 'Yellow'
    if ((Have 'winget') -and (Ask "Install $name with winget?")) {
        winget install --id $wingetId -e --accept-source-agreements --accept-package-agreements
        Say "If $name still isn't found, open a new terminal or sign out and back in (PATH updates)." 'Yellow'
    } else {
        Say "Skipped. You can install it later: winget install $wingetId" 'Yellow'
    }
}
if (-not $NoAudio) {
    Install-Tool 'ffmpeg' 'ffmpeg' 'Gyan.FFmpeg' 'Audio snapping needs it to cut audio clips.'
}
Install-Tool 'Deno' 'deno' 'DenoLand.Deno' 'yt-dlp uses it to solve YouTube''s JavaScript challenges; without it the "confirm you''re not a bot" check shows up far more often.'

# --- 4. config.json ------------------------------------------------------------------------------
$cfgPath = Join-Path $here 'config.json'
if (-not (Test-Path $cfgPath)) {
    Copy-Item (Join-Path $here 'config.example.json') $cfgPath
    Say "Created config.json from config.example.json"
}
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
if ($NoAudio) { $cfg.audio_snapping = $false }
$k = $cfg.keys
$hasKey = $k.typesafe_api_key -or $k.ai_gateway_api_key -or ($k.cloudflare_account_id -and $k.cloudflare_api_token) `
          -or $env:TYPESAFE_API_KEY -or $env:AI_GATEWAY_API_KEY -or $env:CLOUDFLARE_API_TOKEN
if (-not $hasKey) {
    Say ""
    Say "sbjev needs access to Jev (TypeSafe's model). Pick where your key is from:" 'Cyan'
    Say "  1) Vercel AI Gateway  (vercel.com -> AI Gateway -> API Keys; needs paid credit)"
    Say "  2) TypeSafe directly  (console.typesafe.ai/keys)"
    Say "  3) Cloudflare Workers AI (account id + API token with Workers AI permission)"
    Say "  Enter) skip for now and edit config.json later"
    $choice = Read-Host "Choice"
    switch ($choice) {
        '1' { $k.ai_gateway_api_key = (Read-Host "Vercel AI Gateway API key").Trim() }
        '2' { $k.typesafe_api_key = (Read-Host "TypeSafe API key").Trim() }
        '3' { $k.cloudflare_account_id = (Read-Host "Cloudflare account ID").Trim()
              $k.cloudflare_api_token = (Read-Host "Cloudflare API token").Trim() }
        default { Say "No key set. Edit the ""keys"" section of config.json before using it." 'Yellow' }
    }
}
# write back as UTF-8 without BOM (Python's json reader and Windows PowerShell 5.1 disagree on BOMs)
$json = $cfg | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText($cfgPath, $json, (New-Object Text.UTF8Encoding $false))

# --- 5. Start at login + sbjev:// links ----------------------------------------------------------
$vbs = Join-Path $here 'start-server.vbs'
$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup 'sbjev server.lnk'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($lnk)
$s.TargetPath = "$env:WINDIR\System32\wscript.exe"
$s.Arguments = "`"$vbs`""
$s.WorkingDirectory = $here
$s.Description = 'Keeps the sbjev SponsorBlock server running'
$s.Save()
Say "Start at login: $lnk"

# per-user URL protocol (no admin): the userscript's "Start server" button opens sbjev://start
$key = 'HKCU:\Software\Classes\sbjev'
New-Item -Path "$key\shell\open\command" -Force | Out-Null
Set-ItemProperty -Path $key -Name '(default)' -Value 'URL:sbjev server launcher'
Set-ItemProperty -Path $key -Name 'URL Protocol' -Value ''
Set-ItemProperty -Path "$key\shell\open\command" -Name '(default)' -Value "`"$env:WINDIR\System32\wscript.exe`" `"$vbs`" `"%1`""
Say "Registered sbjev:// (lets the userscript start the server)"

# --- 6. Start it now -----------------------------------------------------------------------------
# through explorer so the server isn't a child of this terminal (closing it won't stop the server)
Start-Process explorer.exe -ArgumentList "`"$vbs`""
$port = if ($cfg.port) { $cfg.port } else { 8765 }
$ok = $false
for ($i = 0; $i -lt 20 -and -not $ok; $i++) {
    Start-Sleep 1
    try { $ok = (Invoke-RestMethod "http://127.0.0.1:$port/ping" -TimeoutSec 2).ok } catch { }
}
if ($ok) { Say "Server running on http://127.0.0.1:$port" 'Green' }
else { Say "The server didn't answer yet; check server.log in this folder." 'Yellow' }

Say ""
Say "Last step: install the userscript in Tampermonkey or Violentmonkey:" 'Cyan'
Say "  open sbjev.user.js from this folder (or the repo's raw link) and click Install."
Say "Then open any YouTube video and click the 'Jev SB' button next to the search box."
