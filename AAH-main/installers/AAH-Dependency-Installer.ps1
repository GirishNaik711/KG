#Requires -Version 5.1
<#
.SYNOPSIS
    AAH (Ascend Agentic Harness) Prerequisites Installer for Windows

.DESCRIPTION
    Installs and configures all prerequisites required to run AAH:
    Git, GitHub CLI (gh), Python 3.12, Node.js, uv, AWS CLI, and Claude Code.

    Then installs AAH itself from the harness source shipped ALONGSIDE this
    script. This installer lives in <harness>\installers\, so the source root
    is simply its parent folder — nothing is downloaded and no repository is
    contacted. AAH is installed as a global uv tool into uv's managed
    environment. The unzipped folder can be deleted after install.

    Install source : the folder containing this script's parent pyproject.toml
    Installed to   : %APPDATA%\uv\tools\aah (managed by uv)
    Scope          : global (links into %USERPROFILE%\.claude)

.NOTES
    Run as Administrator. Tested on Windows 10/11 with winget available.
    Edit the CONFIGURATION block below before executing (or let the script prompt you).

.EXAMPLE
    .\AAH-Dependency-Installer.ps1
    .\AAH-Dependency-Installer.ps1
#>

# ================================================================
#  AUTO-ELEVATION CHECK - Runs automatically if not admin
# ================================================================
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $IsAdmin) {
    Write-Host "`n  This script requires Administrator privileges." -ForegroundColor Yellow
    Write-Host "  Attempting to restart as Administrator...`n" -ForegroundColor Yellow

    try {
        $ScriptPath = $MyInvocation.MyCommand.Path
        Start-Process powershell.exe -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -NoExit -File `"$ScriptPath`""
        exit
    } catch {
        Write-Host "  Failed to elevate to Administrator." -ForegroundColor Red
        Write-Host "  Please right-click PowerShell and select 'Run as administrator', then re-run this script." -ForegroundColor Red
        Write-Host ""
        Read-Host "Press Enter to exit"
        exit 1
    }
}

# ================================================================
#  CONFIGURATION  -  Edit these values before running the script
# ================================================================

# --- Your Windows username (auto-detected, override if needed) ---
$Config_Username         = $env:USERNAME

# --- Python version to install (must be 3.11+) ---
$Config_PythonVersion    = "3.12"

# --- Node.js major LTS version ---
$Config_NodeMajor        = "22"

# --- Minimum free disk space required (GB) ---
$Config_MinDiskSpaceGB   = 10

# --- Set to $false to force reinstall even if tool is already installed ---
$Config_SkipIfInstalled  = $true

# --- Git installation path (adjust if using system-wide install) ---
$Config_GitInstallPath   = "C:\Users\$Config_Username\AppData\Local\Programs\Git"

# --- Python base install path (adjust if using system-wide install) ---
$Config_PythonBasePath   = "C:\Users\$Config_Username\AppData\Local\Programs\Python"

# --- uv install directory ---
$Config_UvBinPath        = "C:\Users\$Config_Username\.local\bin"

# ================================================================
#  END OF CONFIGURATION  -  Do not edit below this line
# ================================================================

# Internal flags
$Script:ClaudeAlreadyInstalled = $false
$Script:CertificateIssuesDetected = $false
$Script:AahZipSource = ""  # harness source shipped next to this script (auto-detected)
$Script:WingetUsable = $false

# ----------------------------------------------------------------
#  Logging helpers
# ----------------------------------------------------------------
$Script:Results = [System.Collections.Generic.List[PSCustomObject]]::new()

function Write-Header {
    param([string]$Text)
    $line = "=" * 65
    Write-Host "`n$line" -ForegroundColor Cyan
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host "$line" -ForegroundColor Cyan
}

function Write-Step {
    param([string]$Text)
    Write-Host "`n  >> $Text" -ForegroundColor White
}

function Write-OK {
    param([string]$Text)
    Write-Host "     [OK]  $Text" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Text)
    Write-Host "     [WARN] $Text" -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Text)
    Write-Host "     [FAIL] $Text" -ForegroundColor Red
}

function Write-Info {
    param([string]$Text)
    Write-Host "     [INFO] $Text" -ForegroundColor Gray
}

function Add-Result {
    param(
        [string]$Tool,
        [ValidateSet("Installed","Skipped","Failed","Already Installed","Configured","Restricted")]
        [string]$Status,
        [string]$Detail = ""
    )
    $Script:Results.Add([PSCustomObject]@{
        Tool   = $Tool
        Status = $Status
        Detail = $Detail
    })
}

# ----------------------------------------------------------------
#  Validation helpers
# ----------------------------------------------------------------
function Test-CommandExists {
    param([string]$Command)
    $null -ne (Get-Command $Command -ErrorAction SilentlyContinue)
}

function Get-CommandVersion {
    param(
        [string]$Command,
        [string[]]$CommandArguments = @("--version")
    )
    try {
        $output = & $Command @CommandArguments 2>&1 | Select-Object -First 1
        return $output.ToString().Trim()
    } catch {
        return $null
    }
}

function Test-MinVersion {
    param([string]$VersionString, [string]$MinVersion)
    try {
        $cleaned = ($VersionString -replace '[^0-9\.]', '').Trim('.')
        if ($cleaned -match '^(\d+\.\d+)') {
            return ([version]$cleaned -ge [version]$MinVersion)
        }
        return $false
    } catch {
        return $false
    }
}

function Test-EnvVar {
    param([string]$VarName, [string]$Scope = "Machine")
    $val = [System.Environment]::GetEnvironmentVariable($VarName, $Scope)
    return (-not [string]::IsNullOrWhiteSpace($val))
}

function Set-EnvVarPermanent {
    param([string]$Name, [string]$Value, [string]$Scope = "Machine")
    [System.Environment]::SetEnvironmentVariable($Name, $Value, $Scope)
    Set-Item -Path "Env:$Name" -Value $Value -ErrorAction SilentlyContinue
}

function Add-ToSystemPath {
    param([string]$NewPath)
    $currentPath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    if ($currentPath -notlike "*$NewPath*") {
        $updatedPath = "$currentPath;$NewPath"
        [System.Environment]::SetEnvironmentVariable("Path", $updatedPath, "Machine")
        $env:Path += ";$NewPath"
        Write-OK "Added to system PATH: $NewPath"
        return $true
    } else {
        Write-Info "Already in PATH: $NewPath"
        return $false
    }
}

# ----------------------------------------------------------------
#  Winget operability check
# ----------------------------------------------------------------
function Test-WingetUsable {
    if (-not (Test-CommandExists "winget")) {
        return $false
    }
    try {
        $output = winget list --count 1 --accept-source-agreements 2>&1
        if ($LASTEXITCODE -eq 0) { return $true }
        # Check for Group Policy block
        $outputStr = $output -join " "
        if ($outputStr -match "Group Policy|disabled") {
            Write-Warn "winget is disabled by Group Policy. Will use direct downloads as fallback."
        }
        return $false
    } catch {
        return $false
    }
}

