# One-line bootstrap and local wrapper. The downloaded installer is accepted
# only when it matches the SHA-256 embedded in this wrapper.
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$InstallerArguments
)

$ErrorActionPreference = "Stop"
$defaultInstallUrl = "https://raw.githubusercontent.com/Abdess/retrobios/main/install.py"
$defaultInstallSha256 = "431f7d05350e222a3f4746dd13116bf570ec0f80117eed47d747372c61b57d77"
$maximumInstallerBytes = 2MB
$installerExit = 0
$installer = if ($PSScriptRoot) { Join-Path $PSScriptRoot "install.py" } else { $null }
$temporary = $null

try {
    if (-not $installer -or -not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        $url = if ($env:RETROBIOS_INSTALL_URL) { $env:RETROBIOS_INSTALL_URL } else { $defaultInstallUrl }
        $expected = if ($env:RETROBIOS_INSTALL_SHA256) { $env:RETROBIOS_INSTALL_SHA256 } else { $defaultInstallSha256 }
        $uri = [Uri]$url
        if ($uri.Scheme -ne "https") {
            throw "RETROBIOS_INSTALL_URL must use HTTPS."
        }
        if ($expected -notmatch '^[0-9a-fA-F]{64}$') {
            throw "Installer SHA-256 must contain exactly 64 hexadecimal characters."
        }
        # Windows PowerShell 5.1 still negotiates SSL 3.0 / TLS 1.0 by default
        # and GitHub refuses both, so the download fails before any hash is
        # checked. install.sh pins the same floor with curl --tlsv1.2.
        if ($PSVersionTable.PSEdition -ne "Core") {
            [Net.ServicePointManager]::SecurityProtocol = `
                [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        }
        $temporary = Join-Path ([IO.Path]::GetTempPath()) ("retrobios-install-{0}.py" -f [Guid]::NewGuid())
        Invoke-WebRequest -Uri $uri -OutFile $temporary -UseBasicParsing
        if ((Get-Item -LiteralPath $temporary).Length -gt $maximumInstallerBytes) {
            throw "Downloaded installer exceeds the size limit."
        }
        $actual = (Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected.ToLowerInvariant()) {
            throw "The downloaded installer does not match its expected fingerprint. Nothing was run and nothing was written. The download was most likely cut short; try again, on another network if possible."
        }
        $installer = $temporary
    }

    $launcher = @()
    $python = Get-Command py -ErrorAction SilentlyContinue
    if ($python) {
        $launcher = @("-3")
    } else {
        $python = Get-Command python3 -ErrorAction SilentlyContinue
        if (-not $python) {
            $python = Get-Command python -ErrorAction SilentlyContinue
        }
    }
    if (-not $python) {
        throw "Python 3.8 or newer is required."
    }
    & $python.Source @launcher -c "import sys; raise SystemExit(sys.version_info < (3, 8))"
    if ($LASTEXITCODE -ne 0) { throw "Python 3.8 or newer is required." }
    & $python.Source @launcher $installer @InstallerArguments
    # The installer has already said what happened: its status is passed on
    # as it is (1 for files left to fetch under --check, 2 when nothing was
    # installed, 130 on Ctrl+C), not turned into a red exception that reads
    # as a crash and flattens every code to one.
    $installerExit = $LASTEXITCODE
}
finally {
    if ($temporary -and (Test-Path -LiteralPath $temporary)) {
        Remove-Item -LiteralPath $temporary -Force
    }
}

if ($installerExit) {
    # Run from a file, the script ends with that status. Piped into iex it
    # runs in the user's own session, which exit would close.
    if ($PSScriptRoot) { exit $installerExit }
    $global:LASTEXITCODE = $installerExit
}
