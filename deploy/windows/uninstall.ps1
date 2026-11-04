<#
.SYNOPSIS
    Removes the NetworkMonitor Windows service and program files.

.DESCRIPTION
    Run from an elevated PowerShell:

        powershell -ExecutionPolicy Bypass -File .\deploy\windows\uninstall.ps1          # keep data + DB
        powershell -ExecutionPolicy Bypass -File .\deploy\windows\uninstall.ps1 -Purge   # delete everything

    Without -Purge the data directory (%ProgramData%\NetworkMonitor) and the "netmon"
    database are kept, so a later install.ps1 picks them up again.
    PostgreSQL and Python are NOT uninstalled (other software may use them); remove them
    from "Apps & features" / winget if you no longer need them.

.PARAMETER Purge
    Also drop the "netmon" database and role and delete %ProgramData%\NetworkMonitor.

.PARAMETER Yes
    Do not ask for confirmation with -Purge.

.PARAMETER PostgresSuperPassword
    Password of the "postgres" superuser (only needed with -Purge when the installer did
    not install PostgreSQL itself). Prompted for if needed.
#>
#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [switch]$Purge,
    [switch]$Yes,
    [string]$PostgresSuperPassword = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$ServiceName  = 'NetworkMonitor'
$InstallDir   = Join-Path $env:ProgramFiles 'NetworkMonitor'
$DataDir      = Join-Path $env:ProgramData 'NetworkMonitor'
$PgSuperFile  = Join-Path $DataDir 'postgres-superuser.secret'
$FirewallRule = 'NetworkMonitor-HTTP'
$DbName       = 'netmon'
$DbUser       = 'netmon'

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }

if ($Purge -and -not $Yes) {
    $answer = Read-Host "This permanently deletes ALL NetworkMonitor data (database '$DbName' and $DataDir). Type 'yes' to continue"
    if ($answer -ne 'yes') { Write-Host 'Aborted.'; exit 1 }
}

# ----------------------------------------------------------------------------- service
$svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($svc) {
    Write-Step "Stopping and removing the '$ServiceName' service"
    if ($svc.Status -ne 'Stopped') {
        Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
        try { $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30)) } catch { }
    }
    $nssm = Join-Path $InstallDir 'nssm.exe'
    if (Test-Path $nssm) {
        & $nssm remove $ServiceName confirm | Out-Null
    }
    if (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
        & sc.exe delete $ServiceName | Out-Null
    }
}

# ----------------------------------------------------------------------------- firewall
if (Get-NetFirewallRule -Name $FirewallRule -ErrorAction SilentlyContinue) {
    Write-Step 'Removing the firewall rule'
    Remove-NetFirewallRule -Name $FirewallRule
}

# ----------------------------------------------------------------------------- program files
if (Test-Path $InstallDir) {
    Write-Step "Removing $InstallDir"
    # The service process may need a moment to release netmon.exe / python DLLs.
    for ($i = 0; $i -lt 10; $i++) {
        try { Remove-Item -Recurse -Force $InstallDir; break } catch { Start-Sleep -Seconds 2 }
    }
    if (Test-Path $InstallDir) { Write-Warning "Could not remove $InstallDir completely; delete it after a reboot." }
}

# ----------------------------------------------------------------------------- purge
if ($Purge) {
    $pg = $null
    $root = 'HKLM:\SOFTWARE\PostgreSQL\Installations'
    if (Test-Path $root) {
        $pg = Get-ChildItem $root | ForEach-Object { Get-ItemProperty $_.PSPath } |
              Where-Object { $_.PSObject.Properties['Base Directory'] } |
              Sort-Object { [int](($_.Version -split '\.')[0]) } -Descending | Select-Object -First 1
    }
    if ($pg) {
        $psql = Join-Path $pg.'Base Directory' 'bin\psql.exe'
        $port = 5432
        $svcId = if ($pg.PSObject.Properties['Service ID']) { $pg.'Service ID' } else { '' }
        $svcKey = "HKLM:\SOFTWARE\PostgreSQL\Services\$svcId"
        if ($svcId -and (Test-Path $svcKey)) {
            $sp = Get-ItemProperty $svcKey
            if ($sp.PSObject.Properties['Port']) { $port = [int]$sp.Port }
        }
        $super = $PostgresSuperPassword
        if (-not $super -and (Test-Path $PgSuperFile)) { $super = ([IO.File]::ReadAllText($PgSuperFile)).Trim() }
        if (-not $super) {
            $secure = Read-Host -AsSecureString "Password of the PostgreSQL superuser 'postgres'"
            $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            try { $super = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
            finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        }
        Write-Step "Dropping database '$DbName' and role '$DbUser'"
        $env:PGPASSWORD = $super
        $ErrorActionPreference = 'Continue'
        try {
            "DROP DATABASE IF EXISTS $DbName WITH (FORCE);`nDROP ROLE IF EXISTS $DbUser;" |
                & $psql -X -q -h 127.0.0.1 -p $port -U postgres -d postgres
            if ($LASTEXITCODE -ne 0) { Write-Warning "Could not drop the database/role (psql exit $LASTEXITCODE)." }
        } finally {
            Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
            $ErrorActionPreference = 'Stop'
        }
    } else {
        Write-Warning 'PostgreSQL installation not found; the database was not dropped.'
    }
    if (Test-Path $DataDir) {
        Write-Step "Removing $DataDir"
        if (Test-Path $PgSuperFile) {
            # PostgreSQL stays installed; keep the superuser password the installer generated
            # for it, otherwise a later install.ps1 could not administer that server.
            Get-ChildItem -Force $DataDir | Where-Object { $_.FullName -ne $PgSuperFile } |
                Remove-Item -Recurse -Force
            Write-Host "  kept $PgSuperFile (superuser password of the PostgreSQL server that stays installed)"
        } else {
            Remove-Item -Recurse -Force $DataDir
        }
    }
    Write-Host 'NetworkMonitor was removed completely.' -ForegroundColor Green
} else {
    Write-Host "NetworkMonitor was removed. Data kept: $DataDir and database '$DbName'." -ForegroundColor Green
    Write-Host 'Re-running install.ps1 reuses them; run uninstall.ps1 -Purge to delete them.'
}
