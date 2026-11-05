<#
새 PC로 옮길 파일을 한 폴더(와 zip)로 모은다. 원본은 고치지 않는다.

사용 (프로젝트 폴더에서):
  powershell -NoProfile -ExecutionPolicy Bypass -File tools\pack_for_new_pc.ps1
  옵션: -OutRoot <폴더>       결과를 둘 곳 (기본: 바탕화면\pc_migration)
        -IncludeLogs          logs\ 도 넣는다
        -IncludeFileHistory   Claude Code 체크포인트(~\.claude\file-history)도 넣는다
        -NoCodexSessions      Codex 대화 기록을 넣지 않는다
        -NoZip                zip을 만들지 않는다

넣는 것
  project\ai_top_100.bundle   git 커밋 이력 전체 (원격 저장소가 없으므로 이것으로 옮긴다)
  project\worktree\           커밋되지 않은 파일 + git 제외 파일 중 필요한 것(.env, crosscheck.md, share\, OCR 샘플, 메일 원본)
  claude\                     ~\.claude 의 CLAUDE.md, settings.json, notify.ps1, history.jsonl, 이 프로젝트 대화 기록
  codex\                      ~\.codex 의 AGENTS.md, rules\, config.toml(참고용), 이 프로젝트의 Codex 대화 기록
넣지 않는 것
  로그인 정보(~\.claude\.credentials.json, ~\.codex\auth.json), ~\.claude.json, .venv, .venv-ocr, __pycache__