# ----------------------------------------------------------------
#  Direct download fallback installers (when winget is unavailable)
# ----------------------------------------------------------------
function Install-GitDirect {
    Write-Info "Downloading Git installer from official source..."
    Write-Info "(This may take 1-2 minutes on corporate networks)"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $release = Invoke-RestMethod "https://api.github.com/repos/git-for-windows/git/releases/latest" -UseBasicParsing
        $asset = $release.assets | Where-Object { $_.name -match "64-bit\.exe$" -and $_.name -notmatch "portable" } | Select-Object -First 1
        if (-not $asset) { throw "Could not find Git installer asset in latest release." }
        $installerPath = "$env:TEMP\git-installer.exe"
        Write-Info "Downloading: $($asset.name) ..."
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $installerPath -UseBasicParsing
        Write-Info "Running Git silent install..."
        $proc = Start-Process -FilePath $installerPath -ArgumentList "/VERYSILENT","/NORESTART","/NOCANCEL","/SP-" -Wait -PassThru
        Remove-Item $installerPath -Force -ErrorAction SilentlyContinue
        if ($proc.ExitCode -ne 0) { throw "Git installer exited with code $($proc.ExitCode)." }
        return $true
    } catch {
        Write-Fail "Direct Git installation failed: $_"
        return $false
    }
}

function Install-GitHubCliDirect {
    Write-Info "Downloading GitHub CLI installer from official source..."
    Write-Info "(This may take 1-2 minutes on corporate networks)"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $release = Invoke-RestMethod "https://api.github.com/repos/cli/cli/releases/latest" -UseBasicParsing
        $asset = $release.assets | Where-Object { $_.name -match "windows_amd64\.msi$" } | Select-Object -First 1
        if (-not $asset) { throw "Could not find GitHub CLI MSI asset in latest release." }
        $msiPath = "$env:TEMP\gh-installer.msi"
        Write-Info "Downloading: $($asset.name) ..."
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $msiPath -UseBasicParsing
        Write-Info "Running GitHub CLI silent install..."
        $proc = Start-Process msiexec -ArgumentList "/i","`"$msiPath`"","/quiet","/norestart" -Wait -PassThru
        Remove-Item $msiPath -Force -ErrorAction SilentlyContinue
        if ($proc.ExitCode -ne 0) { throw "GitHub CLI MSI exited with code $($proc.ExitCode)." }
        return $true
    } catch {
        Write-Fail "Direct GitHub CLI installation failed: $_"
        return $false
    }
}

function Install-PythonDirect {
    Write-Info "Downloading Python installer from official source..."
    Write-Info "(This may take 1-2 minutes on corporate networks)"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $pyMajorMinor = $Config_PythonVersion
        $page = Invoke-WebRequest "https://www.python.org/ftp/python/" -UseBasicParsing
        $versions = $page.Links | Where-Object { $_.href -match "^$($pyMajorMinor -replace '\.', '\.')\.\d+/" } |
            ForEach-Object { $_.href -replace '/$', '' } | Sort-Object { [version]$_ } -Descending
        $latestVer = $versions | Select-Object -First 1
        if (-not $latestVer) { throw "Could not find Python $pyMajorMinor release on python.org." }
        $exeUrl = "https://www.python.org/ftp/python/$latestVer/python-$latestVer-amd64.exe"
        $exePath = "$env:TEMP\python-installer.exe"
        Write-Info "Downloading: python-$latestVer-amd64.exe ..."
        Invoke-WebRequest -Uri $exeUrl -OutFile $exePath -UseBasicParsing
        Write-Info "Running Python silent install..."
        $proc = Start-Process -FilePath $exePath -ArgumentList "/quiet","InstallAllUsers=1","PrependPath=1","Include_pip=1" -Wait -PassThru
        Remove-Item $exePath -Force -ErrorAction SilentlyContinue
        if ($proc.ExitCode -ne 0) { throw "Python installer exited with code $($proc.ExitCode)." }
        return $true
    } catch {
        Write-Fail "Direct Python installation failed: $_"
        if (Test-CommandExists "uv") {
            Write-Info "Trying uv python install as last resort..."
            try {
                & uv python install --default $Config_PythonVersion
                return $true
            } catch { }
        }
        return $false
    }
}

function Install-NodeJsDirect {
    Write-Info "Downloading Node.js installer from official source..."
    Write-Info "(This may take 1-2 minutes on corporate networks)"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $baseUrl = "https://nodejs.org/dist/latest-v$($Config_NodeMajor).x"
        $page = Invoke-WebRequest "$baseUrl/" -UseBasicParsing
        $msiHref = ($page.Links | Where-Object { $_.href -match "x64\.msi$" } | Select-Object -First 1).href
        if (-not $msiHref) { throw "Could not find Node.js MSI in latest v$Config_NodeMajor directory." }
        # Extract just the filename — href may contain a full path like /dist/latest-v22.x/node-v22.23.2-x64.msi
        $msiName = $msiHref.Split('/')[-1]
        $msiPath = "$env:TEMP\node-installer.msi"
        Write-Info "Downloading: $msiName ..."
        Invoke-WebRequest -Uri "$baseUrl/$msiName" -OutFile $msiPath -UseBasicParsing
        Write-Info "Running Node.js silent install..."
        $proc = Start-Process msiexec -ArgumentList "/i","`"$msiPath`"","/quiet","/norestart" -Wait -PassThru
        Remove-Item $msiPath -Force -ErrorAction SilentlyContinue
        if ($proc.ExitCode -ne 0) { throw "Node.js MSI exited with code $($proc.ExitCode)." }
        return $true
    } catch {
        Write-Fail "Direct Node.js installation failed: $_"
        return $false
    }
}

