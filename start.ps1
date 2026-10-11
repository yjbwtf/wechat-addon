# ==========================================================================
#  微信群助手 启动脚本（PowerShell 版）
#
#  用法：
#     .\start.ps1              正式运行（按 config.yaml 的 mode 决定草稿/自动）
#     .\start.ps1 -Check       只做配置与大脑自检，不接微信
#     .\start.ps1 -Demo        控制台演示，完全不接触微信
#     .\start.ps1 -Groups      列出微信会话，方便把群名准确填进 whitelist_groups
#     .\start.ps1 -SelfTest    读白名单群的消息并核对字段映射（只读，不发送）
#     .\start.ps1 -Watch       监听并转储原始消息（不回复不发送），用于核对字段
#     .\start.ps1 -Watch -Seconds 60   同上，指定监听秒数
#     .\start.ps1 -Probe       打印 wxauto4 真实 API 结构（首次接入用）
#     .\start.ps1 -Console     用控制台代替微信接入
#     .\start.ps1 -Setup       只创建虚拟环境并安装依赖
#
#  注意：本机 PowerShell 执行策略禁止运行脚本，直接用 start.cmd 更省事；
#  非要用本脚本时请执行：
#     powershell -ExecutionPolicy Bypass -File .\start.ps1 -Check
#
#  所有文件都在 C:\wechat_addon 下：
#     C:\wechat_addon\       源码、配置、人设、数据
#     C:\wechat_addon\venv\  虚拟环境
#
#  虚拟环境刻意不放在 DSH 的工作目录里。实测：位于工作目录内的 python 解释器
#  会被限制为只能写工作目录，导致 dsh 无法写入 ~/.dsh/profiles/wechat/cordis.yml。
# ==========================================================================
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Demo,
    [switch]$Groups,
    [switch]$SelfTest,
    [switch]$Watch,
    [int]$Seconds = 90,
    [switch]$Probe,
    [switch]$Console,
    [switch]$Setup
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

# 中文环境下强制 UTF-8，避免群名/消息出现乱码
try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    [Console]::InputEncoding  = [System.Text.UTF8Encoding]::new()
    $OutputEncoding           = [System.Text.UTF8Encoding]::new()
} catch { }

# 路径全部从脚本自身位置推导，不写死机器 —— 仓库 clone 到哪都能跑。
$root       = $PSScriptRoot
$venvRoot   = Join-Path $root 'venv'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'
$mirror     = 'https://pypi.tuna.tsinghua.edu.cn/simple'

# 创建 venv 用的基础解释器（只在首次运行需要）：
# 显式指定 -> DSH 自带的那份 -> PATH 上的 python。
$basePython = $env:WECHAT_BOT_PYTHON
if (-not $basePython -or -not (Test-Path $basePython)) {
    $candidates = @(
        (Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe')
    )
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $basePython = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

function Ensure-Venv {
    if (-not (Test-Path $venvPython)) {
        Write-Host "创建虚拟环境：$venvRoot" -ForegroundColor Cyan
        if (-not (Test-Path $basePython)) {
            throw "找不到基础 Python：$basePython`n请修改 start.ps1 里的 `$basePython。"
        }
        & $basePython -m venv $venvRoot
    }
    $marker = Join-Path $venvRoot '.deps-installed'
    if (-not (Test-Path $marker)) {
        Write-Host "安装依赖（首次运行，需要几分钟）..." -ForegroundColor Cyan
        & $venvPython -m pip install --upgrade pip -i $mirror
        & $venvPython -m pip install -r (Join-Path $root 'requirements.txt') -i $mirror
        New-Item -ItemType File -Force -Path $marker | Out-Null
        Write-Host "依赖安装完成。" -ForegroundColor Green
    }
}

Ensure-Venv
if ($Setup) {
    Write-Host "环境已就绪：$venvPython" -ForegroundColor Green
    exit 0
}

Set-Location $root

if ($Check)   { & $venvPython -m src.main --check;   exit $LASTEXITCODE }
if ($Demo)    { & $venvPython -m src.main --demo;    exit $LASTEXITCODE }
if ($Groups)  { & $venvPython -m src.main --groups;  exit $LASTEXITCODE }
if ($SelfTest){ & $venvPython -m src.main --selftest; exit $LASTEXITCODE }
if ($Watch)   { & $venvPython -m src.main --watch $Seconds; exit $LASTEXITCODE }
if ($Probe)   { & $venvPython -m src.main --probe;   exit $LASTEXITCODE }
if ($Console) { & $venvPython -m src.main --console; exit $LASTEXITCODE }

& $venvPython -m src.main
exit $LASTEXITCODE
