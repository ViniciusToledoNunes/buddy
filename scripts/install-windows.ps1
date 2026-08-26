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
        & py -3.12 -m venv (Join-Path $RepoRoot ".venv")
    } else {
        & python -m venv (Join-Path $RepoRoot ".venv")
    }
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e "$RepoRoot[dev]"
$IntegrationArgs = @("--project-root", $RepoRoot)
if ($Client -eq "Codex") { $IntegrationArgs += @("--client", "codex") }
if ($Client -eq "Claude") { $IntegrationArgs += @("--client", "claude") }
& $VenvPython (Join-Path $PSScriptRoot "install_agent_integrations.py") @IntegrationArgs
& $VenvPython -m meeting_agent.cli doctor

Write-Host "Installed. Start explicitly with: $VenvPython -m meeting_agent.cli start"
