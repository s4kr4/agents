param(
    [string]$RepositoryRoot = (Join-Path $PSScriptRoot '..'),
    [string]$UserHome = $HOME
)

$ErrorActionPreference = 'Stop'

function Get-Entry([string]$Path) {
    Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
}

function Test-Link($Entry) {
    $null -ne $Entry -and [bool]($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Get-LinkTarget($Entry) {
    if (-not (Test-Link $Entry) -or -not $Entry.Target) { return $null }
    [IO.Path]::GetFullPath($Entry.Target)
}

function Test-SamePath([string]$Left, [string]$Right) {
    [string]::Equals(
        [IO.Path]::GetFullPath($Left).TrimEnd('\'),
        [IO.Path]::GetFullPath($Right).TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Move-ToBackup([string]$Path) {
    $backup = "$Path.backup"
    for ($number = 1; $null -ne (Get-Entry $backup); $number++) {
        $backup = "$Path.backup.$number"
    }
    Move-Item -LiteralPath $Path -Destination $backup
    Write-Host "  Backed up: $Path -> $backup"
    return $backup
}

function Ensure-RealDirectory([string]$Path) {
    $entry = Get-Entry $Path
    if (Test-Link $entry) {
        # Replacing this link would hide local skills stored in its target.
        throw "Skills directory is a reparse point; leave it unchanged: $Path"
    }
    if ($null -ne $entry -and -not $entry.PSIsContainer) {
        throw "Skills directory path is a file; leave it unchanged: $Path"
    }
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
}

function Deploy-Skills([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        Write-Host "  Skipped: $Source does not exist"
        return
    }

    Ensure-RealDirectory $Destination

    foreach ($entry in @(Get-ChildItem -LiteralPath $Destination -Force)) {
        if (-not (Test-Link $entry)) { continue }
        $target = Get-LinkTarget $entry
        if ($null -eq $target) { continue }
        $expected = Join-Path $Source $entry.Name
        if (-not (Test-SamePath $target $expected)) { continue }
        if (-not (Test-Path -LiteralPath (Join-Path $expected 'SKILL.md') -PathType Leaf)) {
            $null = Move-ToBackup $entry.FullName
        }
    }

    foreach ($skill in @(Get-ChildItem -LiteralPath $Source -Directory -Force)) {
        if (-not (Test-Path -LiteralPath (Join-Path $skill.FullName 'SKILL.md') -PathType Leaf)) { continue }
        $link = Join-Path $Destination $skill.Name
        $existing = Get-Entry $link
        $backup = $null
        if ($null -ne $existing) {
            $target = Get-LinkTarget $existing
            if ($null -ne $target -and (Test-SamePath $target $skill.FullName)) {
                Write-Host "  Linked: $link -> $($skill.FullName)"
                continue
            }
            $backup = Move-ToBackup $link
        }
        try {
            New-Item -ItemType Junction -Path $link -Target $skill.FullName | Out-Null
        } catch {
            $createError = $_.Exception.Message
            if ($null -ne $backup -and $null -eq (Get-Entry $link)) {
                try {
                    Move-Item -LiteralPath $backup -Destination $link
                    Write-Host "  Restored: $backup -> $link"
                } catch {
                    throw "Cannot create skill junction '$link': $createError; cannot restore backup '$backup': $($_.Exception.Message)"
                }
            }
            throw "Cannot create skill junction '$link' -> '$($skill.FullName)': $createError"
        }
        Write-Host "  Linked: $link -> $($skill.FullName)"
    }
}

$repository = [IO.Path]::GetFullPath($RepositoryRoot)
$homePath = [IO.Path]::GetFullPath($UserHome)
foreach ($app in @('.claude', '.codex')) {
    Write-Host "==> Deploying $app skills..."
    Deploy-Skills (Join-Path $repository "$app/skills") (Join-Path $homePath "$app/skills")
}
