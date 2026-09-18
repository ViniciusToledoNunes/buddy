[CmdletBinding()]
param(
    [ValidateSet("All", "Codex", "Claude")]
    [string]$Client = "All"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    $PythonCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($PythonCommand) {
        $Created = $false
        foreach ($Version in @("-3.14", "-3.13", "-3.12")) {
            & py $Version -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) {
                & py $Version -m venv (Join-Path $RepoRoot ".venv")
                $Created = $true
                break
            }
        }
        if (-not $Created) { throw "Buddy requires Python 3.12, 3.13, or 3.14." }
    } else {
        & python -m venv (Join-Path $RepoRoot ".venv")
    }
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e "$RepoRoot[dev,window]"
$IntegrationArgs = @("--project-root", $RepoRoot)
if ($Client -eq "Codex") { $IntegrationArgs += @("--client", "codex") }
if ($Client -eq "Claude") { $IntegrationArgs += @("--client", "claude") }
& $VenvPython (Join-Path $PSScriptRoot "install_agent_integrations.py") @IntegrationArgs
& $VenvPython -m meeting_agent.cli doctor

Write-Host "Buddy installed. Start explicitly with: $VenvPython -m meeting_agent.cli start"
