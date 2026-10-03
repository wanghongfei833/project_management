<#
.SYNOPSIS
从阿里云服务器拉取项目**全部**数据（Ledger 数据库 + uploads 附件），并在本地校验后落位。

.DESCRIPTION
流程：
  1) 探测服务器上的项目根目录（自动尝试 /root/project/PM 等常见路径）
  2) 上传并执行 scripts/remote_snapshot.py —— 用 sqlite3 backup API 生成**一致快照**
     （服务器上 gunicorn 正在写库，直接 scp 可能拿到写了一半的文件）
  3) 打包 uploads 并下载：ledger.db + manifest.json + uploads.tar.gz
  4) 用 scripts/verify_data_integrity.py 本地校验
     （integrity_check / 外键 / 逐表行数 / 附件缺失与孤儿 / 每个文件 sha256）
  5) 校验通过才落位：
       -Mode isolated   -> 放到 _aliyun_data\ （默认，不动现有文件）
       -Mode overwrite  -> 先把现有 instance\ 与 uploads\ 备份到 _local_backup\<时间戳>\，再覆盖

.EXAMPLE
# 先只下载 + 校验，不碰现有数据（推荐先跑这个）
powershell -ExecutionPolicy Bypass -File scripts\pull_aliyun_data.ps1

.EXAMPLE
# 确认无误后，用真实数据覆盖本地工作副本（原数据会自动备份）
powershell -ExecutionPolicy Bypass -File scripts\pull_aliyun_data.ps1 -Mode overwrite
#>
[CmdletBinding()]
param(
    [string]$SshHost = "aliyun",
    [string]$RemoteRoot = "",
    [string]$RemotePython = "/root/miniconda3/envs/TIE/bin/python",
    [ValidateSet("isolated", "overwrite")]
    [string]$Mode = "isolated",
    [string]$RepoRoot = "",
    [string]$PythonExe = "F:\Anaconda3\python.exe"
)

# 原生命令（ssh/scp/tar）的非零退出码不会被 Stop 捕获，统一用 $LASTEXITCODE 显式判断
$ErrorActionPreference = "Continue"

if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
$RepoRoot = (Resolve-Path $RepoRoot).Path
$stage = Join-Path $RepoRoot "_aliyun_snapshot"
$remoteTmp = "/tmp/pm_snapshot"

# 首次连接自动接受主机密钥，避免非交互会话卡在 yes/no 提示
$sshArgs = @("-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15")

Write-Host "仓库根目录: $RepoRoot"
Write-Host "下载暂存区: $stage"
Write-Host "落地模式  : $Mode"

# ---------- 0. 检查本地工具 ----------
Write-Host "`n=== 0/6 检查本地工具 ===" -ForegroundColor Cyan
foreach ($cmd in @("ssh", "scp", "tar")) {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
        throw "找不到 $cmd，请先安装 OpenSSH 客户端 / 使用 Windows 10 1803+ 自带 tar"
    }
}
if (-not (Test-Path $PythonExe)) { throw "找不到 Python：$PythonExe" }
ssh @sshArgs -o BatchMode=yes $SshHost "echo CONNECTED"
if ($LASTEXITCODE -ne 0) {
    throw "无法通过 ssh 连接 '$SshHost'（检查 %USERPROFILE%\.ssh\config 与密钥权限）"
}
Write-Host "ssh / scp / tar / python 均可用"

# ---------- 1. 探测服务器项目根目录 ----------
Write-Host "`n=== 1/6 探测服务器项目根目录 ===" -ForegroundColor Cyan
if (-not $RemoteRoot) {
    $probe = ssh @sshArgs $SshHost 'for d in /root/project/PM /root/project_management /root/PM; do [ -f $d/instance/ledger.db ] && echo FOUND=$d; done'
    $found = $probe | Where-Object { $_ -match "^FOUND=" } | ForEach-Object { $_.Substring(6).Trim() } | Select-Object -First 1
    if (-not $found) {
        throw "未在常见路径找到 instance/ledger.db，请用 -RemoteRoot 指定服务器项目根目录"
    }
    $RemoteRoot = $found
}
Write-Host "服务器项目根目录: $RemoteRoot"
ssh @sshArgs $SshHost "ls -la $RemoteRoot"
ssh @sshArgs $SshHost "test -d $RemoteRoot/uploads && du -sh $RemoteRoot/uploads || echo 'NO_UPLOADS_DIR'"

# ---------- 2. 服务器端生成一致快照 ----------
Write-Host "`n=== 2/6 在服务器上生成一致快照 ===" -ForegroundColor Cyan
scp @sshArgs (Join-Path $PSScriptRoot "remote_snapshot.py") "${SshHost}:/tmp/remote_snapshot.py"
if ($LASTEXITCODE -ne 0) { throw "上传 remote_snapshot.py 失败" }
ssh @sshArgs $SshHost "$RemotePython /tmp/remote_snapshot.py --root $RemoteRoot --out $remoteTmp"
if ($LASTEXITCODE -ne 0) { throw "服务器端快照生成失败（数据库可能损坏，或 RemotePython 路径不对）" }

