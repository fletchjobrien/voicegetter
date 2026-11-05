# VoiceGetter installer for Windows. Start it with INSTALL-WINDOWS.bat (double-click).
#
# What it does:
#   1. Installs Python 3.12 (only for this user) if it is not installed.
#   2. Copies VoiceGetter to %LOCALAPPDATA%\VoiceGetter and installs it (GPU build of PyTorch if an NVIDIA GPU exists).
#   3. Downloads the AI models.
#   4. Allows the phone to connect (Windows Firewall rule; asks for admin once).
#   5. Starts VoiceGetter now and every time you sign in to Windows (hidden, no window).
#   6. Optional: Tailscale, so the phone link also works away from home.
#   7. Opens a page with the phone link and a QR code (also saved on the desktop).
#
# Run again to update. Uninstall: UNINSTALL-WINDOWS.bat

param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Src      = Split-Path -Parent $PSScriptRoot
$Dest     = Join-Path $env:LOCALAPPDATA 'VoiceGetter'
$Port     = 8000
$Startup  = [Environment]::GetFolderPath('Startup')
$Launcher = Join-Path $Startup 'VoiceGetter.vbs'
$Desktop  = [Environment]::GetFolderPath('Desktop')
$LinkPage = Join-Path $Desktop 'VoiceGetter phone link.html'
$Venv     = Join-Path $Dest '.venv'
$VPy      = Join-Path $Venv 'Scripts\python.exe'
$VPyw     = Join-Path $Venv 'Scripts\pythonw.exe'
$FwRule   = 'VoiceGetter'

function Step($msg) { Write-Host ''; Write-Host "==> $msg" -ForegroundColor Cyan }
function Info($msg) { Write-Host "    $msg" }
function Warn($msg) { Write-Host "    WARNING: $msg" -ForegroundColor Yellow }
function Fail($msg) {
    Write-Host ''
    Write-Host "ERROR: $msg" -ForegroundColor Red
    Write-Host 'Take a screenshot of this window and send it to whoever set this up for you.' -ForegroundColor Red
    exit 1
}
# Run a program and stop if it fails (PowerShell 5.1 does not stop on native exit codes).
function Run([string]$What, [string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Fail "$What failed (exit code $LASTEXITCODE)." }
}
function Ask([string]$Question) {
    $answer = Read-Host "$Question [Y/n]"
    return ($answer -eq '' -or $answer -match '^(y|yes)$')
}
function Stop-VoiceGetter {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($Venv) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
function Run-Admin([string]$Command) {
    # Runs one PowerShell command as administrator (Windows asks for permission once).
    # -EncodedCommand avoids all quoting problems.
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))
    try {
        $p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
            -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded)
        return ($p.ExitCode -eq 0)
    } catch {
        return $false
    }
}

