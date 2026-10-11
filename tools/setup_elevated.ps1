# ==========================================================================
#  setup_elevated.ps1 —— 一次性安装「预授权提权通道」
#  ------------------------------------------------------------------------
#  **必须以管理员身份运行**（因为它要建计划任务 + 设 ACL）：
#
#      右键 PowerShell → 以管理员身份运行，然后
#      cd C:\wechat_addon
#      powershell -ExecutionPolicy Bypass -File tools\setup_elevated.ps1
#
#  装完之后，agent 就能在**不需要 UAC** 的情况下触发白名单里的少数几个管理操作。
#
#  ## 这个脚本做的事
#
#    1. 建 %ProgramData%\WeChatElevated\
#    2. 把 tools\elevated\run.ps1 复制过去，并**设成仅 Administrators/SYSTEM 可写**
#    3. 建 data\ 子目录，**只对这一层放开**写入（agent 靠它递请求）
#    4. 注册一个「最高权限、不需要 UAC」的计划任务 WeChatElevated
#
#  ## 为什么 ACL 是安全性的核心，不是可选项
#
#  agent 有 dsh 的 danger-full-access，能改本机大部分文件。如果 run.ps1
#  放在它改得动的地方，它就能把自己想跑的命令写进去再触发任务 ——
#  等于直接拿到管理员权限，这个"预授权"设计就完全失效了。
#
#  锁成仅管理员可写之后：非提权进程的令牌里 Administrators 是 deny-only，
#  所以 agent **写不进去**；而计划任务用完整管理员令牌跑，读得了。
#
#  重跑一次这个脚本可以更新 run.ps1（改完白名单动作之后需要重跑）。
# ==========================================================================

#Requires -RunAsAdministrator

$ErrorActionPreference = 'Stop'

$Source     = Join-Path $PSScriptRoot 'elevated\run.ps1'
$TargetDir  = Join-Path $env:ProgramData 'WeChatElevated'
$TargetPs1  = Join-Path $TargetDir 'run.ps1'
$DataDir    = Join-Path $TargetDir 'data'
$TaskName   = 'WeChatElevated'
$Me         = "$env:USERDOMAIN\$env:USERNAME"

function Say([string]$t, [string]$color = 'Gray') {
    Write-Host "  $t" -ForegroundColor $color
}

Write-Host "`n=== 安装预授权提权通道 ===`n" -ForegroundColor Cyan

# --- 0. 前置检查 -----------------------------------------------------------
if (-not (Test-Path -LiteralPath $Source)) {
    throw "找不到 $Source"
}
$isAdmin = ([Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent())
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "这个脚本必须以管理员身份运行（要建计划任务和设 ACL）。"
}
Say "源脚本: $Source"

# --- 1. 目录 + 复制 --------------------------------------------------------
New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
New-Item -ItemType Directory -Path $DataDir   -Force | Out-Null
Copy-Item -LiteralPath $Source -Destination $TargetPs1 -Force
Say "已复制到 $TargetPs1" 'Green'

# --- 2. ACL：脚本仅管理员可写 ---------------------------------------------
# 先把继承关掉，否则 ProgramData 的继承规则会把这个限制冲掉。
$acl = Get-Acl -LiteralPath $TargetDir
$acl.SetAccessRuleProtection($true, $false)   # 断开继承，不保留继承来的规则

foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRule($rule) | Out-Null }

# 目录本身：管理员和 SYSTEM 完全控制
foreach ($who in @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM')) {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $who, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
}
# 普通用户只能**读和进入**，不能写 —— 这正是防护所在
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    'BUILTIN\Users', 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
Set-Acl -LiteralPath $TargetDir -AclObject $acl
Say "已锁定 $TargetDir（仅管理员可写）" 'Green'

# --- 3. data\ 单独放开写入 -------------------------------------------------
# agent 要能写 request.json、读 result.txt、看 run.log。
# 注意这层放开是安全的：里面只有"选哪个动作"的请求，动作本身在白名单里。
$dacl = New-Object Security.AccessControl.DirectorySecurity
$dacl.SetAccessRuleProtection($true, $false)
foreach ($who in @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM')) {
    $dacl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $who, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
}
$dacl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    $Me, 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
$dacl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
    'BUILTIN\Users', 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
Set-Acl -LiteralPath $DataDir -AclObject $dacl
Say "已放开 $DataDir 的写入（agent 靠它递请求）" 'Green'

