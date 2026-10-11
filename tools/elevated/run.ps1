# ==========================================================================
#  WeChatElevated - 以管理员权限执行**预先批准**的少数几个操作
#  ------------------------------------------------------------------------
#  为什么需要它
#
#  机器人（以及它派生的 agent）跑在 Medium 完整性下，碰不到需要管理员的事。
#  UAC 只能由人在电脑前点，而群里没人能点。同时我们又**不想**把整个机器人
#  提权 —— 那等于让 agent 拿到完整管理员权限，一条错命令就能搞坏系统。
#
#  折中办法：一个最高权限的计划任务，只执行本脚本，而本脚本只认白名单里的
#  动作。agent 只能"选一个动作"，不能"塞一条命令进来"。
#
#  ## 安全边界在哪
#
#  本脚本放在 %ProgramData%\WeChatElevated\，**ACL 锁成仅 Administrators 可写**
#  （见 setup_elevated.ps1）。这不是装饰：
#
#    非提权进程的令牌里，Administrators 是 deny-only，所以 agent **改不了这个文件**。
#    而计划任务用完整管理员令牌运行，读得了、也写得进去。
#
#  如果这个文件是可写的，agent 就能把自己想跑的命令写进来再触发任务 ——
#  那样这个"预授权"设计就等于零，直接给管理员权限了。**别把 ACL 去掉。**
#
#  ## 请求/结果的文件通道
#
#      request.json    <- agent 写（该目录对这个用户可写）。内容只有动作名 + 参数
#      result.txt      <- 本脚本写，agent 读
#      run.log         <- 追加式审计：谁在什么时候要了什么
#
#  request.json 的内容**当作不可信输入**处理：动作名必须在白名单里，
#  参数逐个校验。不要因为它"是本机写的"就放松。
# ==========================================================================

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Base    = $PSScriptRoot
# 请求/结果/日志放在 data 子目录里，因为**这一层要单独放开写入权限**：
# 脚本本身是仅管理员可写，但 agent（非提权）必须能写 request.json、
# 也得能读 result.txt。混在一个目录里就没法分开授权了。
$DataDir = Join-Path $Base 'data'
$Request = Join-Path $DataDir 'request.json'
$Result  = Join-Path $DataDir 'result.txt'
$LogFile = Join-Path $DataDir 'run.log'

