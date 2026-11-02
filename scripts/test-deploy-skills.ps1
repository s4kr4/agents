# Run with: pwsh -NoProfile -File scripts/test-deploy-skills.ps1
param(
    [string]$Target = (Join-Path $PSScriptRoot 'deploy-skills.ps1')
)

$ErrorActionPreference = 'Stop'
$realHome = [IO.Path]::GetFullPath($HOME).TrimEnd('\')
$realRepo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$work = Join-Path ([IO.Path]::GetTempPath()) ('deploy-skills-test-' + [guid]::NewGuid().ToString('N'))
$workFull = [IO.Path]::GetFullPath($work).TrimEnd('\')
if ($workFull.StartsWith($realRepo + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $workFull.StartsWith((Join-Path $realHome '.claude\skills') + '\', [StringComparison]::OrdinalIgnoreCase) -or
    $workFull.StartsWith((Join-Path $realHome '.codex\skills') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "FATAL: test area inside a real path: $workFull"
}
$failures = 0

function Assert-Isolated([string]$homePath, [string]$repoPath) {
    $root = [IO.Path]::GetFullPath($work).TrimEnd('\') + '\'
    foreach ($path in @($homePath, $repoPath)) {
        $full = [IO.Path]::GetFullPath($path).TrimEnd('\')
        if (-not $full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
            throw "FATAL: fixture outside test area: $full"
        }
        if ($full -eq $realHome -or $full -eq $realRepo) {
            throw "FATAL: fixture matches a real path: $full"
        }
    }
}

function Snapshot-Entries([string]$dir) {
    if (-not (Test-Path -LiteralPath $dir)) { return @() }
    @(Get-ChildItem -LiteralPath $dir -Force | ForEach-Object {
        '{0}|{1}|{2}' -f $_.Name, $_.Attributes, ($_.Target -join ',')
    } | Sort-Object)
}

function Snapshot-Tree([string]$dir) {
    if (-not (Test-Path -LiteralPath $dir)) { return '' }
    $root = [IO.Path]::GetFullPath($dir).TrimEnd('\')
    @(Get-ChildItem -LiteralPath $dir -Force -Recurse | ForEach-Object {
        $content = if ($_.PSIsContainer) { '' } else { (Get-Content -LiteralPath $_.FullName -Raw) }
        '{0}|{1}' -f $_.FullName.Substring($root.Length), $content
    } | Sort-Object) -join "`n"
}

function Assert-True([bool]$condition, [string]$message) {
    if ($condition) { return }
    $script:failures++
    Write-Host "  FAIL: $message"
}

function Assert-Link([string]$link, [string]$target) {
    $item = Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
    Assert-True ($null -ne $item) "missing: $link"
    if ($null -eq $item) { return }
    $isLink = [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
    Assert-True $isLink "not a directory link: $link"
    if (-not $isLink) { return }
    Assert-True (([IO.Path]::GetFullPath($item.Target)) -eq [IO.Path]::GetFullPath($target)) "wrong target: $link"
}

function New-Fixture([string]$name) {
    $repo = Join-Path $work "$name-repo"
    $homePath = Join-Path $work "$name-home"
    Assert-Isolated $homePath $repo
    New-Item -ItemType Directory -Path $repo, $homePath | Out-Null
    foreach ($app in @('.claude', '.codex')) {
        $skill = Join-Path $repo "$app/skills/with space"
        New-Item -ItemType Directory -Path $skill -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $skill 'SKILL.md') -Value 'fixture'
        $notSkill = Join-Path $repo "$app/skills/without-skill"
        New-Item -ItemType Directory -Path $notSkill -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $notSkill 'README.md') -Value 'fixture'
        $local = Join-Path $homePath "$app/skills/local-only"
        New-Item -ItemType Directory -Path $local -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $local 'SKILL.md') -Value 'local'
    }
    return @{ Repo = $repo; Home = $homePath }
}

function Invoke-Deploy($fixture) {
    Assert-Isolated $fixture.Home $fixture.Repo
    $oldRepo = $env:AGENTSPATH
    $oldHome = $env:USERPROFILE
    try {
        $env:AGENTSPATH = $fixture.Repo
        $env:USERPROFILE = $fixture.Home
        $output = & pwsh -NoProfile -File $Target -RepositoryRoot $fixture.Repo -UserHome $fixture.Home *>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { Write-Host ($output -split "`n" | Select-Object -First 1) }
        return $LASTEXITCODE
    } finally {
        $env:AGENTSPATH = $oldRepo
        $env:USERPROFILE = $oldHome
    }
}

$homeBefore = @(
    Snapshot-Entries (Join-Path $realHome '.claude/skills')
    Snapshot-Entries (Join-Path $realHome '.codex/skills')
) -join "`n"
$repoBefore = @(
    Snapshot-Entries (Join-Path $realRepo '.claude/skills')
    Snapshot-Entries (Join-Path $realRepo '.codex/skills')
) -join "`n"

try {
    New-Item -ItemType Directory -Path $work | Out-Null
    $probe = Join-Path $work 'probe.ps1'
    Set-Content -LiteralPath $probe -Value 'param($RepositoryRoot, $UserHome); if (-not (Test-Path -LiteralPath $RepositoryRoot) -or -not (Test-Path -LiteralPath $UserHome)) { exit 3 }; exit 0'
    $probeFixture = New-Fixture 'probe'
    $savedTarget = $Target
    $Target = $probe
    try { Assert-True ((Invoke-Deploy $probeFixture) -eq 0) 'isolated child process did not run' }
    finally { $Target = $savedTarget }

    Write-Host '== SKILL.md が直下にある Claude と Codex の skill だけを個別に配置する'
    $fixture = New-Fixture 'basic'
    $result = Invoke-Deploy $fixture
    Assert-True ($result -eq 0) 'deploy failed'
    foreach ($app in @('.claude', '.codex')) {
        $source = Join-Path $fixture.Repo "$app/skills/with space"
        $dest = Join-Path $fixture.Home "$app/skills/with space"
        Assert-Link $dest $source
        Assert-True (-not (Test-Path -LiteralPath (Join-Path $fixture.Home "$app/skills/without-skill"))) 'non-skill was deployed'
        Assert-True (Test-Path -LiteralPath (Join-Path $fixture.Home "$app/skills/local-only/SKILL.md")) 'local skill was lost'
    }

    Write-Host '== 再実行しても既存リンクとローカル skill を保持する'
    $before = @(Snapshot-Entries (Join-Path $fixture.Home '.claude/skills')) -join "`n"
    $result = Invoke-Deploy $fixture
    Assert-True ($result -eq 0) 'second deploy failed'
    Assert-True ($before -eq (@(Snapshot-Entries (Join-Path $fixture.Home '.claude/skills')) -join "`n")) 'second deploy changed entries'

    Write-Host '== 既存の実ディレクトリはバックアップしてからリンクを配置する'
    $fixture = New-Fixture 'collision'
    $collision = Join-Path $fixture.Home '.claude/skills/with space'
    New-Item -ItemType Directory -Path $collision | Out-Null
    Set-Content -LiteralPath (Join-Path $collision 'personal.txt') -Value 'keep'
    $result = Invoke-Deploy $fixture
    Assert-True ($result -eq 0) 'collision deploy failed'
    Assert-Link $collision (Join-Path $fixture.Repo '.claude/skills/with space')
    $backup = @(Get-ChildItem -LiteralPath (Split-Path $collision) -Force | Where-Object { $_.Name -like 'with space.backup*' })
    Assert-True ($backup.Count -eq 1) 'collision backup missing'
    if ($backup.Count -eq 1) { Assert-True (Test-Path -LiteralPath (Join-Path $backup[0].FullName 'personal.txt')) 'backup lost its file' }

    Write-Host '== 親 skills が通常ファイルのとき、内容を残して明示エラーで停止する'
    $fixture = New-Fixture 'file-parent'
    $skillsPath = Join-Path $fixture.Home '.claude/skills'
    Move-Item -LiteralPath $skillsPath -Destination "$skillsPath.fixture"
    Set-Content -LiteralPath $skillsPath -Value 'personal skills file'
    $result = Invoke-Deploy $fixture
    Assert-True ($result -ne 0) 'file parent was accepted'
    Assert-True (Test-Path -LiteralPath $skillsPath -PathType Leaf) 'file parent was replaced'
    if (Test-Path -LiteralPath $skillsPath -PathType Leaf) {
        Assert-True ((Get-Content -LiteralPath $skillsPath -Raw).Trim() -eq 'personal skills file') 'file parent content changed'
    }
    $fileBackups = @(Get-ChildItem -LiteralPath (Split-Path $skillsPath) -Force | Where-Object { $_.Name -like 'skills.backup*' })
    Assert-True ($fileBackups.Count -eq 0) 'file parent was moved to backup'

    Write-Host '== リンク作成に失敗したとき、既存 skill の元パスと内容を復元する'
    $fixture = New-Fixture 'collision-failure'
    $collision = Join-Path $fixture.Home '.claude/skills/with space'
    New-Item -ItemType Directory -Path $collision | Out-Null
    Set-Content -LiteralPath (Join-Path $collision 'personal.txt') -Value 'keep-on-failure'
    $faultWrapper = Join-Path $work 'fail-junction.ps1'
    $faultMarker = Join-Path $work 'junction-fault-reached.txt'
    Set-Content -LiteralPath $faultWrapper -Value @'
param($RepositoryRoot, $UserHome)
$ErrorActionPreference = 'Stop'
function New-Item {
    param([string]$ItemType, [string]$Path, [string]$Target, [switch]$Force)
    if ($ItemType -eq 'Junction') {
        Set-Content -LiteralPath $env:DEPLOY_SKILLS_INJECTION_MARKER -Value 'junction'
        throw 'Injected Junction creation failure'
    }
    Microsoft.PowerShell.Management\New-Item @PSBoundParameters
}
& $env:DEPLOY_SKILLS_TARGET -RepositoryRoot $RepositoryRoot -UserHome $UserHome
'@
    $savedTarget = $Target
    $oldInjectedTarget = $env:DEPLOY_SKILLS_TARGET
    $oldMarker = $env:DEPLOY_SKILLS_INJECTION_MARKER
    try {
        $Target = $faultWrapper
        $env:DEPLOY_SKILLS_TARGET = $savedTarget
        $env:DEPLOY_SKILLS_INJECTION_MARKER = $faultMarker
        $result = Invoke-Deploy $fixture
    } finally {
        $Target = $savedTarget
        $env:DEPLOY_SKILLS_TARGET = $oldInjectedTarget
        $env:DEPLOY_SKILLS_INJECTION_MARKER = $oldMarker
    }
    Assert-True (Test-Path -LiteralPath $faultMarker -PathType Leaf) 'Junction failure was not injected'
    Assert-True ($result -ne 0) 'Junction failure did not fail deploy'
    $entry = Get-Item -LiteralPath $collision -Force -ErrorAction SilentlyContinue
    Assert-True ($null -ne $entry) 'original skill path was not restored'
    if ($null -ne $entry) {
        Assert-True (-not [bool]($entry.Attributes -band [IO.FileAttributes]::ReparsePoint)) 'restored skill is a link'
    }
    $personal = Join-Path $collision 'personal.txt'
    Assert-True (Test-Path -LiteralPath $personal -PathType Leaf) 'original skill content was not restored'
    if (Test-Path -LiteralPath $personal -PathType Leaf) {
        Assert-True ((Get-Content -LiteralPath $personal -Raw).Trim() -eq 'keep-on-failure') 'restored skill content changed'
    }

    Write-Host '== ソースから消えた skill の旧リンクを退避し、ローカル skill は保持する'
    $fixture = New-Fixture 'stale'
    $result = Invoke-Deploy $fixture
    Assert-True ($result -eq 0) 'initial stale setup failed'
    $source = Join-Path $fixture.Repo '.codex/skills/with space'
    Move-Item -LiteralPath $source -Destination "$source.old"
    $result = Invoke-Deploy $fixture
    Assert-True ($result -eq 0) 'stale cleanup deploy failed'
    $staleEntry = @(Get-ChildItem -LiteralPath (Join-Path $fixture.Home '.codex/skills') -Force | Where-Object Name -eq 'with space')
    Assert-True ($staleEntry.Count -eq 0) 'stale link was retained'
    Assert-True (Test-Path -LiteralPath (Join-Path $fixture.Home '.codex/skills/local-only/SKILL.md')) 'local skill was lost on cleanup'

    Write-Host '== ソース側の skills が無い場合は既存の配置先を保持する'
    $fixture = New-Fixture 'missing'
    Move-Item -LiteralPath (Join-Path $fixture.Repo '.codex/skills') -Destination (Join-Path $fixture.Repo '.codex/skills.old')
    $result = Invoke-Deploy $fixture
    Assert-True ($result -eq 0) 'missing source deploy failed'
    Assert-True (Test-Path -LiteralPath (Join-Path $fixture.Home '.codex/skills/local-only/SKILL.md')) 'destination was modified for missing source'

    Write-Host '== 配置先の skills がリンクのとき、何も変更せずにエラーで停止する'
    $fixture = New-Fixture 'linked-parent'
    $skillsPath = Join-Path $fixture.Home '.claude/skills'
    $linkedTarget = Join-Path $work 'linked-parent-target'
    Assert-Isolated $linkedTarget $fixture.Repo
    Move-Item -LiteralPath $skillsPath -Destination $linkedTarget
    New-Item -ItemType Junction -Path $skillsPath -Target $linkedTarget | Out-Null
    $targetBefore = Snapshot-Tree $linkedTarget
    Assert-True ($targetBefore -like '*local-only*') 'linked parent fixture is empty'
    $result = Invoke-Deploy $fixture
    Assert-True ($result -ne 0) 'linked parent was accepted'
    Assert-True ($targetBefore -eq (Snapshot-Tree $linkedTarget)) 'linked parent target changed'
    $linkBackups = @(Get-ChildItem -LiteralPath (Split-Path $skillsPath) -Force | Where-Object { $_.Name -like 'skills.backup*' })
    Assert-True ($linkBackups.Count -eq 0) 'linked parent was moved to backup'
    Assert-Link $skillsPath $linkedTarget

    Write-Host '== リポジトリ外を指すローカル skill のリンクは、再実行後も同じ名前・同じリンク先のまま保持する'
    $fixture = New-Fixture 'external-link'
    $external = Join-Path $work 'external-link-outside'
    Assert-Isolated $external $fixture.Repo
    $localLinks = @(
        @{ Name = 'external-only'; Label = 'name absent from repository' }
        @{ Name = 'without-skill'; Label = 'name present in repository' }
    )
    foreach ($local in $localLinks) {
        $local.Target = Join-Path $external $local.Name
        $local.Link = Join-Path $fixture.Home ".claude/skills/$($local.Name)"
        New-Item -ItemType Directory -Path $local.Target -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $local.Target 'SKILL.md') -Value 'external'
        New-Item -ItemType Junction -Path $local.Link -Target $local.Target | Out-Null
    }
    Assert-True (Test-Path -LiteralPath (Join-Path $fixture.Repo '.claude/skills/without-skill') -PathType Container) 'repository lacks same-name fixture'
    foreach ($run in 1..2) {
        $result = Invoke-Deploy $fixture
        Assert-True ($result -eq 0) "deploy $run failed with external links"
        foreach ($local in $localLinks) {
            Assert-Link $local.Link $local.Target
            $moved = @(Get-ChildItem -LiteralPath (Split-Path $local.Link) -Force | Where-Object { $_.Name -like "$($local.Name).backup*" })
            Assert-True ($moved.Count -eq 0) "external link was moved to backup on run ${run}: $($local.Label)"
        }
    }

    Write-Host '== 退避先 .backup が既にあるとき、新しい退避は .backup.1 に入り、既存の .backup は入れ子にならず変わらない'
    $fixture = New-Fixture 'backup-exists'
    $collision = Join-Path $fixture.Home '.claude/skills/with space'
    New-Item -ItemType Directory -Path $collision | Out-Null
    Set-Content -LiteralPath (Join-Path $collision 'personal.txt') -Value 'current'
    $oldBackup = "$collision.backup"
    New-Item -ItemType Directory -Path $oldBackup | Out-Null
    Set-Content -LiteralPath (Join-Path $oldBackup 'previous.txt') -Value 'previous'
    $oldBefore = Snapshot-Tree $oldBackup
    Assert-True ($oldBefore -like '*previous.txt*') 'existing backup fixture is empty'
    $result = Invoke-Deploy $fixture
    Assert-True ($result -eq 0) 'deploy failed with existing backup'
    Assert-Link $collision (Join-Path $fixture.Repo '.claude/skills/with space')
    $newPersonal = Join-Path "$collision.backup.1" 'personal.txt'
    Assert-True (Test-Path -LiteralPath $newPersonal -PathType Leaf) 'new backup is not in .backup.1'
    if (Test-Path -LiteralPath $newPersonal -PathType Leaf) {
        Assert-True ((Get-Content -LiteralPath $newPersonal -Raw).Trim() -eq 'current') 'new backup content changed'
    }
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $oldBackup 'with space'))) 'new backup was nested in existing backup'
    Assert-True ($oldBefore -eq (Snapshot-Tree $oldBackup)) 'existing backup changed'
} finally {
    $homeAfter = @(
        Snapshot-Entries (Join-Path $realHome '.claude/skills')
        Snapshot-Entries (Join-Path $realHome '.codex/skills')
    ) -join "`n"
    $repoAfter = @(
        Snapshot-Entries (Join-Path $realRepo '.claude/skills')
        Snapshot-Entries (Join-Path $realRepo '.codex/skills')
    ) -join "`n"
    Assert-True ($homeBefore -eq $homeAfter) 'real home entries changed'
    Assert-True ($repoBefore -eq $repoAfter) 'real repo entries changed'
    if (Test-Path -LiteralPath $work) { Move-Item -LiteralPath $work -Destination "$work.discarded" }
}

if ($failures -gt 0) { throw "$failures assertion(s) failed" }
Write-Host 'PASS: deploy-skills.ps1'