결과물에는 .env(비밀값)와 대화 원문이 들어 있다. USB 등으로 옮기고 다 쓰면 지운다.
#>
param(
    [string]$OutRoot = (Join-Path ([Environment]::GetFolderPath('Desktop')) 'pc_migration'),
    [switch]$IncludeLogs,
    [switch]$IncludeFileHistory,
    [switch]$NoCodexSessions,
    [switch]$NoZip
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8  # git이 내보내는 한글 파일 이름을 깨지지 않게 읽는다

$Project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ProjectKey = 'C--workspace-ai-top-100'  # ~\.claude\projects 아래 이 프로젝트 폴더 이름
$ClaudeHome = Join-Path $HOME '.claude'
$CodexHome = Join-Path $HOME '.codex'
$Out = Join-Path $OutRoot ('migration_' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
if ((Test-Path -LiteralPath $Out) -or (Test-Path -LiteralPath "$Out.zip")) { throw "이미 있음: $Out (잠시 뒤 다시 실행)" }
$Worktree = Join-Path $Out 'project\worktree'
$Missing = New-Object System.Collections.Generic.List[string]
$Failed = New-Object System.Collections.Generic.List[string]

function Copy-Into([string]$Src, [string]$Dst) {
    if (-not (Test-Path -LiteralPath $Src)) { $Missing.Add($Src); return }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Dst) | Out-Null
    try { Copy-Item -LiteralPath $Src -Destination $Dst -Recurse -Force }
    catch { $Failed.Add("$Src ($($_.Exception.Message))") }
}

function Copy-ProjectPath([string]$Rel) {
    Copy-Into (Join-Path $Project $Rel) (Join-Path $Worktree $Rel)
}

New-Item -ItemType Directory -Force -Path $Out | Out-Null
Write-Host "모으는 곳: $Out"

# 1. 프로젝트: 커밋 이력은 bundle로
Write-Host '[1/4] git bundle'
New-Item -ItemType Directory -Force -Path (Join-Path $Out 'project') | Out-Null
& git -C $Project bundle create (Join-Path $Out 'project\ai_top_100.bundle') --all
if ($LASTEXITCODE -ne 0) { throw 'git bundle 실패' }

# 2. 프로젝트: 커밋 안 된 파일과 git 제외 파일 중 필요한 것
Write-Host '[2/4] 커밋 안 된 파일, .env 등'
# 마지막 커밋 대비 바뀐 파일(스테이징 포함)과 새 파일. 삭제된 파일은 복원할 때 지우도록 목록만 남긴다
$changed = & git -C $Project -c core.quotepath=false diff --name-only --no-renames --diff-filter=d HEAD
$untracked = & git -C $Project -c core.quotepath=false ls-files -o --exclude-standard
foreach ($rel in @($changed) + @($untracked)) { if ($rel) { Copy-ProjectPath $rel } }
$deleted = @(& git -C $Project -c core.quotepath=false diff --name-only --no-renames --diff-filter=D HEAD | Where-Object { $_ })
if ($deleted.Count -gt 0) {
    $deleted | Out-File -FilePath (Join-Path $Out 'project\DELETED.txt') -Encoding utf8
    Write-Host "  커밋 안 된 삭제 $($deleted.Count)개: project\DELETED.txt (복원 후 직접 지운다)"
}
foreach ($rel in @('.env', 'crosscheck.md', 'share', 'tools\ocr\samples')) { Copy-ProjectPath $rel }
Get-ChildItem -LiteralPath $Project -Filter '*.eml' -File | ForEach-Object { Copy-ProjectPath $_.Name }
if ($IncludeLogs) { Copy-ProjectPath 'logs' }

# 3. Claude Code
Write-Host '[3/4] Claude Code 설정과 대화 기록'
$ClaudeOut = Join-Path $Out 'claude'
foreach ($name in @('CLAUDE.md', 'settings.json', 'notify.ps1', 'keybindings.json', 'history.jsonl')) {
    $src = Join-Path $ClaudeHome $name
    if (Test-Path -LiteralPath $src) { Copy-Into $src (Join-Path $ClaudeOut $name) }
}
Copy-Into (Join-Path $ClaudeHome "projects\$ProjectKey") (Join-Path $ClaudeOut "projects\$ProjectKey")
if ($IncludeFileHistory) { Copy-Into (Join-Path $ClaudeHome 'file-history') (Join-Path $ClaudeOut 'file-history') }

# 4. Codex
Write-Host '[4/4] Codex 설정과 이 프로젝트의 대화 기록'
$CodexOut = Join-Path $Out 'codex'
Copy-Into (Join-Path $CodexHome 'AGENTS.md') (Join-Path $CodexOut 'AGENTS.md')
Copy-Into (Join-Path $CodexHome 'rules') (Join-Path $CodexOut 'rules')
# config.toml에는 이 PC 전용 경로가 많아 그대로 덮어쓰면 안 된다. 참고용 이름으로 둔다.
Copy-Into (Join-Path $CodexHome 'config.toml') (Join-Path $CodexOut 'config.toml.reference')
if (-not $NoCodexSessions) {
    $count = 0
    foreach ($dir in @('sessions', 'archived_sessions')) {
        $root = Join-Path $CodexHome $dir
        if (-not (Test-Path -LiteralPath $root)) { continue }
        Get-ChildItem -LiteralPath $root -Recurse -File -Filter '*.jsonl' | ForEach-Object {
            # 첫 줄(session_meta)의 cwd가 이 프로젝트인 대화만 고른다
            # Codex가 쓰고 있는 파일도 읽을 수 있게 공유 모드로 연다
            $stream = New-Object System.IO.FileStream($_.FullName, 'Open', 'Read', 'ReadWrite, Delete')
            $reader = New-Object System.IO.StreamReader($stream)
            try { $first = $reader.ReadLine() } finally { $reader.Dispose() }
            if ($first -match '"cwd":"[^"]*ai_top_100') {
                $rel = $_.FullName.Substring($root.Length).TrimStart('\')
                Copy-Into $_.FullName (Join-Path $CodexOut "$dir\$rel")
                $count++
            }
        }
    }
    Copy-Into (Join-Path $CodexHome 'session_index.jsonl') (Join-Path $CodexOut 'session_index.jsonl')
    Write-Host "  Codex 대화 $count 개"
}

# 복원 안내
@"
복원 방법은 프로젝트의 HANDOFF.md "새 PC에서 복원하기"를 따른다.
요약:
  1) git clone project\ai_top_100.bundle C:\workspace\ai_top_100
  2) project\worktree\ 안의 내용을 C:\workspace\ai_top_100\ 에 덮어 복사.
     project\DELETED.txt 가 있으면 거기 적힌 파일을 지운다 (커밋 안 된 삭제)
  3) claude\ 안의 내용을 %USERPROFILE%\.claude\ 에 복사 (프로젝트 경로가 같아야 대화 기록이 이어진다)
  4) codex\AGENTS.md, codex\rules\, codex\sessions\ 를 %USERPROFILE%\.codex\ 에 복사.
     config.toml.reference 는 필요한 줄(model, model_reasoning_effort 등)만 옮겨 적는다
만든 시각: $(Get-Date -Format 'yyyy-MM-dd HH:mm')
"@ | Out-File -FilePath (Join-Path $Out 'RESTORE.txt') -Encoding utf8

if (-not $NoZip) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = "$Out.zip"
    [System.IO.Compression.ZipFile]::CreateFromDirectory($Out, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $false)
    Write-Host ("zip: {0} ({1:N1} MB)" -f $zip, ((Get-Item $zip).Length / 1MB))
}

$size = (Get-ChildItem -LiteralPath $Out -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ("폴더: {0} ({1:N1} MB)" -f $Out, ($size / 1MB))
if ($Missing.Count -gt 0) {
    Write-Host '없어서 건너뛴 항목:'
    $Missing | ForEach-Object { Write-Host "  $_" }
}
if ($Failed.Count -gt 0) {
    Write-Host '복사 실패 (쓰는 중인 파일이면 Claude Code·Codex를 끄고 다시 실행):' -ForegroundColor Yellow
    $Failed | ForEach-Object { Write-Host "  $_" }
}
Write-Host '주의: .env(비밀값)와 대화 원문이 들어 있다. 옮긴 뒤 지운다.'