if (-not (Test-Path -LiteralPath $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
}

function Write-Log([string]$Text) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Text
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

function Set-Result([string]$Text, [bool]$Ok) {
    $payload = [ordered]@{
        ok      = $Ok
        at      = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
        output  = $Text
    } | ConvertTo-Json -Depth 5
    # result.txt 要能被非提权的 agent 读走，所以用 UTF8 无 BOM 写
    [System.IO.File]::WriteAllText($Result, $payload, [System.Text.UTF8Encoding]::new($false))
}

# --------------------------------------------------------------------------
#  白名单动作
#
#  加新动作 = 在这里加一个 switch 分支，然后重新跑一次 setup_elevated.ps1
#  （因为它要重新把文件复制到 ProgramData 并设 ACL）。
#
#  写新动作时注意：
#    - 参数一定要校验。别把 agent 传进来的字符串直接拼进命令行。
#    - 能不要参数就不要参数。参数越多，"预授权"的边界越模糊。
#    - 返回值别太大，它会被发到微信群里。
# --------------------------------------------------------------------------
function Invoke-Action {
    # 参数名**不能叫 $Args** —— 那是 PowerShell 的自动变量（未绑定参数），
    # 撞名之后 $Args.service 永远是空的。
    # 实测踩过：白名单动作配合法参数也被拒，报「服务  不在白名单里」（名字是空的）。
    param([string]$Action, $Params)

    switch ($Action) {

        # --- 探活：确认通道是通的、而且确实是管理员在跑 ---
        'whoami' {
            $id = [Security.Principal.WindowsIdentity]::GetCurrent()
            $pr = [Security.Principal.WindowsPrincipal]::new($id)
            $isAdmin = $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
            return "以 $($id.Name) 运行，管理员=$isAdmin"
        }

        # --- 回显参数：验证参数真的传进来了，且没有任何副作用 ---
        'echo' {
            return "参数原样返回：" + ($Params | ConvertTo-Json -Compress -Depth 5)
        }

        # --- 重启一个服务。服务名必须在下面的固定列表里 ---
        'restart-service' {
            $allowed = @('wuauserv', 'Spooler', 'Winmgmt', 'BITS')
            $name = [string]$Params.service
            if ($name -notin $allowed) {
                throw "服务「$name」不在白名单里。可用的：$($allowed -join '、')"
            }
            Restart-Service -Name $name -Force
            return "已重启服务 $name"
        }

        # --- 启动具名程序（路径写死，调用方只能选名字）---
        #
        # 这里**只接受名字**，不接受路径。允许传路径的话这个动作就变成了
        # run-anything，白名单等于没有。加新程序 = 在下表加一行 + 重跑安装脚本。
        'launch' {
            $programs = [ordered]@{
                '原神'      = 'G:\Genshin Impact Game\YuanShen.exe'
                '绝区零'    = 'G:\ZenlessZoneZero Game\ZenlessZoneZero.exe'
                '星铁'      = 'D:\Star Rail Game\StarRail.exe'
                '崩坏星穹铁道' = 'D:\Star Rail Game\StarRail.exe'
                'Steam'     = 'C:\Program Files (x86)\Steam\steam.exe'
                'BetterGI'  = 'F:\BetterGI\BetterGI.exe'
            }
            $name = [string]$Params.name
            if ([string]::IsNullOrWhiteSpace($name)) {
                throw "要指定启动哪个。可用的：$($programs.Keys -join '、')"
            }
            # 用 -ieq 逐个比，**不能**用 $programs.Contains()：
            # OrderedDictionary 的 Contains 区分大小写，群里写 bettergi
            # 而表里是 BetterGI 就会匹配不上。
            $key = $programs.Keys | Where-Object { $_ -ieq $name } | Select-Object -First 1
            if (-not $key) {
                throw "「$name」不在列表里。可用的：$($programs.Keys -join '、')"
            }
            $exe = $programs[$key]
            if (-not (Test-Path -LiteralPath $exe)) {
                throw "找不到 $exe（是不是挪位置了？）"
            }
            # **不要加 -Verb RunAs**。
            # 计划任务已经是完整管理员令牌，普通 Start-Process 会让子进程
            # 直接继承这个令牌（CreateProcess 路径），不会触发任何提权请求。
            # 而 -Verb RunAs 是显式走 ShellExecute 的 "runas" 动词，
            # 由 AppInfo 服务处理 —— 那是一条独立的提权请求路径，
            # 已经提权的情况下也可能弹同意框。要提权的是计划任务，不是这条命令。
            Start-Process -FilePath $exe
            return "已启动 $key（以管理员权限）"
        }

        # --- 列出可启动的程序 ---
        'list-programs' {
            $programs = [ordered]@{
                '原神'      = 'G:\Genshin Impact Game\YuanShen.exe'
                '绝区零'    = 'G:\ZenlessZoneZero Game\ZenlessZoneZero.exe'
                '星铁'      = 'D:\Star Rail Game\StarRail.exe'
                '崩坏星穹铁道' = 'D:\Star Rail Game\StarRail.exe'
                'Steam'     = 'C:\Program Files (x86)\Steam\steam.exe'
                'BetterGI'  = 'F:\BetterGI\BetterGI.exe'
            }
            $lines = foreach ($k in $programs.Keys) {
                $exists = if (Test-Path -LiteralPath $programs[$k]) { "" } else { "  <-- 路径不存在" }
                "$k$exists"
            }
            return ($lines -join "、")
        }

        # --- BetterGI 一条龙 ---
        #
        # 走命令行参数，不点 UI。参数见 BetterGI 官方文档「命令行启动」。
        'onekey' {
            $exe = 'F:\BetterGI\BetterGI.exe'
            $dir = 'F:\BetterGI'
            if (-not (Test-Path -LiteralPath $exe)) {
                throw "找不到 $exe"
            }
            $cfg = [string]$Params.config
            if ([string]::IsNullOrWhiteSpace($cfg)) {
                # 不指定配置：走 URL 协议，任意目录都能用，最稳
                Start-Process 'bettergi://startOneDragon'
                return "已让 BetterGI 跑一条龙（页面上选定的配置）"
            }
            # 指定配置：没有对应的 URL 语法，只能用 exe 形式，
            # 而 exe 形式**要求当前目录是 BetterGI 目录**，所以显式设 WorkingDirectory。
            # 配置名可能带空格，用引号包住再拼成一个参数串。
            $argLine = "--startOneDragon `"$cfg`""
            Start-Process -FilePath $exe -ArgumentList $argLine -WorkingDirectory $dir
            return "已让 BetterGI 跑一条龙：$cfg"
        }

        # --- BetterGI 调度器：跑指定的配置组 ---
        #
        #   用法：--startGroups 组名1 组名2
        # 组名来自 BetterGI「全自动 - 调度器」里配置的组。
        'bettergi-groups' {
            $exe = 'F:\BetterGI\BetterGI.exe'
            if (-not (Test-Path -LiteralPath $exe)) { throw "找不到 $exe" }
            $names = @($Params.groups)
            if ($names.Count -eq 0) { throw "要指定配置组名，可以多个（空格分隔）" }
            $argLine = "--startGroups " + ($names -join ' ')
            Start-Process -FilePath $exe -ArgumentList $argLine -WorkingDirectory 'F:\BetterGI'
            return "已让 BetterGI 执行配置组：" + ($names -join '、')
        }

        # --- 列出 BetterGI 的一条龙配置 ---
        'bettergi-configs' {
            $dir = 'F:\BetterGI\User\OneDragon'
            if (-not (Test-Path -LiteralPath $dir)) { throw "找不到一条龙配置目录 $dir" }
            $names = Get-ChildItem -LiteralPath $dir -Filter '*.json' -File |
                     ForEach-Object { $_.BaseName }
            if (-not $names) { return "（没有一条龙配置）" }
            return ($names -join '、')
        }

        # --- 刷 DNS 缓存 ---
        'flush-dns' {
            ipconfig /flushdns | Out-String | ForEach-Object { $_.Trim() }
            return "DNS 缓存已清"
        }

        # ------------------------------------------------------------------
        #  这里往下加你自己的动作。例如：
        #
        #  'install-某软件' {
        #      winget install --id Some.App --silent --accept-package-agreements
        #      return "装完了"
        #  }
        #
        #  注意别写成 'run-anything' { Invoke-Expression $Params.cmd } ——
        #  那就等于把管理员权限整个交出去了。
        # ------------------------------------------------------------------

        default {
            throw "未知动作「$Action」。白名单里没有它。"
        }
    }
}

# --------------------------------------------------------------------------
#  主流程
# --------------------------------------------------------------------------
try {
    if (-not (Test-Path -LiteralPath $Request)) {
        throw "找不到请求文件 $Request"
    }
    $raw = Get-Content -LiteralPath $Request -Raw -Encoding UTF8
    $req = $raw | ConvertFrom-Json
    $action = [string]$req.action
    if ([string]::IsNullOrWhiteSpace($action)) {
        throw "请求里没有 action"
    }

    Write-Log "动作=$action 参数=$($req.args | ConvertTo-Json -Compress -Depth 3)"

    $output = Invoke-Action -Action $action -Params $req.args
    Set-Result -Text ([string]$output) -Ok $true
    Write-Log "成功：$action"
}
catch {
    $msg = $_.Exception.Message
    Set-Result -Text $msg -Ok $false
    Write-Log "失败：$msg"
}
finally {
    # 请求只生效一次：跑完就删，免得重复触发同一个动作。
    Remove-Item -LiteralPath $Request -Force -ErrorAction SilentlyContinue
}