function Install-AwsCliDirect {
    Write-Info "Downloading AWS CLI installer from official source..."
    Write-Info "(This may take 1-2 minutes on corporate networks)"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $msiPath = "$env:TEMP\AWSCLIV2.msi"
        Write-Info "Downloading: AWSCLIV2.msi ..."
        Invoke-WebRequest -Uri "https://awscli.amazonaws.com/AWSCLIV2.msi" -OutFile $msiPath -UseBasicParsing
        Write-Info "Running AWS CLI silent install..."
        $proc = Start-Process msiexec -ArgumentList "/i","`"$msiPath`"","/quiet","/norestart" -Wait -PassThru
        Remove-Item $msiPath -Force -ErrorAction SilentlyContinue
        if ($proc.ExitCode -ne 0) { throw "AWS CLI MSI exited with code $($proc.ExitCode)." }
        return $true
    } catch {
        Write-Fail "Direct AWS CLI installation failed: $_"
        return $false
    }
}

# ----------------------------------------------------------------
#  Certificate validation for Claude Code endpoints
# ----------------------------------------------------------------
function Test-ClaudeCodeCertificates {
    Write-Step "Checking SSL/TLS certificate validation for Claude Code endpoints..."

    $endpoints = @{
        "claude.ai" = "https://claude.ai"
        "Anthropic API" = "https://api.anthropic.com"
    }

    $certIssues = $false

    foreach ($name in $endpoints.Keys) {
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $response = Invoke-WebRequest -Uri $endpoints[$name] -Method Head -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
            Write-OK "$name - Certificate valid"
        } catch {
            if ($_.Exception.Message -match "SSL|certificate|trust|TLS|Could not create SSL/TLS") {
                Write-Warn "$name - Certificate validation FAILED"
                Write-Info "Error: $($_.Exception.Message.Split([Environment]::NewLine)[0])"
                $certIssues = $true
            } else {
                Write-OK "$name - Certificate valid (HTTP $($_.Exception.Response.StatusCode) is expected)"
            }
        }
    }

    if ($certIssues) {
        Write-Host ""
        Write-Warn "Certificate validation issues detected. This is common on corporate networks with SSL inspection."
        Write-Info "The installer will use TLS workaround if standard installation fails."
        $Script:CertificateIssuesDetected = $true
    }

    return (-not $certIssues)
}

# ----------------------------------------------------------------
#  Timeout-enabled command version check
# ----------------------------------------------------------------
function Get-InstallStatusWithTimeout {
    param(
        [string]$Cmd,
        [string]$Label,
        [int]$TimeoutSeconds = 30
    )

    try {
        try {
            $quickCheck = & $Cmd --version 2>&1 | Select-Object -First 1
            if ($quickCheck -and $quickCheck.ToString().Trim()) {
                Write-Host ("  {0,-18}: Found - {1}" -f $Label, $quickCheck) -ForegroundColor Green
                return $quickCheck
            }
        } catch {}

        $currentPath = $env:PATH
        $job = Start-Job -ScriptBlock {
            param($command, $pathVar)
            $env:PATH = $pathVar
            try {
                $output = & $command --version 2>&1 | Select-Object -First 1
                return $output
            } catch {
                return $null
            }
        } -ArgumentList $Cmd, $currentPath

        $completed = Wait-Job -Job $job -Timeout $TimeoutSeconds

        if ($completed) {
            $v = Receive-Job -Job $job
            Remove-Job -Job $job -Force
            if ($v) {
                Write-Host ("  {0,-18}: Found - {1}" -f $Label, $v) -ForegroundColor Green
                return $v
            } else {
                Write-Host ("  {0,-18}: Not found - will install" -f $Label) -ForegroundColor Yellow
                return $null
            }
        } else {
            Stop-Job -Job $job -ErrorAction SilentlyContinue
            Remove-Job -Job $job -Force
            Write-Host ("  {0,-18}: Check timed out - will attempt install" -f $Label) -ForegroundColor Yellow
            return $null
        }
    } catch {
        Write-Host ("  {0,-18}: Error checking - will attempt install" -f $Label) -ForegroundColor Yellow
        return $null
    }
}

# ----------------------------------------------------------------
#  PRE-FLIGHT CHECKS
# ----------------------------------------------------------------
function Invoke-PreflightChecks {
    Write-Header "PRE-FLIGHT CHECKS"
    $allPassed = $true

    # 1. Admin check
    Write-Step "Checking for Administrator privileges..."
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($isAdmin) {
        Write-OK "Running as Administrator."
    } else {
        Write-Fail "This script must be run as Administrator."
        exit 1
    }

    # 2. OS version check
    Write-Step "Checking Windows version..."
    $os = Get-CimInstance Win32_OperatingSystem
    $build = [int]$os.BuildNumber
    if ($build -ge 17763) {
        Write-OK "Windows build $build detected - compatible."
    } else {
        Write-Warn "Windows build $build may be too old. Windows 10 1809+ or Windows 11 recommended."
    }

    # 3. winget availability
    Write-Step "Checking winget availability..."
    $Script:WingetUsable = Test-WingetUsable
    if ($Script:WingetUsable) {
        $wgVer = winget --version 2>&1
        Write-OK "winget found and operational: $wgVer"
    } elseif (Test-CommandExists "winget") {
        Write-Warn "winget found but not operational (likely blocked by Group Policy). Will use direct downloads."
    } else {
        Write-Warn "winget not found. Will use direct downloads as fallback."
    }

    # 4. Internet connectivity
    Write-Step "Checking internet connectivity..."
    $pingResult = Test-Connection -ComputerName "8.8.8.8" -Count 1 -Quiet -ErrorAction SilentlyContinue
    if ($pingResult) {
        Write-OK "Internet connectivity confirmed."
    } else {
        Write-Fail "No internet connectivity detected. Check your network and retry."
        $allPassed = $false
    }

    # 5. Certificate validation
    if (-not $Script:ClaudeAlreadyInstalled) {
        Test-ClaudeCodeCertificates | Out-Null
    }

    # 6. Disk space check
    Write-Step "Checking available disk space (minimum: $Config_MinDiskSpaceGB GB)..."
    $drive = Split-Path $env:SystemDrive -Qualifier
    $disk  = Get-PSDrive -Name ($drive.TrimEnd(':')) -ErrorAction SilentlyContinue
    if ($disk) {
        $freeGB = [math]::Round($disk.Free / 1GB, 1)
        if ($freeGB -ge $Config_MinDiskSpaceGB) {
            Write-OK "${freeGB} GB free on $drive - sufficient."
        } else {
            Write-Fail "Only ${freeGB} GB free on $drive. At least $Config_MinDiskSpaceGB GB required."
            $allPassed = $false
        }
    } else {
        Write-Warn "Could not determine disk space. Proceeding with caution."
    }

    if (-not $allPassed) {
        Write-Host "`n[ABORT] One or more pre-flight checks failed. Fix the issues above and re-run.`n" -ForegroundColor Red
        exit 1
    }

    Write-Host "`n  All pre-flight checks passed.`n" -ForegroundColor Green
}

# ----------------------------------------------------------------
#  1. GIT
# ----------------------------------------------------------------
function Install-Git {
    $gitVersion = Get-CommandVersion "git"
    if ($Config_SkipIfInstalled -and $gitVersion) {
        # Silently ensure env var is set (idempotent, no output)
        $gitBashExe = "$Config_GitInstallPath\bin\bash.exe"
        if (-not (Test-Path $gitBashExe)) { $gitBashExe = "C:\Program Files\Git\bin\bash.exe" }
        if ((Test-Path $gitBashExe) -and -not (Test-EnvVar "CLAUDE_CODE_GIT_BASH_PATH")) {
            Set-EnvVarPermanent "CLAUDE_CODE_GIT_BASH_PATH" $gitBashExe "Machine"
        }
        Add-Result "Git" "Already Installed" $gitVersion
        return
    }

    Write-Header "Git"

    $gitInstalled = $false
    if ($Script:WingetUsable) {
        Write-Step "Installing Git via winget..."
        try {
            winget install --id Git.Git --silent --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) { $gitInstalled = $true }
        } catch { }
    }
    if (-not $gitInstalled) {
        Write-Step "Installing Git via direct download..."
        $gitInstalled = Install-GitDirect
    }
    if ($gitInstalled) {
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        Write-OK "Git installed."
        Add-Result "Git" "Installed"
    } else {
        Write-Fail "Git installation failed via all methods."
        Add-Result "Git" "Failed" "winget and direct download both failed"
        return
    }

    # Set CLAUDE_CODE_GIT_BASH_PATH
    Write-Step "Configuring CLAUDE_CODE_GIT_BASH_PATH environment variable..."
    $gitBashExe = "$Config_GitInstallPath\bin\bash.exe"
    if (-not (Test-Path $gitBashExe)) {
        $gitBashExe = "C:\Program Files\Git\bin\bash.exe"
    }
    if (Test-Path $gitBashExe) {
        Set-EnvVarPermanent "CLAUDE_CODE_GIT_BASH_PATH" $gitBashExe "Machine"
        Write-OK "CLAUDE_CODE_GIT_BASH_PATH = $gitBashExe"
    } else {
        Write-Warn "bash.exe not found at expected paths. Set CLAUDE_CODE_GIT_BASH_PATH manually."
    }

    # Add Git cmd to system PATH
    Write-Step "Ensuring Git cmd directory is on system PATH..."
    $gitCmdPath = "$Config_GitInstallPath\cmd"
    if (-not (Test-Path $gitCmdPath)) {
        $gitCmdPath = "C:\Program Files\Git\cmd"
    }
    if (Test-Path $gitCmdPath) {
        Add-ToSystemPath $gitCmdPath | Out-Null
    }

    # Post-install validation
    Write-Step "Post-install validation - Git..."
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    $v = Get-CommandVersion "git"
    if ($v) { Write-OK "git --version: $v" } else { Write-Fail "git command not found after install." }
}

# ----------------------------------------------------------------
#  2. GITHUB CLI (gh)
# ----------------------------------------------------------------
function Install-GitHubCli {
    $ghVersion = Get-CommandVersion "gh"
    if ($Config_SkipIfInstalled -and $ghVersion) {
        Add-Result "GitHub CLI" "Already Installed" $ghVersion
        return
    }

    Write-Header "GitHub CLI (gh)"

    $ghInstalled = $false
    if ($Script:WingetUsable) {
        Write-Step "Installing GitHub CLI via winget..."
        try {
            winget install --id GitHub.cli --silent --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) { $ghInstalled = $true }
        } catch { }
    }
    if (-not $ghInstalled) {
        Write-Step "Installing GitHub CLI via direct download..."
        $ghInstalled = Install-GitHubCliDirect
    }
    if ($ghInstalled) {
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        Write-OK "GitHub CLI installed."
        Add-Result "GitHub CLI" "Installed"
    } else {
        Write-Fail "GitHub CLI installation failed via all methods."
        Add-Result "GitHub CLI" "Failed" "winget and direct download both failed"
        return
    }

    # Post-install validation
    Write-Step "Post-install validation - GitHub CLI..."
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    $v = Get-CommandVersion "gh"
    if ($v) {
        Write-OK "gh --version: $v"
    } else {
        Write-Fail "gh command not found after install. PATH may need a shell restart."
    }
}

# ----------------------------------------------------------------
#  3. PYTHON
# ----------------------------------------------------------------
function Install-Python {
    $pyVer = Get-CommandVersion "python"
    if (-not $pyVer) { $pyVer = Get-CommandVersion "python3" }
    $meetsMin = $pyVer -and (Test-MinVersion $pyVer "3.11")

    if ($Config_SkipIfInstalled -and $meetsMin) {
        Add-Result "Python" "Already Installed" $pyVer
        return
    }

    Write-Header "Python $Config_PythonVersion+"

    $pyInstalled = $false
    if ($Script:WingetUsable) {
        Write-Step "Installing Python $Config_PythonVersion via winget..."
        try {
            $wingetId = "Python.Python.$($Config_PythonVersion.Split('.')[0]).$($Config_PythonVersion.Split('.')[1])"
            winget install --id $wingetId --silent --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) { $pyInstalled = $true }
        } catch { }
    }
    if (-not $pyInstalled) {
        Write-Step "Installing Python $Config_PythonVersion via direct download (fallback)..."
        $pyInstalled = Install-PythonDirect
    }
    if ($pyInstalled) {
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        Write-OK "Python installed."
        Add-Result "Python" "Installed" $Config_PythonVersion
    } else {
        Write-Fail "Python installation failed via all methods."
        Add-Result "Python" "Failed" "winget and direct download both failed"
        return
    }

    # Ensure Python and Scripts dirs are on PATH
    Write-Step "Ensuring Python and Scripts directories are on system PATH..."
    $pyBase = "$Config_PythonBasePath\Python$($Config_PythonVersion -replace '\.', '')"
    $pyPaths = @($pyBase, "$pyBase\Scripts")
    foreach ($p in $pyPaths) {
        if (Test-Path $p) { Add-ToSystemPath $p | Out-Null }
    }

    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")

    # Post-install validation
    Write-Step "Post-install validation - Python..."
    $v = Get-CommandVersion "python"
    if (-not $v) { $v = Get-CommandVersion "python3" }
    if ($v) { Write-OK "python --version: $v" } else { Write-Fail "python/python3 command not found after install." }
}

# ----------------------------------------------------------------
#  4. NODE.JS
# ----------------------------------------------------------------
function Install-NodeJs {
    $nodeVer = Get-CommandVersion "node"
    if ($Config_SkipIfInstalled -and $nodeVer) {
        Add-Result "Node.js" "Already Installed" $nodeVer
        return
    }

    Write-Header "Node.js (LTS $Config_NodeMajor.x)"

    $nodeInstalled = $false
    if ($Script:WingetUsable) {
        Write-Step "Installing Node.js LTS via winget..."
        try {
            winget install --id OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -eq 0) { $nodeInstalled = $true }
        } catch { }
    }
    if (-not $nodeInstalled) {
        Write-Step "Installing Node.js via direct download..."
        $nodeInstalled = Install-NodeJsDirect
    }
    if ($nodeInstalled) {
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        Write-OK "Node.js installed."
        Add-Result "Node.js" "Installed"
    } else {
        Write-Fail "Node.js installation failed via all methods."
        Add-Result "Node.js" "Failed" "winget and direct download both failed"
        return
    }

    # Post-install validation
    Write-Step "Post-install validation - Node.js..."
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    $v = Get-CommandVersion "node"
    if ($v) { Write-OK "node --version: $v" } else { Write-Fail "node command not found after install." }
    $npmV = Get-CommandVersion "npm"
    if ($npmV) { Write-OK "npm --version: $npmV" } else { Write-Fail "npm command not found after install." }
}

# ----------------------------------------------------------------
#  5. UV
# ----------------------------------------------------------------
function Install-Uv {
    # uv installs to ~\.local\bin, which is not on PATH until a new shell picks
    # up the machine/user PATH. Seed it here so an existing uv is detected.
    if ((Test-Path $Config_UvBinPath) -and ($env:Path -notlike "*$Config_UvBinPath*")) {
        $env:Path = "$Config_UvBinPath;$env:Path"
    }
    $uvVer = Get-CommandVersion "uv"
    if ($Config_SkipIfInstalled -and $uvVer) {
        Add-Result "uv" "Already Installed" $uvVer
        return
    }

    Write-Header "uv (Python package manager)"

    Write-Step "Installing uv via official installer..."
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-Expression (Invoke-RestMethod "https://astral.sh/uv/install.ps1" -UseBasicParsing)
        Write-OK "uv install script executed."
        Add-Result "uv" "Installed"
    } catch {
        Write-Fail "uv installation failed: $_"
        Add-Result "uv" "Failed" $_.Exception.Message
        return
    }

    # Ensure uv bin is on PATH
    Write-Step "Ensuring uv bin directory is on system PATH..."
    if (Test-Path $Config_UvBinPath) {
        Add-ToSystemPath $Config_UvBinPath | Out-Null
    }

    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")

    Write-Step "Post-install validation - uv..."
    $v = Get-CommandVersion "uv"
    if ($v) { Write-OK "uv --version: $v" } else { Write-Warn "uv not found in current session. Open a new shell and run: uv --version" }
}

# ----------------------------------------------------------------
#  6. AWS CLI
# ----------------------------------------------------------------
function Install-AwsCli {
    $awsVersion = Get-CommandVersion "aws"
    if ($Config_SkipIfInstalled -and $awsVersion -and $awsVersion -match '^aws-cli/2\.') {
        Add-Result "AWS CLI" "Already Installed" $awsVersion
        return
    }

    Write-Header "AWS CLI"

    if ($awsVersion -and $awsVersion -notmatch '^aws-cli/2\.') {
        Write-Info "Existing AWS CLI is not v2; installing the v2 package."
    }

    $awsInstalled = $false
    if ($Script:WingetUsable) {
        Write-Step "Installing AWS CLI via winget..."
        try {
            $wingetPath = (Get-Command "winget" -ErrorAction Stop).Source
            $wingetArgs = @(
                "install",
                "--exact",
                "--id", "Amazon.AWSCLI",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements"
            )
            $wingetProcess = Start-Process `
                -FilePath $wingetPath `
                -ArgumentList $wingetArgs `
                -Verb RunAs `
                -Wait `
                -PassThru
            $wingetExitCode = $wingetProcess.ExitCode
            if ($null -eq $wingetExitCode -or $wingetExitCode -eq 0) {
                $awsInstalled = $true
            }
        } catch { }
    }

    if (-not $awsInstalled) {
        Write-Step "Installing AWS CLI via direct download..."
        $awsInstalled = Install-AwsCliDirect
    }

    if (-not $awsInstalled) {
        Write-Fail "AWS CLI installation failed via all methods."
        Add-Result "AWS CLI" "Failed" "winget and direct download both failed"
        return
    }

    # Refresh PATH so the MSI-installed command is visible in this session.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")

    Write-Step "Post-install validation - AWS CLI..."
    $v = Get-CommandVersion "aws"
    if ($v) {
        Write-OK "aws --version: $v"
        Add-Result "AWS CLI" "Installed" $v
    } else {
        Write-Fail "aws command not found after install. PATH may need a shell restart."
        Add-Result "AWS CLI" "Failed" "Command not found after install"
    }
}


