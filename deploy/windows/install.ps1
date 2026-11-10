<#
.SYNOPSIS
    Installs NetworkMonitor as a Windows service (native, no Docker).

.DESCRIPTION
    Run from an elevated PowerShell (Windows PowerShell 5.1 or PowerShell 7):

        powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1

    Steps (docs/architecture.md section 8, "Native Windows"):
      1. Installs Python 3.12 and PostgreSQL 16 with winget when they are missing.
         TimescaleDB is attempted (optional). Without it the app runs in plain
         PostgreSQL mode, which is expected on Windows and fully supported.
      2. Creates "%ProgramFiles%\NetworkMonitor" (venv) and "%ProgramData%\NetworkMonitor"
         (data; ACL restricted to SYSTEM + Administrators).
      3. Creates the DB role/database "netmon" with a random password and writes
         "%ProgramData%\NetworkMonitor\netmon.env".
      4. Installs the app (wheel, or the repository checkout with the web UI built)
         into the venv and registers the "NetworkMonitor" service with NSSM
         (LocalSystem, automatic start).
      5. Opens TCP 8080 inbound in Windows Firewall, plus UDP 162 (SNMP traps) and
         UDP/TCP 514 (syslog) for the receivers, which listen only while the Traps /
         Syslog features are ON (Settings -> Features). Warns when the Windows "SNMP Trap"
         service (SNMPTRAP) or another program holds those ports.
         [3] Also UDP 2055 (NetFlow/IPFIX), 4739 (IPFIX) and 6343 (sFlow) for the flow
         receivers (Flows feature), with the same -IngestRemoteAddress restriction.
      6. Starts the service and prints the URL.

    Corporate TLS inspection: the app trusts the Windows certificate store, so a root CA
    deployed by IT (group policy) works with no action. Certificates copied into
    deploy\certs (see deploy\certs\README.md) are trusted in addition by the web UI build
    (npm), pip and the service.

    The script is idempotent: re-running it upgrades the app and keeps the data,
    the database and the generated passwords.

    Kerberos single sign-on (Windows integrated login) needs MIT krb5 and is available only
    with the Docker / Linux installation: on this service the "Kerberos SSO" feature reports
    "unavailable". Sign in with Microsoft (Entra ID / OIDC), config backup over SSH, flows and
    all other phase-3 features work here.

.PARAMETER Port
    HTTP port of the web UI / API. Default 8080 (or the port of an existing install).

.PARAMETER Wheel
    Path to a netmon-*.whl to install (with the web UI bundled). By default the script
    uses a wheel next to it (release zip) or the repository checkout it lives in.

.PARAMETER SourceDir
    Repository checkout to install from (contains backend\ and frontend\).

.PARAMETER PostgresSuperPassword
    Password of the "postgres" superuser of an EXISTING PostgreSQL installation.
    Not needed when this script installs PostgreSQL itself (it then generates and
    stores one). If omitted and needed, you are prompted.

.PARAMETER SkipTimescale
    Do not try to install TimescaleDB.

.PARAMETER TimescaleVersion
    TimescaleDB release to try (Windows zip from GitHub releases).

.PARAMETER NssmPath
    Path to an nssm.exe to use instead of the bundled / downloaded one.

.PARAMETER IngestRemoteAddress
    Who may send SNMP traps / syslog / flow export (NetFlow, IPFIX, sFlow) through Windows
    Firewall: "Any" (default), "LocalSubnet", or a list of addresses / networks, e.g.
    10.0.0.0/8,192.168.10.0/24.

.PARAMETER DisableWindowsSnmpTrap
    Stop the Windows "SNMP Trap" service (SNMPTRAP) and set it to Disabled, so that
    NetworkMonitor can receive traps on UDP 162. Without it the installer only warns.

.PARAMETER HttpsProxy
    Outbound HTTPS proxy for the service (Telegram, Teams, Sign in with Microsoft, webhooks),
    e.g. http://proxy.corp.local:8080. Default: the value from an earlier run, else
    HTTPS_PROXY of this PowerShell session. SSH, SNMP, flows and LDAP never use it. The proxy
    must allow long requests (>= 70 s) to api.telegram.org (Telegram long polling).

.PARAMETER NoProxy
    Extra NO_PROXY entries for the service (internal webhook targets), e.g.
    .corp.local,10.0.0.0/8. localhost and 127.0.0.1 are always included.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1 -Port 8081
#>
#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 0,
    [string]$Wheel = '',
    [string]$SourceDir = '',
    [string]$PostgresSuperPassword = '',
    [switch]$SkipTimescale,
    [string]$TimescaleVersion = '2.31.0',
    [string]$NssmPath = '',
    [string[]]$IngestRemoteAddress = @('Any'),
    [switch]$DisableWindowsSnmpTrap,
    [string]$HttpsProxy = '',
    [string]$NoProxy = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# ----------------------------------------------------------------------------- constants
