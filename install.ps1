# Installs the Spend-Guard skill for this Windows user (~/.copilot/skills/spend-guard).
# Then, in any project chat, type /spend-guard (or ask to "set up spend-guard").
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw 'Python 3.10+ is required (python not found on PATH).' }
& $py.Source -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { throw 'Python 3.10+ is required.' }
$copilotHome = if ($env:COPILOT_HOME) { $env:COPILOT_HOME } else { Join-Path $env:USERPROFILE '.copilot' }
$dest = Join-Path $copilotHome 'skills\spend-guard'
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item (Join-Path $here 'skill\spend-guard\*') $dest -Recurse -Force
Write-Host "Spend-Guard installed: $dest"
Write-Host 'Open a project, start a new chat and type /spend-guard.'