# ---------- 3. 打包 uploads ----------
Write-Host "`n=== 3/6 打包 uploads ===" -ForegroundColor Cyan
ssh @sshArgs $SshHost "tar -czf $remoteTmp/uploads.tar.gz -C $RemoteRoot uploads 2>/dev/null || echo 'NO_UPLOADS'"

# ---------- 4. 下载到本地 ----------
Write-Host "`n=== 4/6 下载到本地 ===" -ForegroundColor Cyan
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage | Out-Null
scp @sshArgs "${SshHost}:$remoteTmp/ledger.db" $stage
if ($LASTEXITCODE -ne 0) { throw "下载 ledger.db 失败" }
scp @sshArgs "${SshHost}:$remoteTmp/manifest.json" $stage
if ($LASTEXITCODE -ne 0) { throw "下载 manifest.json 失败" }
scp @sshArgs "${SshHost}:$remoteTmp/uploads.tar.gz" $stage
if ($LASTEXITCODE -eq 0) {
    $tarFile = Join-Path $stage "uploads.tar.gz"
    tar -xzf $tarFile -C $stage
    if ($LASTEXITCODE -ne 0) { throw "解包 uploads.tar.gz 失败" }
    Remove-Item $tarFile -Force
    Write-Host "uploads 已解包"
} else {
    Write-Host "服务器上没有 uploads.tar.gz，跳过附件" -ForegroundColor Yellow
}

# ---------- 5. 本地校验 ----------
Write-Host "`n=== 5/6 本地校验 ===" -ForegroundColor Cyan
& $PythonExe (Join-Path $PSScriptRoot "verify_data_integrity.py") `
    --db (Join-Path $stage "ledger.db") `
    --uploads (Join-Path $stage "uploads") `
    --manifest (Join-Path $stage "manifest.json")
if ($LASTEXITCODE -ne 0) {
    throw "校验未通过（退出码 $LASTEXITCODE）。数据保留在 $stage 供排查；服务器临时文件也未清理。"
}

# ---------- 6. 落位 ----------
Write-Host "`n=== 6/6 落位（Mode=$Mode）===" -ForegroundColor Cyan
if ($Mode -eq "isolated") {
    $target = Join-Path $RepoRoot "_aliyun_data"
    if (Test-Path $target) {
        $old = "$target.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
        Move-Item $target $old
        Write-Host "上一次的 _aliyun_data 已改名保留：$old"
    }
    Move-Item $stage $target
    $dbUrl = "sqlite:///" + (($target -replace "\\", "/") + "/ledger.db")
    Write-Host "数据已就位：$target"
    Write-Host "用它跑本地服务（示例，PowerShell）："
    Write-Host "  `$env:DATABASE_URL = '$dbUrl'"
    Write-Host "  `$env:UPLOAD_FOLDER = '$target\uploads'"
    Write-Host "  `$env:SECRET_KEY = 'local-dev'"
    Write-Host "  & '$PythonExe' run.py"
}
else {
    $backupDir = Join-Path $RepoRoot ("_local_backup\" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    if (Test-Path (Join-Path $RepoRoot "instance")) {
        Copy-Item (Join-Path $RepoRoot "instance") $backupDir -Recurse -Force
    }
    if (Test-Path (Join-Path $RepoRoot "uploads")) {
        Copy-Item (Join-Path $RepoRoot "uploads") $backupDir -Recurse -Force
    }
    Write-Host "本地原数据已备份到：$backupDir"

    $instDir = Join-Path $RepoRoot "instance"
    if (-not (Test-Path $instDir)) { New-Item -ItemType Directory -Path $instDir | Out-Null }
    Copy-Item (Join-Path $stage "ledger.db") (Join-Path $instDir "ledger.db") -Force

    $uploadsSrc = Join-Path $stage "uploads"
    if (Test-Path $uploadsSrc) {
        $uploadsDst = Join-Path $RepoRoot "uploads"
        if (-not (Test-Path $uploadsDst)) { New-Item -ItemType Directory -Path $uploadsDst | Out-Null }
        # 先清空，避免本地残留服务器上已删除的文件（原内容已在 _local_backup 中）
        Get-ChildItem -Path $uploadsDst -Force | Remove-Item -Recurse -Force
        Copy-Item (Join-Path $uploadsSrc "*") $uploadsDst -Recurse -Force
    }
    Write-Host "已覆盖 instance\ledger.db 与 uploads\"
    Remove-Item $stage -Recurse -Force
}

ssh @sshArgs $SshHost "rm -rf $remoteTmp"
Write-Host "`n完成：全部数据已同步并通过校验。" -ForegroundColor Green