$ServiceName   = 'NetworkMonitor'
$InstallDir    = Join-Path $env:ProgramFiles 'NetworkMonitor'
$VenvDir       = Join-Path $InstallDir 'venv'
$DataDir       = Join-Path $env:ProgramData 'NetworkMonitor'
$LogDir        = Join-Path $DataDir 'logs'
$EnvFile       = Join-Path $DataDir 'netmon.env'
$PgSuperFile   = Join-Path $DataDir 'postgres-superuser.secret'  # only when we installed PostgreSQL
$DbName        = 'netmon'
$DbUser        = 'netmon'
$PgMajor       = 16
$PgWingetId    = "PostgreSQL.PostgreSQL.$PgMajor"
$PyWingetId    = 'Python.Python.3.12'
$FirewallRule  = 'NetworkMonitor-HTTP'
# [2b-3] Receiver rules (removed by uninstall.ps1): name -> protocol, port.
$IngestRules   = @(
    @{ Name = 'NetworkMonitor-Traps';      Protocol = 'UDP'; Port = 162; Display = 'NetworkMonitor SNMP traps (UDP 162)' },
    @{ Name = 'NetworkMonitor-Syslog-UDP'; Protocol = 'UDP'; Port = 514; Display = 'NetworkMonitor syslog (UDP 514)' },
    @{ Name = 'NetworkMonitor-Syslog-TCP'; Protocol = 'TCP'; Port = 514; Display = 'NetworkMonitor syslog (TCP 514)' },
    # [3] Flow receivers (Flows feature).
    @{ Name = 'NetworkMonitor-NetFlow';    Protocol = 'UDP'; Port = 2055; Display = 'NetworkMonitor NetFlow/IPFIX (UDP 2055)' },
    @{ Name = 'NetworkMonitor-IPFIX';      Protocol = 'UDP'; Port = 4739; Display = 'NetworkMonitor IPFIX (UDP 4739)' },
    @{ Name = 'NetworkMonitor-sFlow';      Protocol = 'UDP'; Port = 6343; Display = 'NetworkMonitor sFlow (UDP 6343)' }
)
$NssmUrls      = @('https://nssm.cc/ci/nssm-2.24-101-g897c7ad.zip', 'https://nssm.cc/release/nssm-2.24.zip')
$SidSystem     = '*S-1-5-18'       # NT AUTHORITY\SYSTEM (SIDs: independent of the UI language)
$SidAdmins     = '*S-1-5-32-544'   # BUILTIN\Administrators
$Utf8NoBom     = New-Object System.Text.UTF8Encoding($false)
$RepoRoot      = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$CertsDir      = Join-Path $PSScriptRoot '..\certs'        # deploy\certs (corporate CAs, optional)
$CorpCaFile    = Join-Path $DataDir 'corporate-ca.pem'      # NETMON_EXTRA_CA_FILE of the service
$WorkDir       = Join-Path $env:TEMP ("netmon-install-" + [guid]::NewGuid().ToString('N').Substring(0, 8))

# ----------------------------------------------------------------------------- helpers
function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message)   { Write-Host "  ok $Message" -ForegroundColor Green }
function Write-Warn2([string]$Message) { Write-Warning $Message }

function Invoke-Native {
    # Runs a native command, throws on a non-zero exit code. Output goes to the host.
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$Arguments = @(), [int[]]$OkCodes = @(0))
    & $FilePath @Arguments
    if ($OkCodes -notcontains $LASTEXITCODE) {
        throw "'$FilePath $($Arguments -join ' ')' failed with exit code $LASTEXITCODE"
    }
}

function Sync-SessionPath {
    # winget installers change the machine PATH; make it visible to this session.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ';'
}

function Get-RandomHex([int]$Bytes = 24) {
    # Hex only: safe in a URL and in an SQL literal without escaping.
    $buffer = New-Object byte[] $Bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return -join ($buffer | ForEach-Object { $_.ToString('x2') })
}

function Write-TextFile([string]$Path, [string]$Text) {
    # UTF-8 without BOM (Set-Content -Encoding UTF8 adds a BOM on Windows PowerShell 5.1).
    [System.IO.File]::WriteAllText($Path, $Text, $Utf8NoBom)
}

function Protect-Path([string]$Path) {
    # SYSTEM + Administrators only (full control), no inheritance from the parent.
    Invoke-Native icacls.exe @($Path, '/inheritance:r', '/grant:r', "${SidSystem}:(OI)(CI)F", "${SidAdmins}:(OI)(CI)F", '/Q') | Out-Null
}

function Test-Winget {
    if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
        throw ("winget is not available. Install 'App Installer' from the Microsoft Store " +
               "(or on Windows Server install winget manually), then re-run this script.")
    }
}

function Install-WingetPackage([string]$Id, [string]$Override = '') {
    Test-Winget
    $wingetArgs = @('install', '--exact', '--id', $Id, '--source', 'winget', '--silent',
                    '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity',
                    '--scope', 'machine')
    if ($Override) { $wingetArgs += @('--override', $Override) }
    & winget.exe @wingetArgs
    # -1978335189 (0x8A15002B) = no applicable upgrade / already installed
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {
        throw "winget install $Id failed with exit code $LASTEXITCODE"
    }
    Sync-SessionPath
}

function ConvertTo-Pem([System.Security.Cryptography.X509Certificates.X509Certificate2]$Cert) {
    $b64 = [Convert]::ToBase64String($Cert.RawData)
    $lines = for ($i = 0; $i -lt $b64.Length; $i += 64) { $b64.Substring($i, [Math]::Min(64, $b64.Length - $i)) }
    return "-----BEGIN CERTIFICATE-----`n" + ($lines -join "`n") + "`n-----END CERTIFICATE-----`n"
}

function Read-CorporateCa([string]$Dir) {
    # Certificates from deploy\certs\*.crt|*.pem|*.cer, PEM (Base-64, one or more per file)
    # or DER. Returns X509Certificate2 objects; unreadable files are skipped with a warning.
    $certs = New-Object System.Collections.Generic.List[System.Security.Cryptography.X509Certificates.X509Certificate2]
    if (-not (Test-Path $Dir)) { return ,$certs }
    $files = Get-ChildItem -Path $Dir -File -ErrorAction SilentlyContinue |
             Where-Object { @('.crt', '.pem', '.cer') -contains $_.Extension.ToLowerInvariant() }
    foreach ($f in $files) {
        $before = $certs.Count
        try {
            $bytes = [System.IO.File]::ReadAllBytes($f.FullName)
            $text = [System.Text.Encoding]::ASCII.GetString($bytes)
            $blocks = [regex]::Matches($text, '-----BEGIN CERTIFICATE-----([A-Za-z0-9+/=\s]+?)-----END CERTIFICATE-----')
            if ($blocks.Count -gt 0) {
                foreach ($b in $blocks) {
                    $der = [Convert]::FromBase64String(($b.Groups[1].Value -replace '\s', ''))
                    $certs.Add((New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(, $der)))
                }
            } else {
                $certs.Add((New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(, $bytes)))
            }
        } catch {
            Write-Warn2 "Skipping $($f.Name): not a PEM/DER certificate ($($_.Exception.Message))"
        }
        for ($i = $before; $i -lt $certs.Count; $i++) { Write-Ok "Corporate CA: $($f.Name) -> $($certs[$i].Subject)" }
    }
    return ,$certs
}