# ------------------------------------------------------------------ uninstall
if ($Uninstall) {
    Step 'Removing VoiceGetter'
    Stop-VoiceGetter
    Remove-Item $Launcher -ErrorAction SilentlyContinue
    Remove-Item $LinkPage -ErrorAction SilentlyContinue
    [void](Run-Admin "netsh advfirewall firewall delete rule name=`"$FwRule`"")
    Start-Sleep -Seconds 2
    Remove-Item $Dest -Recurse -Force -ErrorAction SilentlyContinue
    Info 'Done. (Python and Tailscale stay installed. Remove them in Settings > Apps if you want.)'
    exit 0
}

Write-Host ''
Write-Host '  VoiceGetter installer' -ForegroundColor Magenta
Write-Host '  This takes about 10-20 minutes the first time (it downloads about 3 GB).'

if (-not (Test-Path (Join-Path $Src 'app\main.py'))) {
    Fail "Cannot find the VoiceGetter files next to this installer ($Src). Extract the whole ZIP first, then run INSTALL-WINDOWS.bat from the extracted folder."
}

# ------------------------------------------------------------------ 1. Python
Step 'Checking Python'
function Find-Python {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe')
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    foreach ($v in @('3.12', '3.13', '3.11', '3.10')) {
        try {
            $exe = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
        } catch { }
    }
    return $null
}
$Py = Find-Python
if (-not $Py) {
    Info 'Python is not installed. Downloading Python 3.12...'
    $installer = Join-Path $env:TEMP 'python-3.12.10-amd64.exe'
    try {
        Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $installer -UseBasicParsing
    } catch {
        Fail "Could not download Python. Check the internet connection. ($_)"
    }
    Info 'Installing Python 3.12 (only for this user)...'
    $p = Start-Process $installer -Wait -PassThru -ArgumentList @('/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=1', 'Include_test=0')
    if ($p.ExitCode -ne 0) { Fail "The Python installer failed (exit code $($p.ExitCode))." }
    $Py = Find-Python
    if (-not $Py) { Fail 'Python was installed, but the installer cannot find it. Restart the PC and run the installer again.' }
}
Info "Python: $Py"

# ------------------------------------------------------------------ 2. Copy + install
Step 'Copying VoiceGetter'
Stop-VoiceGetter
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
foreach ($item in @('app', 'cli.py', 'requirements.txt', 'windows\phone_link.py')) {
    $from = Join-Path $Src $item
    $to = Join-Path $Dest (Split-Path $item -Leaf)
    if (Test-Path $to) { Remove-Item $to -Recurse -Force }
    Copy-Item $from $to -Recurse -Force
}
Get-ChildItem $Dest -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
Info "Installed to $Dest"

Step 'Installing (the slow part: about 3 GB of downloads)'
if (-not (Test-Path $VPy)) { Run 'Creating the Python environment' $Py @('-m', 'venv', $Venv) }
Run 'Updating pip' $VPy @('-m', 'pip', 'install', '--upgrade', 'pip', '--quiet')

$HasNvidia = [bool](Get-CimInstance Win32_VideoController | Where-Object { $_.Name -match 'NVIDIA' })
if ($HasNvidia) {
    Info 'NVIDIA GPU found: installing the GPU version of PyTorch (3.5 GB)...'
    Info 'After the download reaches 100%, the window shows nothing for 5-15 minutes while it unpacks.'
    Info 'That is normal. Do not close this window.'
    Run 'Installing PyTorch (GPU)' $VPy @('-m', 'pip', 'install', 'torch==2.8.0', 'torchaudio==2.8.0',
        '--index-url', 'https://download.pytorch.org/whl/cu128')
} else {
    Warn 'No NVIDIA GPU found. VoiceGetter will work, but slowly.'
}
Run 'Installing VoiceGetter packages' $VPy @('-m', 'pip', 'install', '-r', (Join-Path $Dest 'requirements.txt'), 'qrcode')

if ($HasNvidia) {
    $cuda = ''
    try { $cuda = & $VPy -c "import torch; print(torch.cuda.is_available())" 2>$null } catch { }
    if ("$cuda".Trim() -eq 'True') {
        Info 'GPU is ready.'
    } else {
        Warn 'PyTorch cannot use the GPU. Update the NVIDIA driver (GeForce Experience or nvidia.com/drivers), then run this installer again.'
    }
}

# ------------------------------------------------------------------ 3. Models
Step 'Downloading the AI models (about 400 MB)'
Push-Location $Dest
try {
    Run 'Downloading models' $VPy @('-c', "from app import pipeline as P; P.get_demucs('cpu'); P.get_encoder('cpu'); P.get_vad(); print('    Models ready.')")
} finally {
    Pop-Location
}

# ------------------------------------------------------------------ 4. Firewall + sleep
Step 'Letting your phone connect (Windows asks for permission)'
$fwCmd = "netsh advfirewall firewall delete rule name=`"$FwRule`" | Out-Null; " +
         "netsh advfirewall firewall add rule name=`"$FwRule`" dir=in action=allow protocol=TCP localport=$Port profile=any | Out-Null"
if (Run-Admin $fwCmd) {
    Info 'Firewall rule added.'
} else {
    Warn 'No firewall rule. If the phone cannot open the link, run this installer again and click Yes when Windows asks.'
}

if (Ask '    Keep this PC from sleeping while it is plugged in? (needed to use it from the phone)') {
    if (Run-Admin 'powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0') {
        Info 'Sleep is off while plugged in. (The screen still turns off.)'
    } else {
        Warn 'Could not change the sleep setting.'
    }
}

# ------------------------------------------------------------------ 5. Autostart
Step 'Starting VoiceGetter (and adding it to Windows startup)'
$q = '""'   # a literal double quote inside a VBScript string
$vbs = @"
' Starts VoiceGetter hidden when you sign in to Windows. Delete this file to stop that.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$Dest"
sh.Environment("PROCESS")("VOICEGETTER_LOG") = "$Dest\server.log"
sh.Environment("PROCESS")("VOICEGETTER_DATA") = "$Dest\data"
sh.Environment("PROCESS")("VOICEGETTER_MODELS") = "$Dest\models"
sh.Run "$q$VPyw$q -m app.main", 0, False
"@
Set-Content -Path $Launcher -Value $vbs -Encoding Unicode   # UTF-16: works with any user name
Start-Process wscript.exe -ArgumentList "`"$Launcher`""

# Check the port directly on 127.0.0.1. (Invoke-WebRequest to "localhost" can try IPv6 or a proxy first and fail.)
$LogFile = Join-Path $Dest 'server.log'
$up = $false
for ($i = 0; $i -lt 120; $i++) {
    Start-Sleep -Seconds 1
    $tcp = New-Object Net.Sockets.TcpClient
    try {
        $tcp.Connect('127.0.0.1', $Port)
        if ($tcp.Connected) { $up = $true }
    } catch { } finally { $tcp.Close() }
    if (-not $up -and (Test-Path $LogFile)) {
        if (Select-String -Path $LogFile -Pattern 'VoiceGetter is running' -Quiet) { $up = $true }
    }
    if ($up) { break }
}
if ($up) {
    Info "VoiceGetter is running: http://localhost:$Port"
} else {
    Write-Host ''
    Get-Content (Join-Path $Dest 'server.log') -Tail 30 -ErrorAction SilentlyContinue
    Fail 'VoiceGetter did not start. See the log above.'
}

# ------------------------------------------------------------------ 6. Tailscale (optional)
$TsName = ''
Step 'Use it away from home?'
Info 'At home, your phone only needs the same Wi-Fi. For use anywhere, VoiceGetter uses Tailscale'
Info '(free; you install the Tailscale app on this PC and on your phone, and sign in with the same account).'
if (Ask '    Set up Tailscale now?') {
    $ts = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (-not (Test-Path $ts)) {
        Info 'Installing Tailscale (Windows may ask for permission)...'
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            try {
                & winget install -e --id Tailscale.Tailscale --accept-package-agreements --accept-source-agreements --silent
            } catch { }
        }
        if (-not (Test-Path $ts)) {
            Info 'Opening the Tailscale download page. Install Tailscale, then come back to this window.'
            Start-Process 'https://tailscale.com/download/windows'
            [void](Read-Host '    Press Enter after Tailscale is installed')
        }
    }
    if (Test-Path $ts) {
        $gui = Join-Path $env:ProgramFiles 'Tailscale\tailscale-ipn.exe'
        if (Test-Path $gui) { Start-Process $gui }
        Info 'Sign in to Tailscale in the window or browser that opens (or click the Tailscale icon near the clock > Log in).'
        Info 'Waiting for you to sign in (up to 5 minutes)...'
        for ($i = 0; $i -lt 100; $i++) {
            try {
                $status = (& $ts status --json 2>$null) | Out-String | ConvertFrom-Json
                if ($status.BackendState -eq 'Running' -and $status.Self.DNSName) {
                    $TsName = $status.Self.DNSName.TrimEnd('.')
                    break
                }
            } catch { }
            Start-Sleep -Seconds 3
        }
        if ($TsName) {
            Info "Tailscale is ready: $TsName"
            Info 'On your phone: install the Tailscale app and sign in with the same account.'
        } else {
            Warn 'Tailscale is not signed in yet. Sign in, then run this installer again to get the "anywhere" link.'
        }
    }
}

# ------------------------------------------------------------------ 7. Phone link
Step 'Your phone link'
$linkArgs = @((Join-Path $Dest 'phone_link.py'), $LinkPage, '--port', "$Port")
if ($TsName) { $linkArgs += @('--tailscale', $TsName) }
& $VPy @linkArgs

Write-Host ''
Write-Host '  Done! A page with the link and a QR code is open in your browser.' -ForegroundColor Green
Write-Host '  Scan the QR code with your phone camera. The page is also on your desktop.' -ForegroundColor Green
Write-Host '  VoiceGetter starts by itself every time you sign in to Windows.' -ForegroundColor Green
