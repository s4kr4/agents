# Arguments are passed through to the common generator, e.g. --client codex.
$ErrorActionPreference = 'Stop'
$AgentsPath = if ($env:AGENTSPATH) { $env:AGENTSPATH } else { Split-Path -Parent $PSScriptRoot }
$UvCommand = Get-Command uv -CommandType Application -ErrorAction SilentlyContinue
if (-not $UvCommand) {
    [Console]::Error.WriteLine('skipped: uv is unavailable; install native Windows uv and retry')
    exit 2
}
$UvPath = $UvCommand.Source
$MemoryPath = Join-Path $AgentsPath 'memory'
& $UvPath sync --locked --project $MemoryPath
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# Handshake-test check_mcp.py against an isolated, throwaway Vault/local/
# queue/config -- never the caller's real Vault or real MCP client config.
# check_mcp.py's live round trip actually calls write_memory(...), so
# running it against whatever the caller's environment/config already
# points at would write a real "editor" profile memory into the real Vault
# on every first-time setup run (and Syncthing would then propagate it to
# every other machine). Windows resolves its config/lock search paths from
# APPDATA/LOCALAPPDATA rather than XDG_CONFIG_HOME/XDG_CACHE_HOME (see
# memory_config.py's default_config_path and store_lock.py's
# lock_directory), so both must be redirected here too, not just the
# LLM_MEMORY_* variables (see the Unix equivalent, scripts/memory-mcp-check.sh).
$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('memory-mcp-check-' + [System.Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $TempRoot | Out-Null
$PrevVault = $env:LLM_MEMORY_VAULT
$PrevLocalDir = $env:LLM_MEMORY_LOCAL_DIR
$PrevQueueDir = $env:LLM_MEMORY_QUEUE_DIR
$PrevAppData = $env:APPDATA
$PrevLocalAppData = $env:LOCALAPPDATA
$PrevConfig = $env:LLM_MEMORY_CONFIG
$CheckExitCode = 0
try {
    $env:LLM_MEMORY_VAULT = Join-Path $TempRoot 'vault'
    $env:LLM_MEMORY_LOCAL_DIR = Join-Path $TempRoot 'local'
    $env:LLM_MEMORY_QUEUE_DIR = Join-Path $TempRoot 'queue'
    $env:APPDATA = Join-Path $TempRoot 'config'
    $env:LOCALAPPDATA = Join-Path $TempRoot 'cache'
    Remove-Item Env:LLM_MEMORY_CONFIG -ErrorAction SilentlyContinue
    & $UvPath run --locked --project $MemoryPath python (Join-Path $MemoryPath 'check_mcp.py')
    $CheckExitCode = $LASTEXITCODE
}
finally {
    # $env:X = $null removes the variable, restoring "was unset" correctly.
    $env:LLM_MEMORY_VAULT = $PrevVault
    $env:LLM_MEMORY_LOCAL_DIR = $PrevLocalDir
    $env:LLM_MEMORY_QUEUE_DIR = $PrevQueueDir
    $env:APPDATA = $PrevAppData
    $env:LOCALAPPDATA = $PrevLocalAppData
    $env:LLM_MEMORY_CONFIG = $PrevConfig
    Remove-Item -Recurse -Force $TempRoot -ErrorAction SilentlyContinue
}
if ($CheckExitCode -ne 0) { exit $CheckExitCode }

& $UvPath run --locked --project $MemoryPath python (Join-Path $AgentsPath 'scripts/memory_mcp_config.py') --uv $UvPath --agents-path $AgentsPath @args
exit $LASTEXITCODE
