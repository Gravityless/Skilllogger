<#
.SYNOPSIS
    Skill 数据埋点 client 脚本（Windows PowerShell 5.1+ / PowerShell 7+）。

.DESCRIPTION
    上报当前用户调用 skill 的事件到 telemetry server。
    特性：
      - 全程静默：不输出任何 stdout/stderr，绝不抛异常给调用方（大模型）
      - 离线容错：server 不可达时，事件落到本地 JSONL 队列；下次调用时优先补传
      - 跨用户：使用 $env:USERNAME 与 $env:LOCALAPPDATA，不依赖固定路径
      - 兼容性：PS 5.1 / 7+；用 -NoProfile -ExecutionPolicy Bypass 启动可避开本地策略
      - 短超时（3s），不阻塞 skill 主流程

.PARAMETER SkillName
    必填。本次被调用的 skill 名称。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass `
      -File "scripts/telemetry_client.ps1" -SkillName "weather"

.NOTES
    Server 地址：默认 http://localhost:8000，可通过环境变量 SKILL_TELEMETRY_URL 覆盖。
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $false)]
    [string]$SkillName = ''
)

# ---- 全程静默：吞掉所有错误，绝不影响 skill 主流程 ----
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference   = 'SilentlyContinue'

# 缺参数或空白 → 静默退出（兼容大模型偶尔漏传参的情况）
if ([string]::IsNullOrWhiteSpace($SkillName)) { exit 0 }

try {

    # ---- 配置 ----
    $ClientVersion = '1.0.0'
    $TimeoutSec    = 3
    $ServerUrl     = if ($env:SKILL_TELEMETRY_URL) { $env:SKILL_TELEMETRY_URL } else { 'http://localhost:8000' }
    $ServerUrl     = $ServerUrl.TrimEnd('/')

    # ---- 本地队列路径（兼容中文用户名）----
    $QueueDir  = Join-Path $env:LOCALAPPDATA 'SkillTelemetry'
    $QueueFile = Join-Path $QueueDir 'queue.jsonl'
    if (-not (Test-Path -LiteralPath $QueueDir)) {
        [void](New-Item -ItemType Directory -Path $QueueDir -Force)
    }

    # ---- 构造当前事件 ----
    $event = [ordered]@{
        username       = $env:USERNAME
        skill          = $SkillName
        hostname       = $env:COMPUTERNAME
        timestamp      = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ')
        client_version = $ClientVersion
    }

    # 兼容 PS 5.1：ConvertTo-Json -Compress 单行
    $eventJson = $event | ConvertTo-Json -Compress -Depth 5

    # ---- 通用 POST helper ----
    function Invoke-Post {
        param(
            [string]$Url,
            [string]$JsonBody
        )
        # 用 .NET 直接发请求，避免 Invoke-RestMethod 在某些环境下的代理/编码问题
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($JsonBody)
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Method        = 'POST'
        $req.ContentType   = 'application/json; charset=utf-8'
        $req.Timeout       = $TimeoutSec * 1000
        $req.ReadWriteTimeout = $TimeoutSec * 1000
        $req.ContentLength = $bytes.Length
        # 显式跳过系统代理可能造成的延迟
        $req.Proxy = $null
        $stream = $req.GetRequestStream()
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Close()
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return $code
    }

    # ---- 辅助：把一个文件的内容安全地 append 回主队列（用于回滚 / 孤儿回收）----
    function Append-FileToQueue {
        param([string]$SrcPath)
        try {
            if (-not (Test-Path -LiteralPath $SrcPath)) { return }
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            $content = [System.IO.File]::ReadAllText($SrcPath, $utf8NoBom)
            if (-not [string]::IsNullOrEmpty($content)) {
                # 确保以换行结尾，避免下一行与本段粘连
                if ($content[-1] -ne "`n") { $content = $content + "`n" }
                [System.IO.File]::AppendAllText($QueueFile, $content, $utf8NoBom)
            }
            Remove-Item -LiteralPath $SrcPath -Force -ErrorAction SilentlyContinue
        } catch { }
    }

    # ---- Step 0: 回收孤儿 sending 文件（上一个进程被强杀时可能残留）----
    # 用「rename 认领」防止多 client 同时回收同一孤儿造成 queue 重复
    try {
        $orphans = @(Get-ChildItem -LiteralPath $QueueDir -Filter 'queue.sending.*.jsonl' -File -ErrorAction SilentlyContinue)
        foreach ($orphan in $orphans) {
            # 只回收「已经不在被写入」的文件：过期 60 秒以上视为孤儿
            # （正在进行的补传，其 sending 文件生命周期通常在秒级内）
            $ageSec = ((Get-Date) - $orphan.LastWriteTime).TotalSeconds
            if ($ageSec -gt 60) {
                # 原子认领：Move-Item 成功 → 本实例独占；失败 → 已被其它实例抢走
                # 仍用 queue.sending.* 命名，万一本实例又被杀掉，下次 Step 0 还能再次回收
                $claimName = "queue.sending.recover.{0}.jsonl" -f ([guid]::NewGuid().ToString('N'))
                $claimPath = Join-Path $QueueDir $claimName
                try {
                    Move-Item -LiteralPath $orphan.FullName -Destination $claimPath -ErrorAction Stop
                    Append-FileToQueue -SrcPath $claimPath
                } catch { }
            }
        }
    } catch { }

    # ---- Step 1: 优先补传本地队列 ----
    $queueSent = $false
    if (Test-Path -LiteralPath $QueueFile) {
        try {
            # 原子重命名，避免与并发写入冲突
            $tempName = "queue.sending.{0}.jsonl" -f ([guid]::NewGuid().ToString('N'))
            $tempPath = Join-Path $QueueDir $tempName
            Move-Item -LiteralPath $QueueFile -Destination $tempPath -Force

            $lines = @(Get-Content -LiteralPath $tempPath -Encoding UTF8 -ErrorAction SilentlyContinue)
            $events = @()
            foreach ($line in $lines) {
                if ([string]::IsNullOrWhiteSpace($line)) { continue }
                try {
                    $obj = $line | ConvertFrom-Json -ErrorAction Stop
                    $events += $obj
                } catch { }
            }

            if ($events.Count -gt 0) {
                $batchJson = @{ events = $events } | ConvertTo-Json -Compress -Depth 6
                try {
                    $code = Invoke-Post -Url "$ServerUrl/track/batch" -JsonBody $batchJson
                    if ($code -ge 200 -and $code -lt 300) {
                        $queueSent = $true
                        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
                    } else {
                        # 失败：append 回主队列（而非覆盖），保护期间产生的新事件
                        Append-FileToQueue -SrcPath $tempPath
                    }
                } catch {
                    Append-FileToQueue -SrcPath $tempPath
                }
            } else {
                Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
            }
        } catch { }
    }

    # ---- Step 2: 上报当前事件 ----
    $sent = $false
    try {
        $code = Invoke-Post -Url "$ServerUrl/track" -JsonBody $eventJson
        if ($code -ge 200 -and $code -lt 300) { $sent = $true }
    } catch { }

    # ---- Step 3: 失败则入队 ----
    if (-not $sent) {
        try {
            # 原子追加单行 JSON（utf-8 无 BOM）
            $line = $eventJson + "`n"
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::AppendAllText($QueueFile, $line, $utf8NoBom)
        } catch { }
    }

} catch {
    # 任何意外都吞掉
}

# 永远以 0 退出，保证不打断 skill 主流程
exit 0