# ----------------------------------------------------------------
#  8. CLAUDE CODE
# ----------------------------------------------------------------
function Install-ClaudeCode {
    if ($Script:ClaudeAlreadyInstalled) {
        $v = Get-CommandVersion "claude"
        Add-Result "Claude Code" "Already Installed" $v
        return
    }

    Write-Header "Claude Code"

    Write-Step "Installing Claude Code via official installer..."
    $claudeInstalled = $false
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-Expression (Invoke-RestMethod "https://claude.ai/install.ps1" -UseBasicParsing)
        if ($LASTEXITCODE -eq 0 -or $?) {
            Write-OK "Claude Code installer executed."
            $claudeInstalled = $true
        }
    } catch {
        if ($_.Exception.Message -match "SSL|certificate|trust|TLS|Could not create SSL/TLS") {
            Write-Warn "Certificate error detected. Attempting TLS workaround..."
            try {
                $env:NODE_TLS_REJECT_UNAUTHORIZED = "0"
                [System.Environment]::SetEnvironmentVariable("NODE_TLS_REJECT_UNAUTHORIZED", "0", "User")
                Invoke-Expression (Invoke-RestMethod "https://claude.ai/install.ps1" -UseBasicParsing)
                Write-OK "Claude Code installed (TLS workaround applied)."
                $claudeInstalled = $true
            } catch { }
        }
    }

    if ($claudeInstalled) {
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")
        $v = Get-CommandVersion "claude"
        if ($v) {
            Write-OK "claude --version: $v"
            Add-Result "Claude Code" "Installed" $v
        } else {
            Add-Result "Claude Code" "Installed" "restart shell to verify"
        }
    } else {
        Write-Warn "Claude Code download is restricted on this network."
        Write-Warn "Please contact your ITS team to unblock: https://downloads.claude.ai"
        Write-Info "Once unblocked, re-run this installer or install manually:"
        Write-Info "  curl -fsSL https://claude.ai/install.ps1 | powershell"
        Add-Result "Claude Code" "Restricted" "download blocked - contact ITS to unblock"
    }
}