function Get-EnvFileValue([string]$Key) {
    if (-not (Test-Path $EnvFile)) { return $null }
    foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
        if ($line -match "^\s*$([regex]::Escape($Key))=(.*)$") { return $Matches[1].Trim() }
    }
    return $null
}

# ============================================================================= start
New-Item -ItemType Directory -Force -Path $DataDir, $LogDir, $WorkDir | Out-Null
$transcript = Join-Path $LogDir 'install.log'
try { Start-Transcript -Path $transcript -Append | Out-Null } catch { }

try {
    Write-Step "Installing NetworkMonitor (data: $DataDir, program: $InstallDir)"
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'A 64-bit Windows is required.' }
    Protect-Path $DataDir

    # ========================================================================= corporate CA
    # A TLS-inspecting firewall re-signs HTTPS with a corporate root CA. The app uses the
    # Windows certificate store (truststore), so a CA deployed by IT is trusted as is.
    # Certificates dropped into deploy\certs are trusted in addition: by npm (web UI build),
    # pip and the service (NETMON_EXTRA_CA_FILE). See deploy\certs\README.md.
    $corpCerts = Read-CorporateCa $CertsDir
    $useCorpCa = $corpCerts.Count -gt 0
    if ($useCorpCa) {
        Write-Step "Trusting $($corpCerts.Count) corporate CA certificate(s) from deploy\certs"
        $pem = ($corpCerts | ForEach-Object { ConvertTo-Pem $_ }) -join ''
        Write-TextFile $CorpCaFile $pem
        $env:NODE_EXTRA_CA_CERTS = $CorpCaFile
    } elseif (Test-Path $CorpCaFile) {
        Remove-Item -Force $CorpCaFile   # certificates were removed from deploy\certs
    }

    # ========================================================================= Python
    function Find-Python {
        # Returns the path of a python.exe >= 3.12, or $null.
        # Windows PowerShell 5.1 turns native stderr output into terminating errors when
        # it is redirected and ErrorActionPreference is Stop, so relax it in this scope.
        $ErrorActionPreference = 'Continue'
        $candidates = New-Object System.Collections.Generic.List[string]
        foreach ($v in @('3.13', '3.12')) {
            if (Get-Command py.exe -ErrorAction SilentlyContinue) {
                $p = & py.exe "-$v" -c 'import sys; print(sys.executable)' 2>$null
                if ($LASTEXITCODE -eq 0 -and $p) { $candidates.Add($p.Trim()) }
            }
        }
        foreach ($d in @("$env:ProgramFiles\Python313", "$env:ProgramFiles\Python312",
                         "$env:LOCALAPPDATA\Programs\Python\Python313", "$env:LOCALAPPDATA\Programs\Python\Python312")) {
            $candidates.Add((Join-Path $d 'python.exe'))
        }
        $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source -notlike '*WindowsApps*') { $candidates.Add($cmd.Source) }  # skip the Store stub
        foreach ($c in $candidates) {
            if (-not (Test-Path $c)) { continue }
            & $c -c 'import sys, venv; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { return $c }
        }
        return $null
    }

    Write-Step 'Checking Python (>= 3.12)'
    $python = Find-Python
    if (-not $python) {
        Write-Step "Installing Python 3.12 with winget ($PyWingetId)"
        Install-WingetPackage $PyWingetId '/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_test=0'
        $python = Find-Python
        if (-not $python) { throw 'Python 3.12 was installed but python.exe was not found. Open a new console and re-run.' }
    }
    Write-Ok "Python: $python ($(& $python -V))"

    # ========================================================================= PostgreSQL
    function Find-PostgresInstall {
        # Newest PostgreSQL >= 14 installed by the EDB installer: @{Major; Bin; Data; Service; Port}
        $root = 'HKLM:\SOFTWARE\PostgreSQL\Installations'
        if (-not (Test-Path $root)) { return $null }
        $found = foreach ($k in Get-ChildItem $root) {
            $p = Get-ItemProperty $k.PSPath
            $ver = 0
            if ($p.PSObject.Properties['Version']) { [void][int]::TryParse(($p.Version -split '\.')[0], [ref]$ver) }
            if ($ver -lt 14 -or -not $p.PSObject.Properties['Base Directory']) { continue }
            $svc = if ($p.PSObject.Properties['Service ID']) { $p.'Service ID' } else { "postgresql-x64-$ver" }
            $data = if ($p.PSObject.Properties['Data Directory']) { $p.'Data Directory' } else { Join-Path $p.'Base Directory' 'data' }
            $portValue = 5432
            $svcKey = "HKLM:\SOFTWARE\PostgreSQL\Services\$svc"
            if (Test-Path $svcKey) {
                $sp = Get-ItemProperty $svcKey
                if ($sp.PSObject.Properties['Port']) { $portValue = [int]$sp.Port }
            }
            [pscustomobject]@{ Major = $ver; Bin = (Join-Path $p.'Base Directory' 'bin'); Base = $p.'Base Directory';
                               Data = $data; Service = $svc; Port = $portValue }
        }
        return $found | Sort-Object Major -Descending | Select-Object -First 1
    }

    Write-Step "Checking PostgreSQL (>= 14)"
    $pg = Find-PostgresInstall
    $pgSuperPassword = $PostgresSuperPassword
    if (-not $pg) {
        # Fresh install: we choose the superuser password and keep it (ACL-protected) for re-runs.
        if (-not $pgSuperPassword) { $pgSuperPassword = Get-RandomHex 24 }
        Write-Step "Installing PostgreSQL $PgMajor with winget ($PgWingetId)"
        $override = "--mode unattended --unattendedmodeui none --superpassword $pgSuperPassword " +
                    "--servicename postgresql-x64-$PgMajor --serverport 5432 --enable-components server,commandlinetools " +
                    "--disable-components pgAdmin,stackbuilder"
        Install-WingetPackage $PgWingetId $override
        Write-TextFile $PgSuperFile $pgSuperPassword
        Protect-Path $PgSuperFile
        $pg = Find-PostgresInstall
        if (-not $pg) { throw 'PostgreSQL was installed but could not be located in the registry.' }
    }
    $psql = Join-Path $pg.Bin 'psql.exe'
    if (-not (Test-Path $psql)) { throw "psql.exe not found in $($pg.Bin)" }
    $pgService = Get-Service -Name $pg.Service -ErrorAction SilentlyContinue
    if (-not $pgService) { throw "PostgreSQL service '$($pg.Service)' not found." }
    if ($pgService.StartType -ne 'Automatic') { Set-Service -Name $pg.Service -StartupType Automatic }
    if ($pgService.Status -ne 'Running') { Start-Service -Name $pg.Service }
    Write-Ok "PostgreSQL $($pg.Major) (service $($pg.Service), port $($pg.Port))"

    if (-not $pgSuperPassword -and (Test-Path $PgSuperFile)) {
        $pgSuperPassword = ([System.IO.File]::ReadAllText($PgSuperFile)).Trim()
    }
    if (-not $pgSuperPassword) {
        $secure = Read-Host -AsSecureString "Password of the PostgreSQL superuser 'postgres'"
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $pgSuperPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }

    function Invoke-PgSuper {
        # Runs SQL as the postgres superuser. SQL is passed through stdin (never on the command line).
        param([Parameter(Mandatory)][string]$Sql, [string]$Database = 'postgres', [switch]$Scalar, [switch]$IgnoreError)
        $ErrorActionPreference = 'Continue'   # psql NOTICEs on stderr must not throw (see Find-Python)
        $env:PGPASSWORD = $pgSuperPassword
        $env:PGCLIENTENCODING = 'UTF8'
        try {
            $psqlArgs = @('-X', '-q', '-v', 'ON_ERROR_STOP=1', '-h', '127.0.0.1', '-p', "$($pg.Port)", '-U', 'postgres', '-d', $Database)
            if ($Scalar) { $psqlArgs += @('-A', '-t') }
            $out = $Sql | & $psql @psqlArgs 2>&1
            $code = $LASTEXITCODE
        } finally {
            Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
        }
        if ($code -ne 0 -and -not $IgnoreError) { throw "psql failed ($code): $out" }
        if ($Scalar) { return (($out | Out-String).Trim()) }
        return $code
    }

    function Wait-PostgresReady([int]$Seconds = 60) {
        for ($i = 0; $i -lt $Seconds; $i++) {
            try { if ((Invoke-PgSuper -Sql 'SELECT 1;' -Scalar) -eq '1') { return $true } } catch { }
            Start-Sleep -Seconds 1
        }
        return $false
    }
    if (-not (Wait-PostgresReady 60)) {
        throw ("Cannot connect to PostgreSQL as 'postgres' on 127.0.0.1:$($pg.Port). " +
               'If the password is wrong, re-run with -PostgresSuperPassword <password>.')
    }

    # ========================================================================= TimescaleDB (optional)
    $timescale = $false
    function Test-TimescaleAvailable {
        return ((Invoke-PgSuper -Sql "SELECT count(*) FROM pg_available_extensions WHERE name = 'timescaledb';" -Scalar) -eq '1')
    }
    function Install-TimescaleWindows {
        # Copies the files of the official Windows release zip into the PostgreSQL directories.
        # (The bundled setup.exe is interactive, so it is not used.)
        $zipName = "timescaledb-postgresql-$($pg.Major)-windows-amd64.zip"
        $url = "https://github.com/timescale/timescaledb/releases/download/$TimescaleVersion/$zipName"
        $zip = Join-Path $WorkDir $zipName
        $dir = Join-Path $WorkDir 'timescaledb'
        Write-Step "Downloading TimescaleDB $TimescaleVersion for PostgreSQL $($pg.Major)"
        Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
        Expand-Archive -Path $zip -DestinationPath $dir -Force
        $dlls = @(Get-ChildItem $dir -Recurse -Filter 'timescaledb*.dll')
        $ext = @(Get-ChildItem $dir -Recurse -Include 'timescaledb*.control', 'timescaledb*.sql')
        if ($dlls.Count -eq 0 -or $ext.Count -eq 0) { throw 'Unexpected TimescaleDB archive layout.' }
        $libDir = Join-Path $pg.Base 'lib'
        $extDir = Join-Path $pg.Base 'share\extension'
        $dlls | Copy-Item -Destination $libDir -Force
        $ext | Copy-Item -Destination $extDir -Force
    }
    function Enable-TimescalePreload {
        # Adds timescaledb to shared_preload_libraries and restarts PostgreSQL.
        # If the server does not come back, the change is reverted.
        $current = Invoke-PgSuper -Sql 'SHOW shared_preload_libraries;' -Scalar
        if (($current -replace '\s', '') -split ',' -contains 'timescaledb') { return $true }
        $new = if ($current) { "$current, timescaledb" } else { 'timescaledb' }
        Invoke-PgSuper -Sql "ALTER SYSTEM SET shared_preload_libraries = '$new';" | Out-Null
        Restart-Service -Name $pg.Service -Force
        if (Wait-PostgresReady 60) { return $true }
        Write-Warn2 'PostgreSQL did not start with TimescaleDB preloaded; reverting.'
        $auto = Join-Path $pg.Data 'postgresql.auto.conf'
        $lines = [System.IO.File]::ReadAllLines($auto) | Where-Object { $_ -notmatch '^\s*shared_preload_libraries' }
        [System.IO.File]::WriteAllLines($auto, [string[]]$lines, $Utf8NoBom)
        Start-Service -Name $pg.Service
        if (-not (Wait-PostgresReady 60)) { throw "PostgreSQL does not start any more. Check $($pg.Data)\log." }
        return $false
    }

    if (-not $SkipTimescale) {
        Write-Step 'Trying TimescaleDB (optional)'
        try {
            if (-not (Test-TimescaleAvailable)) { Install-TimescaleWindows }
            if ((Test-TimescaleAvailable) -and (Enable-TimescalePreload)) {
                $timescale = $true
                Write-Ok 'TimescaleDB enabled'
            }
        } catch {
            Write-Warn2 "TimescaleDB not installed ($($_.Exception.Message))."
        }
        if (-not $timescale) { Write-Warn2 'NetworkMonitor will use plain PostgreSQL mode (expected on Windows).' }
    }

    # ========================================================================= database
    Write-Step 'Configuring the database'
    $dbPassword = $null
    $existingUrl = Get-EnvFileValue 'DATABASE_URL'
    if ($existingUrl -and $existingUrl -match '^postgres[^:]*://[^:/@]+:([0-9A-Za-z_-]+)@') { $dbPassword = $Matches[1] }
    if (-not $dbPassword) { $dbPassword = Get-RandomHex 24 }

    $roleExists = (Invoke-PgSuper -Sql "SELECT count(*) FROM pg_roles WHERE rolname = '$DbUser';" -Scalar) -eq '1'
    $verb = if ($roleExists) { 'ALTER' } else { 'CREATE' }
    Invoke-PgSuper -Sql ("SET password_encryption = 'scram-sha-256';`n" +
                         "$verb ROLE $DbUser WITH LOGIN PASSWORD '$dbPassword';") | Out-Null
    $dbExists = (Invoke-PgSuper -Sql "SELECT count(*) FROM pg_database WHERE datname = '$DbName';" -Scalar) -eq '1'
    if (-not $dbExists) {
        Invoke-PgSuper -Sql "CREATE DATABASE $DbName OWNER $DbUser ENCODING 'UTF8' TEMPLATE template0;" | Out-Null
    }
    Invoke-PgSuper -Database $DbName -Sql "GRANT ALL ON SCHEMA public TO $DbUser;" | Out-Null
    if ($timescale) {
        $rc = Invoke-PgSuper -Database $DbName -Sql 'CREATE EXTENSION IF NOT EXISTS timescaledb;' -IgnoreError
        if ($rc -ne 0) { Write-Warn2 'CREATE EXTENSION timescaledb failed; plain PostgreSQL mode will be used.' }
    }
    # [2b-3] pg_trgm (contrib, shipped with the EDB installer) indexes the syslog/trap text search.
    $rc = Invoke-PgSuper -Database $DbName -Sql 'CREATE EXTENSION IF NOT EXISTS pg_trgm;' -IgnoreError
    if ($rc -ne 0) { Write-Warn2 'CREATE EXTENSION pg_trgm failed; log search will be slower.' }
    Write-Ok "Database '$DbName' ready"

    # ========================================================================= netmon.env
    if ($Port -eq 0) {
        $existingPort = Get-EnvFileValue 'NETMON_PORT'
        $Port = if ($existingPort -match '^\d+$') { [int]$existingPort } else { 8080 }
    }
    $envText = "# Generated by the NetworkMonitor installer on $((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')). Do not edit:`n" +
               "# re-run install.ps1 to change it.`n" +
               "DATABASE_URL=postgresql://${DbUser}:${dbPassword}@127.0.0.1:$($pg.Port)/$DbName`n" +
               "NETMON_PORT=$Port`n"
    Write-TextFile $EnvFile $envText
    Protect-Path $EnvFile
    Write-Ok "Wrote $EnvFile"

    # ========================================================================= app package
    Write-Step 'Preparing the application package'
    $pkg = $null
    $spaDir = $null
    if (-not $Wheel -and -not $SourceDir) {
        # Release zip layout: <root>\netmon-*.whl next to deploy\windows\install.ps1
        $w = Get-ChildItem -Path $RepoRoot -Filter 'netmon-*.whl' -ErrorAction SilentlyContinue |
             Sort-Object Name | Select-Object -Last 1
        if ($w) { $Wheel = $w.FullName } else { $SourceDir = $RepoRoot }
    }
    if ($Wheel) {
        if (-not (Test-Path $Wheel)) { throw "Wheel not found: $Wheel" }
        $pkg = (Resolve-Path $Wheel).Path
        Write-Ok "Wheel: $pkg"
    } else {
        $SourceDir = (Resolve-Path $SourceDir).Path
        if (-not (Test-Path (Join-Path $SourceDir 'backend\pyproject.toml'))) {
            throw "No backend\pyproject.toml under $SourceDir. Use -Wheel or -SourceDir."
        }
        # Stage a clean copy of the backend (+ README.md, the package readme "../README.md").
        $stage = Join-Path $WorkDir 'stage'
        New-Item -ItemType Directory -Force -Path $stage | Out-Null
        Invoke-Native robocopy.exe @((Join-Path $SourceDir 'backend'), (Join-Path $stage 'backend'), '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP',
                                     '/XD', '.venv', '__pycache__', '.pytest_cache', '.ruff_cache',
                                     (Join-Path $SourceDir 'backend\build'), (Join-Path $SourceDir 'backend\dist'),
                                     (Join-Path $SourceDir 'backend\netmon\static'),
                                     '/XF', '*.pyc') -OkCodes @(0, 1, 2, 3, 4, 5, 6, 7)
        $readme = Join-Path $SourceDir 'README.md'
        if (Test-Path $readme) { Copy-Item $readme $stage }

        # Web UI: build with Node.js >= 18 if present, otherwise use a prebuilt copy.
        $frontend = Join-Path $SourceDir 'frontend'
        $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
        $node = Get-Command node.exe -ErrorAction SilentlyContinue
        $prebuilt = (Test-Path (Join-Path $frontend 'dist\index.html')) -or
                    (Test-Path (Join-Path $SourceDir 'backend\netmon\static\index.html'))
        if ((Test-Path (Join-Path $frontend 'package.json')) -and -not ($npm -and $node) -and -not $prebuilt) {
            # A fresh git clone has no built UI (frontend\dist is not committed): get Node.js LTS.
            Write-Step 'Installing Node.js LTS with winget (to build the web UI)'
            try {
                Install-WingetPackage 'OpenJS.NodeJS.LTS'
                $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
                $node = Get-Command node.exe -ErrorAction SilentlyContinue
            } catch {
                Write-Warn2 "Node.js could not be installed: $($_.Exception.Message)"
            }
        }
        if ((Test-Path (Join-Path $frontend 'package.json')) -and $npm -and $node) {
            $nodeMajor = [int]((& $node.Source -p "process.versions.node.split('.')[0]") | Out-String).Trim()
            if ($nodeMajor -ge 18) {
                Write-Step "Building the web UI with Node.js $nodeMajor"
                $feStage = Join-Path $WorkDir 'frontend'
                Invoke-Native robocopy.exe @($frontend, $feStage, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP',
                                             '/XD', (Join-Path $frontend 'node_modules'), (Join-Path $frontend 'dist')) -OkCodes @(0, 1, 2, 3, 4, 5, 6, 7)
                Push-Location $feStage
                try {
                    if (Test-Path 'package-lock.json') { Invoke-Native $npm.Source @('ci', '--no-audit', '--no-fund') }
                    else { Invoke-Native $npm.Source @('install', '--no-audit', '--no-fund') }
                    Invoke-Native $npm.Source @('run', 'build')
                    if (Test-Path (Join-Path $feStage 'dist\index.html')) { $spaDir = Join-Path $feStage 'dist' }
                } catch {
                    Write-Warn2 "Building the web UI failed: $($_.Exception.Message)"
                } finally { Pop-Location }
            }
        }
        if (-not $spaDir) {
            foreach ($d in @((Join-Path $frontend 'dist'), (Join-Path $SourceDir 'backend\netmon\static'))) {
                if (Test-Path (Join-Path $d 'index.html')) { $spaDir = $d; break }
            }
        }
        $stageStatic = Join-Path $stage 'backend\netmon\static'
        if ($spaDir) {
            if (Test-Path $stageStatic) { Remove-Item -Recurse -Force $stageStatic }
            Copy-Item -Recurse $spaDir $stageStatic
            $spaDir = $stageStatic
            Write-Ok 'Web UI bundled'
        } else {
            Write-Warn2 'No web UI found (install Node.js >= 18 or use a release zip). The API works, the UI will not.'
        }
        $pkg = Join-Path $stage 'backend'
    }

    # ========================================================================= stop old service (upgrade)
    $nssm = $null
    $existingSvc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existingSvc -and $existingSvc.Status -ne 'Stopped') {
        Write-Step 'Stopping the running NetworkMonitor service (upgrade)'
        Stop-Service -Name $ServiceName -Force
        $existingSvc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }

    # ========================================================================= venv + install
    Write-Step "Installing into $VenvDir"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $venvPython = Join-Path $VenvDir 'Scripts\python.exe'
    if (Test-Path $venvPython) {
        & $venvPython -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
        if ($LASTEXITCODE -ne 0) { Write-Warn2 'Recreating the venv (unsupported Python).'; Remove-Item -Recurse -Force $VenvDir }
    }
    if (-not (Test-Path $venvPython)) { Invoke-Native $python @('-m', 'venv', $VenvDir) }
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $env:PIP_NO_CACHE_DIR = '1'
    if ($useCorpCa) {
        # pip: its own Mozilla bundle (certifi, vendored in pip) + the corporate CAs.
        $pipCertifi = (& $venvPython -c 'from pip._vendor import certifi; print(certifi.where())' | Out-String).Trim()
        if ($pipCertifi -and (Test-Path $pipCertifi)) {
            $pipBundle = Join-Path $WorkDir 'pip-ca-bundle.pem'
            Write-TextFile $pipBundle (([System.IO.File]::ReadAllText($pipCertifi)) + "`n" + ([System.IO.File]::ReadAllText($CorpCaFile)))
            $env:PIP_CERT = $pipBundle
        }
    }
    Invoke-Native $venvPython @('-m', 'pip', 'install', '-q', '--upgrade', 'pip', 'setuptools', 'wheel')
    Invoke-Native $venvPython @('-m', 'pip', 'install', '-q', '--upgrade', $pkg)
    Invoke-Native $venvPython @('-m', 'pip', 'install', '-q', '--no-deps', '--force-reinstall', $pkg)
    $netmonExe = Join-Path $VenvDir 'Scripts\netmon.exe'
    if (-not (Test-Path $netmonExe)) { throw "netmon.exe was not installed ($netmonExe)." }

    # The SPA must sit next to the installed package (NETMON_FRONTEND_DIST default).
    $pkgDir = (& $venvPython -c 'import netmon, pathlib; print(pathlib.Path(netmon.__file__).resolve().parent)' | Out-String).Trim()
    $installedStatic = Join-Path $pkgDir 'static'
    if ($spaDir -and -not (Test-Path (Join-Path $installedStatic 'index.html'))) {
        if (Test-Path $installedStatic) { Remove-Item -Recurse -Force $installedStatic }
        Copy-Item -Recurse $spaDir $installedStatic
    }
    $appVersion = (& $venvPython -c 'import netmon; print(netmon.__version__)' | Out-String).Trim()
    Write-Ok "netmon $appVersion installed"

    # ========================================================================= NSSM
    Write-Step 'Preparing NSSM (service wrapper)'
    $nssmTarget = Join-Path $InstallDir 'nssm.exe'
    $nssmCandidates = @($NssmPath, (Join-Path $PSScriptRoot 'nssm.exe'), (Join-Path $PSScriptRoot 'win64\nssm.exe')) |
                      Where-Object { $_ -and (Test-Path $_) }
    if ($nssmCandidates) {
        $src = (Resolve-Path ($nssmCandidates | Select-Object -First 1)).Path
        if ($src -ne $nssmTarget) { Copy-Item $src $nssmTarget -Force }
    } elseif (-not (Test-Path $nssmTarget)) {
        $downloaded = $false
        foreach ($u in $NssmUrls) {
            try {
                $zip = Join-Path $WorkDir 'nssm.zip'
                Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile $zip -TimeoutSec 60
                $nssmDir = Join-Path $WorkDir 'nssm'
                Expand-Archive -Path $zip -DestinationPath $nssmDir -Force
                $exe = Get-ChildItem $nssmDir -Recurse -Filter 'nssm.exe' | Where-Object { $_.DirectoryName -like '*win64' } | Select-Object -First 1
                if ($exe) { Copy-Item $exe.FullName $nssmTarget -Force; $downloaded = $true; break }
            } catch {
                Write-Warn2 "NSSM download from $u failed: $($_.Exception.Message)"
            }
        }
        if (-not $downloaded) {
            Write-Step 'Installing NSSM with winget'
            Install-WingetPackage 'NSSM.NSSM'
            $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
            if (-not $cmd) { throw 'NSSM could not be obtained. Put nssm.exe next to install.ps1 and re-run.' }
            Copy-Item $cmd.Source $nssmTarget -Force
        }
    }
    $nssm = $nssmTarget
    Write-Ok "NSSM: $nssm"

    # ========================================================================= service
    Write-Step "Registering the '$ServiceName' service"
    if (-not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
        Invoke-Native $nssm @('install', $ServiceName, $netmonExe, 'serve') | Out-Null
    }
    $serviceLog = Join-Path $LogDir 'service.log'
    # [3] Outbound proxy (parameter > earlier run > this session's HTTPS_PROXY). NSSM keeps
    # the service environment in the registry (REG_MULTI_SZ AppEnvironmentExtra).
    $proxyEnv = @()
    $prevEnv = @()
    try {
        $prevEnv = @((Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters" `
                       -Name AppEnvironmentExtra -ErrorAction Stop).AppEnvironmentExtra)
    } catch { }
    $prevProxy = @($prevEnv | Where-Object { $_ -like 'HTTPS_PROXY=*' } | ForEach-Object { $_.Substring(12) }) | Select-Object -First 1
    $prevNoProxy = @($prevEnv | Where-Object { $_ -like 'NO_PROXY=*' } | ForEach-Object { $_.Substring(9) }) | Select-Object -First 1
    $proxy = ''
    if ($HttpsProxy) { $proxy = $HttpsProxy }
    elseif ($prevProxy) { $proxy = $prevProxy }
    elseif ($env:HTTPS_PROXY) { $proxy = $env:HTTPS_PROXY }
    if ($proxy) {
        $noProxyList = 'localhost,127.0.0.1,::1'
        if ($NoProxy) { $noProxyList = "$noProxyList,$NoProxy" }
        elseif ($prevNoProxy) { $noProxyList = $prevNoProxy }
        elseif ($env:NO_PROXY) { $noProxyList = "$noProxyList,$($env:NO_PROXY)" }
        $proxyEnv = @("HTTPS_PROXY=$proxy", "HTTP_PROXY=$proxy", "NO_PROXY=$noProxyList")
        Write-Ok "Service outbound proxy: $(($proxy -split '://')[0])://... (NO_PROXY=$noProxyList)"
    }
    $settings = @(
        @('Application', $netmonExe),
        @('AppParameters', 'serve'),
        @('AppDirectory', $DataDir),
        (@('AppEnvironmentExtra', "NETMON_DATA_DIR=$DataDir", 'PYTHONUNBUFFERED=1', 'PYTHONIOENCODING=utf-8') +
         @(if ($useCorpCa) { "NETMON_EXTRA_CA_FILE=$CorpCaFile" }) + $proxyEnv),
        @('DisplayName', 'NetworkMonitor'),
        @('Description', "NetworkMonitor - network monitoring web application (http://localhost:$Port)"),
        @('Start', 'SERVICE_AUTO_START'),
        @('ObjectName', 'LocalSystem'),
        @('DependOnService', $pg.Service),
        @('AppStdout', $serviceLog),
        @('AppStderr', $serviceLog),
        @('AppRotateFiles', '1'),
        @('AppRotateOnline', '1'),
        @('AppRotateBytes', '10485760'),
        @('AppStopMethodSkip', '0'),
        @('AppStopMethodConsole', '15000'),
        @('AppExit', 'Default', 'Restart'),
        @('AppRestartDelay', '5000'),
        @('AppThrottle', '10000')
    )
    foreach ($s in $settings) {
        # nssm prints UTF-16 text; suppress it and rely on the exit code.
        Invoke-Native $nssm (@('set', $ServiceName) + $s) | Out-Null
    }
    # Exit code 3 = another instance holds the DB lock: do not restart in a loop.
    Invoke-Native $nssm @('set', $ServiceName, 'AppExit', '3', 'Exit') | Out-Null

    # ========================================================================= firewall
    Write-Step "Opening TCP $Port in Windows Firewall"
    $rule = Get-NetFirewallRule -Name $FirewallRule -ErrorAction SilentlyContinue
    if ($rule) {
        Set-NetFirewallRule -Name $FirewallRule -Enabled True -Action Allow -Direction Inbound -Profile Any
        Get-NetFirewallRule -Name $FirewallRule | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port
    } else {
        New-NetFirewallRule -Name $FirewallRule -DisplayName "NetworkMonitor (TCP $Port)" -Direction Inbound `
            -Protocol TCP -LocalPort $Port -Action Allow -Profile Any | Out-Null
    }
    # Echo replies for the ping checks are inbound ICMP; Windows Firewall allows the replies to
    # our own requests automatically, so no ICMP rule is needed.

    # [2b-3] SNMP traps / syslog, [3] flows. Nothing listens until the Traps / Syslog / Flows
    # features are turned on in Settings -> Features; the rules only let the datagrams in.
    Write-Step ("Opening UDP 162 (traps), UDP/TCP 514 (syslog) and UDP 2055/4739/6343 (flows) in Windows Firewall " +
                "(from: $($IngestRemoteAddress -join ', '))")
    foreach ($r in $IngestRules) {
        if (Get-NetFirewallRule -Name $r.Name -ErrorAction SilentlyContinue) {
            Set-NetFirewallRule -Name $r.Name -Enabled True -Action Allow -Direction Inbound -Profile Any `
                -RemoteAddress $IngestRemoteAddress
            Get-NetFirewallRule -Name $r.Name | Get-NetFirewallPortFilter |
                Set-NetFirewallPortFilter -Protocol $r.Protocol -LocalPort $r.Port
        } else {
            New-NetFirewallRule -Name $r.Name -DisplayName $r.Display -Direction Inbound -Protocol $r.Protocol `
                -LocalPort $r.Port -RemoteAddress $IngestRemoteAddress -Action Allow -Profile Any | Out-Null
        }
    }

    # The Windows "SNMP Trap" service holds UDP 162 while it runs.
    $snmpTrap = Get-Service -Name 'SNMPTRAP' -ErrorAction SilentlyContinue
    if ($snmpTrap) {
        $snmpTrapCim = Get-CimInstance Win32_Service -Filter "Name='SNMPTRAP'" -ErrorAction SilentlyContinue
        $snmpTrapStart = if ($snmpTrapCim) { [string]$snmpTrapCim.StartMode } else { 'unknown' }
        if ($DisableWindowsSnmpTrap) {
            if ($snmpTrap.Status -ne 'Stopped') {
                Write-Step 'Stopping the Windows SNMP Trap service (SNMPTRAP) - it holds UDP 162'
                Stop-Service -Name 'SNMPTRAP' -Force
            }
            Set-Service -Name 'SNMPTRAP' -StartupType Disabled
            Write-Ok 'Windows SNMP Trap service stopped and disabled'
        } elseif ($snmpTrap.Status -eq 'Running' -or $snmpTrapStart -eq 'Auto') {
            Write-Warn2 ("The Windows 'SNMP Trap' service (SNMPTRAP) is $($snmpTrap.Status.ToString().ToLower()) " +
                         "(start: $snmpTrapStart). While it runs it holds UDP 162 and NetworkMonitor cannot receive traps. " +
                         "Stop it with: Stop-Service SNMPTRAP; Set-Service SNMPTRAP -StartupType Disabled " +
                         "(or re-run this installer with -DisableWindowsSnmpTrap).")
        }
    }

    # ========================================================================= start + health
    Write-Step 'Starting the service'
    Start-Service -Name $ServiceName
    $healthy = $false
    for ($i = 0; $i -lt 90; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3
            if ($r.StatusCode -eq 200) { $healthy = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
    }
    if (-not $healthy) {
        if (Test-Path $serviceLog) { Get-Content $serviceLog -Tail 40 | Write-Host }
        throw "NetworkMonitor did not become healthy on port $Port. See $serviceLog"
    }

    $ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
             Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
             Select-Object -ExpandProperty IPAddress)
    $hostShown = if ($ips.Count -gt 0) { $ips[0] } else { $env:COMPUTERNAME }
    Write-Host ''
    Write-Host "NetworkMonitor $appVersion is running." -ForegroundColor Green
    Write-Host "  Open:     http://${hostShown}:$Port   (complete the setup wizard on first visit)"
    Write-Host "  Health:   http://${hostShown}:$Port/api/health"
    Write-Host "  Service:  Get-Service $ServiceName   |   logs: $LogDir"
    Write-Host "  Data:     $DataDir"
    if (-not $timescale) { Write-Host '  Storage:  plain PostgreSQL mode (TimescaleDB not installed)' }
    Write-Host "  Traps/syslog: turn them on in Settings -> Features, then point the devices at ${hostShown}"
    Write-Host '                (UDP 162 traps, UDP/TCP 514 syslog; examples in deploy\README.md).'
    Write-Host '  Flows:    turn on "Flows" in Settings -> Features, then configure flow export on the devices'
    Write-Host '            (UDP 2055 NetFlow/IPFIX, 4739 IPFIX, 6343 sFlow; examples in deploy\README.md).'
    Write-Host '  SSO:      Kerberos (Windows integrated login) needs the Docker/Linux installation;'
    Write-Host '            "Sign in with Microsoft" (Entra ID) works here.'

    # [2b-3] Other programs listening on 162 / 514, [3] 2055 / 4739 / 6343 (the service's own
    # processes excluded).
    $own = @{}
    $svcCim = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
    $svcPid = if ($svcCim) { [int]$svcCim.ProcessId } else { 0 }
    if ($svcPid -gt 0) {
        $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
        $own[[int]$svcPid] = $true
        do {
            $added = $false
            foreach ($p in $procs) {
                if ($own.ContainsKey([int]$p.ParentProcessId) -and -not $own.ContainsKey([int]$p.ProcessId)) {
                    $own[[int]$p.ProcessId] = $true; $added = $true
                }
            }
        } while ($added)
    }
    $holders = @()
    $holders += @(Get-NetUDPEndpoint -LocalPort 162, 514, 2055, 4739, 6343 -ErrorAction SilentlyContinue |
                  ForEach-Object { [pscustomobject]@{ Proto = 'UDP'; Port = $_.LocalPort; ProcId = $_.OwningProcess } })
    $holders += @(Get-NetTCPConnection -LocalPort 514 -State Listen -ErrorAction SilentlyContinue |
                  ForEach-Object { [pscustomobject]@{ Proto = 'TCP'; Port = $_.LocalPort; ProcId = $_.OwningProcess } })
    foreach ($h in ($holders | Where-Object { -not $own.ContainsKey([int]$_.ProcId) } | Sort-Object Proto, Port, ProcId -Unique)) {
        $proc = Get-Process -Id $h.ProcId -ErrorAction SilentlyContinue
        $name = if ($proc) { $proc.ProcessName } else { '?' }
        Write-Warn2 ("$($h.Proto) $($h.Port) is already used by process $($h.ProcId) ($name): NetworkMonitor cannot " +
                     'receive there while it runs. Stop / reconfigure that program (svchost on UDP 162 = SNMPTRAP).')
    }
}
catch {
    Write-Host ''
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Details are in $transcript"
    exit 1
}
finally {
    Remove-Item -Recurse -Force $WorkDir -ErrorAction SilentlyContinue
    try { Stop-Transcript | Out-Null } catch { }
}