# --- 4. 计划任务 -----------------------------------------------------------
# Interactive + Highest：不用存密码，以完整管理员令牌运行，**且不弹 UAC**。
$arg = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$TargetPs1`""
$taskAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arg
$principal  = New-ScheduledTaskPrincipal -UserId $Me -LogonType Interactive -RunLevel Highest
$settings   = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Say "已移除旧任务，重新注册"
}
Register-ScheduledTask -TaskName $TaskName -Action $taskAction `
    -Principal $principal -Settings $settings `
    -Description '以管理员权限执行微信机器人预先批准的操作（见 run.ps1 白名单）' | Out-Null
Say "已注册计划任务 $TaskName（最高权限，不弹 UAC）" 'Green'

# --- 5. 验证 ---------------------------------------------------------------
Write-Host "`n=== 自检 ===`n" -ForegroundColor Cyan

# 5a. 确认 ACL 真的挡住了普通用户
$testFile = Join-Path $TargetDir 'acl_probe.txt'
$canWrite = $true
try { [System.IO.File]::WriteAllText($testFile, 'x') } catch { $canWrite = $false }
if ($canWrite) {
    Remove-Item -LiteralPath $testFile -Force -ErrorAction SilentlyContinue
    Say "!! 警告：当前（已提权）会话能写 $TargetDir，属正常。" 'Yellow'
    Say "   但要确认**非提权**进程写不了 —— 装完后从普通窗口试：" 'Yellow'
    Say "   echo x > `"$TargetDir\probe.txt`"   （应该失败）" 'Yellow'
} else {
    Say "ACL 生效：连这个提权会话写目录根都被拒（意外，检查一下）" 'Yellow'
}

# 5b. 真的跑一次通道
#
# 测两个动作，缺一不可：
#   whoami —— 证明通道通、而且确实是管理员在跑
#   echo   —— 证明**参数真的传进来了**
#
# 为什么要单独测参数：第一版把函数参数命名成了 $Args，那是 PowerShell 的
# 自动变量，撞名之后 $Args.service 永远是空的 —— 白名单动作配合法参数
# 也会被拒，报「服务  不在白名单里」。光跑 whoami 发现不了，因为它没参数。
function Test-Channel([string]$Action, [string]$Json) {
    $body = if ($Json) { "{""action"":""$Action"",""args"":$Json}" }
            else { "{""action"":""$Action""}" }
    [System.IO.File]::WriteAllText((Join-Path $DataDir 'request.json'),
        $body, [System.Text.UTF8Encoding]::new($false))
    Remove-Item (Join-Path $DataDir 'result.txt') -Force -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 500
        $rf = Join-Path $DataDir 'result.txt'
        if (Test-Path -LiteralPath $rf) {
            Start-Sleep -Milliseconds 200
            try { return (Get-Content -LiteralPath $rf -Raw -Encoding UTF8 | ConvertFrom-Json) }
            catch { continue }
        }
    }
    return $null
}

$r1 = Test-Channel 'whoami' ''
if ($r1) { Say "通道自检: $($r1.output)" $(if ($r1.ok) { 'Green' } else { 'Red' }) }
else     { Say "通道自检超时 —— 看任务计划程序里的 $TaskName" 'Red' }

$r2 = Test-Channel 'echo' '{"service":"Spooler","n":42}'
if ($r2 -and $r2.ok -and $r2.output -match 'Spooler') {
    Say "参数传递自检: OK  ($($r2.output))" 'Green'
} elseif ($r2 -and $r2.ok) {
    Say "!! 参数传递有问题：参数没到脚本里。输出=$($r2.output)" 'Red'
    Say "   检查 run.ps1 的 Invoke-Action 参数名是不是撞了 PowerShell 自动变量。" 'Red'
} else {
    Say "参数传递自检失败：$(if ($r2) { $r2.output } else { '超时' })" 'Red'
}

Write-Host "`n=== 装好了 ===`n" -ForegroundColor Cyan
Say "agent 现在可以这样用（不需要 UAC）：" 'White'
Say "  venv\Scripts\python.exe tools\elevated_request.py whoami" 'White'
Say "  venv\Scripts\python.exe tools\elevated_request.py flush-dns" 'White'
Say ""
Say "要加新的管理操作：改 tools\elevated\run.ps1 里的白名单，然后**重跑这个脚本**。" 'Yellow'
Say "改完不重跑 = 计划任务还在跑 ProgramData 里的旧副本。" 'Yellow'