# ----------------------------------------------------------------
#  9. AAH HARNESS INSTALL (final step)
# ----------------------------------------------------------------
function Install-AahHarness {
    Write-Header "AAH Harness Install"

    # --- Resolve the harness source shipped with this script ------------
    # This installer lives in <harness>\installers\, so the source root is its
    # parent: the folder holding pyproject.toml. Nothing is downloaded and no
    # repository is contacted - the code being installed is the code that was
    # unzipped alongside this script.
    $Script:AahZipSource = Split-Path -Parent $PSScriptRoot
    if (-not (Test-Path (Join-Path $Script:AahZipSource "pyproject.toml"))) {
        Write-Fail "Could not find the AAH source next to this installer."
        Write-Warn "Expected pyproject.toml in: $($Script:AahZipSource)"
        Write-Warn "Run this script from the installers\ folder of the unzipped"
        Write-Warn "harness download (ascend-agentic-harness-main\installers\)."
        Add-Result "AAH Harness" "Failed" "harness source not found next to installer"
        return
    }
    Write-OK "Harness source: $($Script:AahZipSource)"

    # --- Tear down existing AAH install (if any) --------------------------
    # Best-effort cleanup before reinstalling. Avoids OS error 32 (locked files)
    # and stale .claude assets that the linker refuses to overwrite.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User")
    $aahExists = Get-Command aah -ErrorAction SilentlyContinue

    if ($aahExists) {
        Write-Step "Removing existing AAH install..."
        Get-Process aah -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2

        try { & aah uninstall --purge 2>&1 | Out-Null } catch { }

        $aahToolDir = Join-Path $env:APPDATA "uv\tools\aah"
        if (Test-Path $aahToolDir) {
            Remove-Item -Path $aahToolDir -Recurse -Force -ErrorAction SilentlyContinue
        }

        # NOTE: we intentionally do NOT delete ~/.claude/agents or ~/.claude/skills.
        # `aah uninstall --purge` above already removes exactly our own links/files
        # by name (see linkutil.unlink_if_ours); blowing away the whole directory
        # would also destroy the user's own agents/skills. The linker refreshes our
        # entries in place on reinstall, so no manual teardown of those dirs is needed.

        # Remove stale aah shim from current session PATH
        $staleDir = Split-Path $aahExists.Source -Parent
        $env:Path = ($env:Path -split ';' | Where-Object { $_ -ne $staleDir }) -join ';'
        Write-OK "Previous install removed."
    } else {
        Write-Info "No existing AAH install detected - performing fresh install."
    }

    # --- Install AAH as a global uv tool (non-editable) ------------------
    # Installs directly from the unzipped source. uv packages everything into
    # its managed environment (%APPDATA%\uv\tools\aah). The unzipped folder
    # can be deleted after install.
    $sourceSpec = "$($Script:AahZipSource)[cloud,gcp]"
    $setupArgs  = @("setup", "--platform", "claude")

    Write-Step "Running AAH harness install..."
    Write-Info "Command: uv tool install --force --reinstall `"$sourceSpec`""
    Write-Info "Setup  : aah $($setupArgs -join ' ')"
    Write-Host ""

    try {
        # Refresh PATH to pick up uv
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User")

        & uv tool install --force --reinstall $sourceSpec
        $installExitCode = $LASTEXITCODE
        if ($installExitCode -ne 0) {
            Write-Fail "AAH tool install exited with code $installExitCode"
            Write-Host ""
            Write-Host "  ========================================================================" -ForegroundColor Yellow
            Write-Host "    If the error above mentions Python >= 3.11 not being satisfied, uv" -ForegroundColor Yellow
            Write-Host "    did not find a new enough interpreter. Fix it with:" -ForegroundColor Yellow
            Write-Host "" -ForegroundColor Yellow
            Write-Host "      uv python install $Config_PythonVersion" -ForegroundColor White
            Write-Host "" -ForegroundColor Yellow
            Write-Host "    then re-run this installer. 'uv python list' shows what uv detects." -ForegroundColor Yellow
            Write-Host "  ========================================================================" -ForegroundColor Yellow
            Add-Result "AAH Harness" "Failed" "uv tool exit code: $installExitCode"
            return
        }

        # Pick up the newly installed aah launcher before linking assets. uv
        # drops it in ~\.local\bin, which an elevated session may not inherit.
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                    $Config_UvBinPath
        & aah @setupArgs
        $setupExitCode = $LASTEXITCODE
        if ($setupExitCode -ne 0) {
            Write-Fail "AAH setup exited with code $setupExitCode"
            Add-Result "AAH Harness" "Failed" "aah setup exit code: $setupExitCode"
            return
        }

        Write-OK "AAH harness installed successfully."
        Add-Result "AAH Harness" "Installed" "global (managed by uv)"
    } catch {
        Write-Fail "AAH harness install failed: $_"
        Add-Result "AAH Harness" "Failed" $_.Exception.Message
    }
}

# ----------------------------------------------------------------
#  FINAL SUMMARY REPORT
# ----------------------------------------------------------------
function Write-SummaryReport {
    Write-Header "INSTALLATION SUMMARY"

    $colW = @{ Tool = 20; Status = 18; Detail = 35 }
    $sep  = "-" * ($colW.Tool + $colW.Status + $colW.Detail + 6)

    Write-Host ""
    Write-Host ("  {0,-$($colW.Tool)} {1,-$($colW.Status)} {2,-$($colW.Detail)}" -f "TOOL", "STATUS", "DETAIL") -ForegroundColor White
    Write-Host "  $sep"

    foreach ($r in $Script:Results) {
        $color = switch ($r.Status) {
            "Installed"        { "Green"  }
            "Already Installed"{ "Cyan"   }
            "Configured"       { "Green"  }
            "Skipped"          { "Yellow" }
            "Restricted"       { "Yellow" }
            "Failed"           { "Red"    }
            default            { "White"  }
        }
        $icon = switch ($r.Status) {
            "Installed"        { "[OK] " }
            "Already Installed"{ "[OK] " }
            "Configured"       { "[OK] " }
            "Skipped"          { "[--] " }
            "Restricted"       { "[!!] " }
            "Failed"           { "[!!] " }
            default            { "     " }
        }
        $line = "  {0} {1,-$($colW.Tool - 5)} {2,-$($colW.Status)} {3,-$($colW.Detail)}" -f `
            $icon, $r.Tool, $r.Status, $r.Detail
        Write-Host $line -ForegroundColor $color
    }

    Write-Host "  $sep"
    $failed = @($Script:Results | Where-Object { $_.Status -eq "Failed" })
    $restricted = @($Script:Results | Where-Object { $_.Status -eq "Restricted" })

    if ($failed.Count -eq 0 -and $restricted.Count -eq 0) {
        Write-Host "`n  All steps completed successfully!" -ForegroundColor Green
    } else {
        if ($restricted.Count -gt 0) {
            Write-Host ""
            foreach ($r in $restricted) {
                Write-Host "  [RESTRICTED] $($r.Tool): $($r.Detail)" -ForegroundColor Yellow
            }
            Write-Host "  Please contact your ITS team to unblock the restricted downloads." -ForegroundColor Yellow
            Write-Host "  Once unblocked, re-run this installer or see: installers\AAH-Dependency-Installer-Guide.md (Troubleshooting)" -ForegroundColor Yellow
        }
        if ($failed.Count -gt 0) {
            Write-Host "`n  $($failed.Count) step(s) failed:" -ForegroundColor Red
            foreach ($f in $failed) {
                Write-Host "    - $($f.Tool): $($f.Detail)" -ForegroundColor Red
            }
        }
    }

    Write-Host ""
    Write-Host "  Install type  : global uv tool (links into ~\.claude)" -ForegroundColor White
    # --- Verification -------------------------------------------------
    # Report what is actually on disk rather than assuming success. A binary
    # present in ~\.local\bin but not resolvable means the CURRENT shell has a
    # stale PATH, which is a "reopen your terminal" situation, not a failure.
    # Refresh PATH to pick up all changes made during this session
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" +
                "$env:USERPROFILE\.local\bin"

    Write-Host "  ---- Verifying Installation ----" -ForegroundColor Cyan
    Write-Host ""

    $verifyOk = $true
    $needsShellReload = $false

    function Test-Component {
        param([string]$Name, [string]$Command, [string[]]$ProbeArgs)

        $resolved = $null
        $cmdInfo = Get-Command $Command -ErrorAction SilentlyContinue
        if ($cmdInfo) {
            $resolved = $cmdInfo.Source
        } else {
            $fallback = Join-Path $Config_UvBinPath "$Command.exe"
            if (Test-Path $fallback) {
                $resolved = $fallback
                $Script:NeedsReload = $true
            }
        }

        if (-not $resolved) {
            Write-Host ("    [MISS] {0,-14} not found" -f $Name) -ForegroundColor Red
            $Script:VerifyFailed = $true
            return
        }

        # Present but non-functional is a real failure, so gate on exit code.
        # Reset LASTEXITCODE before probing to avoid stale values from prior commands.
        $Global:LASTEXITCODE = 0
        $out = & $resolved @ProbeArgs 2>&1 | Select-Object -First 1
        if ($LASTEXITCODE -eq 0 -or $out) {
            Write-Host ("    [OK]   {0,-14} {1}" -f $Name, $out) -ForegroundColor Green
        } else {
            Write-Host ("    [WARN] {0,-14} found at {1} but not responding" -f $Name, $resolved) -ForegroundColor Yellow
            $Script:VerifyFailed = $true
        }
    }

    $Script:VerifyFailed = $false
    $Script:NeedsReload  = $false

    # `aah` has no --version flag; `path` is its cheapest successful call.
    Test-Component "aah"         "aah"    @("path")
    # Skip Claude Code verification if it was restricted (already reported above)
    $claudeRestricted = @($Script:Results | Where-Object { $_.Tool -eq "Claude Code" -and $_.Status -eq "Restricted" })
    if ($claudeRestricted.Count -eq 0) {
        Test-Component "Claude Code" "claude" @("--version")
    }
    Test-Component "uv"          "uv"     @("--version")

    $claudeDir = Join-Path $env:USERPROFILE ".claude"
    if ((Test-Path (Join-Path $claudeDir "skills")) -and (Test-Path (Join-Path $claudeDir "agents"))) {
        Write-Host ("    [OK]   {0,-14} {1} (skills + agents linked)" -f "AAH assets", $claudeDir) -ForegroundColor Green
    } else {
        Write-Host ("    [MISS] {0,-14} {1} incomplete" -f "AAH assets", $claudeDir) -ForegroundColor Red
        $Script:VerifyFailed = $true
    }

    $verifyOk = -not $Script:VerifyFailed
    $needsShellReload = $Script:NeedsReload

    Write-Host ""
    if ($needsShellReload) {
        Write-Host "  NOTE: Installed correctly, but this shell has a stale PATH." -ForegroundColor Yellow
        Write-Host "        Open a new PowerShell window to pick up the changes.`n" -ForegroundColor Yellow
    } elseif (-not $verifyOk) {
        Write-Host "  Some components could not be installed (restricted network)." -ForegroundColor Yellow
        Write-Host "  Contact your ITS team to unblock restricted downloads, then" -ForegroundColor Yellow
        Write-Host "  re-run this installer. See: installers\AAH-Dependency-Installer-Guide.md`n" -ForegroundColor Gray
    }

    Write-Host "  +============================================================================+" -ForegroundColor Red
    Write-Host "  |                                                                            |" -ForegroundColor Red
    Write-Host "  |        !!  IMPORTANT - PLEASE READ BEFORE USING AAH  !!                   |" -ForegroundColor Red
    Write-Host "  |                                                                            |" -ForegroundColor Red
    Write-Host "  |   Warning: AAH uses the Playwright MCP tool to validate and test the       |" -ForegroundColor Yellow
    Write-Host "  |   frontend components of the application you build. Playwright works by    |" -ForegroundColor Yellow
    Write-Host "  |   capturing your screen. These captures must not be shared outside the     |" -ForegroundColor Yellow
    Write-Host "  |   team without thorough review. Do not pass client data through this       |" -ForegroundColor Yellow
    Write-Host "  |   workflow without explicit approval from your LCSP / QRM team.            |" -ForegroundColor Yellow
    Write-Host "  |                                                                            |" -ForegroundColor Red
    Write-Host "  +============================================================================+" -ForegroundColor Red
    Write-Host ""

    Write-Host "  IMPORTANT: Open a NEW PowerShell window before using AAH." -ForegroundColor Yellow
    Write-Host "  Environment variables and PATH changes take effect in new sessions.`n" -ForegroundColor Yellow

    # Next steps
    Write-Host "  NEXT STEPS:" -ForegroundColor Cyan
    Write-Host "  1. Open a new terminal" -ForegroundColor White
    Write-Host "  2. Confirm AAH is working:" -ForegroundColor White
    Write-Host "       aah status" -ForegroundColor Gray
    Write-Host "  3. Create or navigate to your project folder" -ForegroundColor White
    Write-Host "  4. Start Claude Code:" -ForegroundColor White
    Write-Host "       claude" -ForegroundColor Gray
    Write-Host "  5. Initialize your project:" -ForegroundColor White
    Write-Host "       /aah-init-project" -ForegroundColor Gray
    Write-Host ""


}

# ================================================================
#  INTERACTIVE CONFIG COLLECTION
# ================================================================
function Invoke-ConfigReview {
    Write-Header "AAH INSTALLATION SETUP"
    Write-Host ""

    # ----------------------------------------------------------------
    #  1. Show what will be installed (no mode/scope questions to ask)
    # ----------------------------------------------------------------
    # There is one install shape: global uv tool, from the harness source
    # that shipped in this download. Nothing here needs a decision from the
    # user, so it is reported rather than prompted for.
    $zipSource = Split-Path -Parent $PSScriptRoot
    Write-Host "  ---- AAH Install Plan ----" -ForegroundColor Cyan
    Write-Host ""
    if (Test-Path (Join-Path $zipSource "pyproject.toml")) {
        Write-Host "    Source (this download) : $zipSource" -ForegroundColor White
    } else {
        Write-Host "    Source (this download) : NOT FOUND next to this script" -ForegroundColor Red
        Write-Host "      Run the installer from the unzipped harness folder:" -ForegroundColor Yellow
        Write-Host "      ascend-agentic-harness-main\installers\" -ForegroundColor Yellow
    }
    Write-Host "    Install              : global uv tool" -ForegroundColor White
    Write-Host "    Links into           : $(Join-Path $env:USERPROFILE '.claude')" -ForegroundColor White
    Write-Host ""
    Write-Host "    No repository is contacted - AAH is installed from the files" -ForegroundColor Gray
    Write-Host "    you just unzipped." -ForegroundColor Gray
    Write-Host ""

    # ----------------------------------------------------------------
    #  2. Detect an existing install (informational)
    # ----------------------------------------------------------------
    Write-Host "  ---- Current Installation ----" -ForegroundColor Cyan
    Write-Host ""

    $earlyUvBin = Join-Path $env:USERPROFILE ".local\bin"
    if ((Test-Path $earlyUvBin) -and ($env:Path -notlike "*$earlyUvBin*")) {
        $env:Path = "$earlyUvBin;$env:Path"
    }
    $aahCmd = Get-Command aah -ErrorAction SilentlyContinue
    if ($aahCmd) {
        Write-Host "    AAH is currently installed:" -ForegroundColor Green
        Write-Host "      Binary : $($aahCmd.Source)" -ForegroundColor White
        Write-Host ""
    } else {
        Write-Host "    No existing AAH installation detected." -ForegroundColor Gray
        Write-Host ""
    }
    Write-Host ""


    # ----------------------------------------------------------------
    #  4. Claude Code detection
    # ----------------------------------------------------------------
    Write-Host "  ---- Claude Code ----" -ForegroundColor Cyan

    $claudeCmd   = Get-Command claude -ErrorAction SilentlyContinue
    $claudeVer   = if ($claudeCmd) { (& claude --version 2>&1) | Select-Object -First 1 } else { $null }
    $claudeWorks = $claudeCmd -and ($LASTEXITCODE -eq 0) -and $claudeVer

    if ($claudeWorks) {
        Write-Host "  Claude Code already installed: $claudeVer" -ForegroundColor Green
        $Script:ClaudeAlreadyInstalled = $true
    } else {
        Write-Host "  Claude Code not detected - will install." -ForegroundColor Yellow
    }
    Write-Host ""

    # ----------------------------------------------------------------
    #  5. Detect currently installed software
    # ----------------------------------------------------------------
    Write-Host "  ---- Checking Installed Software ----" -ForegroundColor Cyan
    Write-Host ""

    Get-InstallStatusWithTimeout "git"    "Git"       | Out-Null
    Get-InstallStatusWithTimeout "gh"     "GitHub CLI"| Out-Null
    $pyCheck = Get-InstallStatusWithTimeout "python" "Python"
    if (-not $pyCheck) { Get-InstallStatusWithTimeout "python3" "Python" | Out-Null }
    else { $pyCheck | Out-Null }
    Get-InstallStatusWithTimeout "node"   "Node.js"   | Out-Null
    Get-InstallStatusWithTimeout "uv"     "uv"        | Out-Null
    Get-InstallStatusWithTimeout "aws"    "AWS CLI"   | Out-Null

    if ($Script:ClaudeAlreadyInstalled) {
        Write-Host "  Claude Code      : Found - $claudeVer" -ForegroundColor Green
    } else {
        Write-Host "  Claude Code      : Not found - will install" -ForegroundColor Yellow
    }

    Write-Host ""

    # ----------------------------------------------------------------
    #  6. Final confirmation
    # ----------------------------------------------------------------
    Write-Host "  ---- Confirm and Proceed ----" -ForegroundColor Cyan
    Write-Host "  AAH install   : global uv tool" -ForegroundColor White
    Write-Host ""

    # Determine what will actually be done
    $Script:ToolsToInstall = @()
    if (-not (Get-CommandVersion "git"))    { $Script:ToolsToInstall += "Git" }
    if (-not (Get-CommandVersion "gh"))     { $Script:ToolsToInstall += "GitHub CLI" }
    $pyVer = Get-CommandVersion "python"
    if (-not $pyVer) { $pyVer = Get-CommandVersion "python3" }
    if (-not $pyVer -or -not (Test-MinVersion $pyVer "3.11")) { $Script:ToolsToInstall += "Python $Config_PythonVersion" }
    if (-not (Get-CommandVersion "node"))   { $Script:ToolsToInstall += "Node.js $Config_NodeMajor" }
    if (-not (Get-CommandVersion "uv"))     { $Script:ToolsToInstall += "uv" }
    if (-not (Get-CommandVersion "aws"))    { $Script:ToolsToInstall += "AWS CLI" }
    if (-not $Script:ClaudeAlreadyInstalled) { $Script:ToolsToInstall += "Claude Code" }

    if ($Script:ToolsToInstall.Count -gt 0) {
        Write-Host "  Will install    : $($Script:ToolsToInstall -join ', ')" -ForegroundColor Yellow
    } else {
        Write-Host "  Prerequisites   : All already installed (nothing to install)" -ForegroundColor Green
    }
    Write-Host "  AAH Harness     : Will install/update (global uv tool)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Proceed with AAH harness installation? (yes/no)" -ForegroundColor Yellow
    $confirm = ""
    while ($confirm -notin @("yes","no","y","n")) {
        $confirm = (Read-Host -Prompt "    >").Trim().ToLower()
    }

    if ($confirm -in @("no","n")) {
        Write-Host "`n  Installation cancelled.`n" -ForegroundColor Yellow
        exit 0
    }
    Write-Host ""
}

# ================================================================
#  MAIN EXECUTION
# ================================================================
$ErrorActionPreference = "Continue"

Write-Host "`n  AAH (Ascend Agentic Harness) Prerequisites Installer" -ForegroundColor Cyan
Write-Host "  ==================================================" -ForegroundColor Cyan
Write-Host ""

Invoke-ConfigReview
Invoke-PreflightChecks
Install-Git
Install-GitHubCli
Install-Python
Install-NodeJs
Install-Uv
Install-AwsCli
Install-ClaudeCode
Install-AahHarness
Write-SummaryReport
