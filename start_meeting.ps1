<#
  start_meeting.ps1 - 会議の直前（10分前）に実行する

  実行方法:
    Set-ExecutionPolicy -Scope Process Bypass
    .\start_meeting.ps1                                   # PLaMo 3 Translate 2B で起動
    .\start_meeting.ps1 -Context 'Quarterly sales review between the Tokyo and US teams.'
    .\start_meeting.ps1 -Form plain                       # 訳を常体（だ・である調）にする（PLaMo のみ）
    .\start_meeting.ps1 -Threads 4 -PartialGap 6          # 長時間で遅れてくる時（CPU の負荷を下げる）
    .\start_meeting.ps1 -Model mt-tg                      # TranslateGemma で起動
    .\start_meeting.ps1 -Model mt-qwen -NoProxy           # プロキシなし（Translator → Ollama 直接。Qwen / Gemma のみ）
    .\start_meeting.ps1 -TranslatorPath 'C:\mt\LiveCaptions-Translator\LiveCaptionsTranslator.exe'
    .\start_meeting.ps1 -AddProxyException                # Windows のプロキシ例外に 127.0.0.1;localhost を追加

  やること:
    1. ネット接続・Ollama の待ち受けアドレスを確認（外部送信しない状態か）
    2. Ollama とモデルを起動して暖機（1文目の遅れをなくす）
    3. 翻訳プロキシを別ウィンドウで起動（置換表 fixes.tsv・用語集 glossary.tsv を自動で読み込む）
    4. Translator を起動し、確認すべき設定を表示
#>
param(
    [string]$Model = 'mt-plamo',
    [string]$Context = '',
    [ValidateSet('none', 'plain', 'polite')][string]$Form = 'none',
    [int]$History = 2,
    [int]$Threads = 0,          # Ollama の CPU スレッド数（0 = Modelfile の設定 8）。遅れる時は 4〜6 を試す
    [double]$PartialGap = 4,    # 話している途中の文を訳す最短間隔（秒）。大きいほど CPU に余裕ができる
    [switch]$NoProxy,
    [int]$ProxyPort = 11500,
    [string]$TranslatorPath = (Join-Path $PSScriptRoot 'LiveCaptions-Translator\LiveCaptionsTranslator.exe'),
    [switch]$AddProxyException,
    [switch]$Quiet
)

$ErrorActionPreference = 'Continue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Set-Location $PSScriptRoot
$Ollama = 'http://127.0.0.1:11434'

# このスクリプト自身の通信は PC 内（127.0.0.1）だけなので、社内プロキシを使わない
[Net.WebRequest]::DefaultWebProxy = New-Object Net.WebProxy
try { [Net.Http.HttpClient]::DefaultProxy = New-Object Net.WebProxy } catch { }

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  !!  $msg" -ForegroundColor Yellow }
function Test-Url($url) { try { Invoke-RestMethod $url -TimeoutSec 3 | Out-Null; $true } catch { $false } }

$mode = if ($Model -match 'plamo') { 'plamo' } elseif ($Model -match 'tg|translategemma') { 'tg' } else { 'passthrough' }

if ($NoProxy -and $mode -eq 'plamo') {
    Write-Host 'PLaMo は翻訳プロキシが必要です（入力形式が専用のため）。-NoProxy を外して実行してください。' -ForegroundColor Red; exit 1
}
if ($NoProxy -and $mode -eq 'tg') {
    Warn 'TranslateGemma はプロキシなしだとプロンプト形式が合わず、訳が不安定になります。-NoProxy を外すことを推奨します。'
}

# ---------------------------------------------------------------
Step '1. 外部送信しない状態かの確認'
$online = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object { $_.IPv4Connectivity -eq 'Internet' -or $_.IPv6Connectivity -eq 'Internet' }
if ($online) { Warn "インターネットに接続中です（$($online.Name -join ', ')）。この構成は外部送信しませんが、念のため Wi-Fi を切っても動作します。" }
else { Ok 'オフライン' }

$envHost = [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'User') + [Environment]::GetEnvironmentVariable('OLLAMA_HOST', 'Machine')
if ($envHost -match '0\.0\.0\.0|::') { Warn "OLLAMA_HOST=$envHost（外部から接続可能な設定）です。削除して Ollama を再起動してください。" }

# Translator から PC 内（127.0.0.1）への通信が、社内プロキシ経由にならないかの確認
$isKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
$is = Get-ItemProperty $isKey -ErrorAction SilentlyContinue
if ($is.ProxyEnable -eq 1 -and $is.ProxyServer) {
    $override = [string]$is.ProxyOverride
    $entries = @($override -split ';' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if (@($entries | Where-Object { $_ -in @('127.0.0.1', 'localhost', '127.*') }).Count -gt 0) {
        Ok 'Windows のプロキシ例外に 127.0.0.1 / localhost が入っています（PC 内の通信は直接）'
    } elseif ($AddProxyException) {
        Set-ItemProperty $isKey -Name ProxyOverride -Value (($entries + @('127.0.0.1', 'localhost')) -join ';')
        Ok 'Windows のプロキシ例外に 127.0.0.1;localhost を追加しました（起動中の Translator は再起動すると反映）'
    } else {
        Warn 'Windows のプロキシ例外に 127.0.0.1 がありません。Translator から PC 内への通信（会議の文字起こし）が社内プロキシ経由になるおそれがあります。'
        Warn '  -AddProxyException を付けて実行するか、設定 > ネットワークとインターネット > プロキシ の例外に「127.0.0.1;localhost」を追加してください。'
    }
} elseif ($is.AutoConfigURL) {
    Warn '自動構成スクリプト（PAC）でプロキシが設定されています。PC 内の通信が直接つながるかは、Wi-Fi を切ったリハーサルで訳が出るかで確認してください。'
}

# ---------------------------------------------------------------
Step '2. Ollama の起動とモデルの暖機'
if (-not (Test-Url "$Ollama/api/version")) {
    $app = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'
    if (Test-Path $app) { Start-Process $app } else { Start-Process 'ollama' -ArgumentList 'serve' -WindowStyle Hidden }
    for ($i = 0; $i -lt 30 -and -not (Test-Url "$Ollama/api/version"); $i++) { Start-Sleep 1 }
}
if (-not (Test-Url "$Ollama/api/version")) { Write-Host 'Ollama を起動できませんでした。' -ForegroundColor Red; exit 1 }
Ok 'Ollama 起動中'

$tags = (Invoke-RestMethod "$Ollama/api/tags").models.name
if (-not ($tags -contains "${Model}:latest" -or $tags -contains $Model)) {
    Write-Host "モデル $Model が見つかりません。setup.ps1 を実行済みか確認してください。" -ForegroundColor Red; exit 1
}

# 暖機：モデルをメモリに読み込み、60分間保持（本番と同じ形式の入力を1回送る）
if ($mode -eq 'plamo') {
    $path = '/api/generate'
    $body = @{ model = $Model; stream = $false; keep_alive = '60m'; raw = $true
               prompt = '<|plamo:tag|>dataset<|plamo:msg|>translation<|plamo:tag|>input<|plamo:key|>lang<|plamo:val|>en<|plamo:msg|>Good morning.' +
                        '<|plamo:tag|>output<|plamo:key|>lang<|plamo:val|>ja<|plamo:msg|>'
               options = @{ temperature = 0; num_predict = 20; stop = @('<|plamo:tag|>', '<|plamo:bos|>') } }
} else {
    $path = '/api/chat'
    $body = @{ model = $Model; stream = $false; keep_alive = '60m'
               messages = @(@{ role = 'user'; content = 'Translate into Japanese: Good morning.' }) }
    if ($mode -eq 'passthrough') { $body.think = $false }
}
$sw = [Diagnostics.Stopwatch]::StartNew()
try {
    $res = Invoke-WebRequest "$Ollama$path" -Method Post -UseBasicParsing -TimeoutSec 180 `
        -ContentType 'application/json; charset=utf-8' `
        -Body ([Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Depth 5 -Compress)))
    $obj = [Text.Encoding]::UTF8.GetString($res.RawContentStream.ToArray()) | ConvertFrom-Json
    $sample = if ($mode -eq 'plamo') { $obj.response } else { $obj.message.content }
    Ok ("{0} を読み込み済み（{1:N1} 秒）  例: Good morning. → {2}" -f $Model, $sw.Elapsed.TotalSeconds, ($sample -replace '<\|plamo:[a-z]+\|>', '').Trim())
} catch { Warn "暖機に失敗しました: $($_.Exception.Message)" }

# ---------------------------------------------------------------
$apiUrl = $Ollama
if (-not $NoProxy) {
    Step '3. 翻訳プロキシの起動'
    $py = (Get-Command py -ErrorAction SilentlyContinue).Source
    if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $py) { Write-Host 'Python が見つかりません。setup.ps1 を実行するか、-NoProxy を付けて実行してください。' -ForegroundColor Red; exit 1 }

    $running = $null
    try { $running = Invoke-RestMethod "http://127.0.0.1:$ProxyPort/proxy/status" -TimeoutSec 2 } catch { }
    if ($running) {
        if ($running.model -ne $Model) {
            Warn "別のモデル（$($running.model)）でプロキシが起動中です。そのウィンドウを Ctrl+C で閉じてから再実行してください。"
        } else { Ok "プロキシは起動済み（ポート $ProxyPort、$($running.model)）" }
    } else {
        $pyArgs = @('mt_proxy.py', '--mode', $mode, '--model', $Model, '--port', $ProxyPort,
                    '--partial-gap', $PartialGap, '--threads', $Threads)
        if (Test-Path 'fixes.tsv') { $pyArgs += @('--fix', 'fixes.tsv') }
        if ($mode -eq 'plamo') {
            if (Test-Path 'glossary.tsv') { $pyArgs += @('--glossary', 'glossary.tsv') }
            $pyArgs += @('--history', $History, '--form', $Form)
            if ($Context) { $pyArgs += @('--context', ('"' + ($Context -replace '"', "'") + '"')) }
        }
        if ($Quiet) { $pyArgs += '--quiet' }
        Start-Process $py -ArgumentList $pyArgs -WorkingDirectory $PSScriptRoot
        for ($i = 0; $i -lt 10 -and -not (Test-Url "http://127.0.0.1:$ProxyPort/proxy/status"); $i++) { Start-Sleep 1 }
        if (Test-Url "http://127.0.0.1:$ProxyPort/proxy/status") { Ok "プロキシ起動（mode=$mode、別ウィンドウに1文ごとの翻訳時間が流れます）" }
        else { Write-Host 'プロキシが起動しませんでした。別ウィンドウのエラーを確認してください。' -ForegroundColor Red; exit 1 }
    }
    $apiUrl = "http://127.0.0.1:$ProxyPort"
}

# 待ち受けアドレスの確認（0.0.0.0 などで待ち受けていないこと）
$ports = @(11434) + $(if (-not $NoProxy) { $ProxyPort } else { @() })
$listen = Get-NetTCPConnection -State Listen -LocalPort $ports -ErrorAction SilentlyContinue
$exposed = $listen | Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') }
if ($exposed) { Warn ("ローカル以外で待ち受けています: " + (($exposed | ForEach-Object { "$($_.LocalAddress):$($_.LocalPort)" }) -join ', ')) }
else { Ok 'Ollama / プロキシとも 127.0.0.1 のみで待ち受け' }

# ---------------------------------------------------------------
Step '4. Translator の起動'
if (Test-Path $TranslatorPath) {
    if (-not (Get-Process -Name 'LiveCaptionsTranslator' -ErrorAction SilentlyContinue)) {
        Start-Process $TranslatorPath -WorkingDirectory (Split-Path $TranslatorPath)
    }
    Ok 'LiveCaptions-Translator 起動'
} else {
    Warn "Translator が見つかりません: $TranslatorPath（手動で起動してください）"
}

$modelForTranslator = if ($NoProxy) { $Model } else { "$Model（プロキシが差し替えるので何でも可）" }
Write-Host @"

----------------------------------------------------------------
 Translator の設定（初回のみ。2回目以降は保存されています）
   Translate API     : Ollama
   API Setting
     API URL         : $apiUrl
     Model Name      : $modelForTranslator
   Target Language   : ja-JP
   API Interval      : 5〜8（小さいほど話の途中でも訳すが CPU 負荷が上がる）
   Context Aware     : オフ（PLaMo は文脈をプロキシが渡すので不要）
 ライブキャプション側
   言語              : 英語（米国）
   マイクの音声を含める : オン（対面会議）
   既定の入力デバイス  : 使うマイク（設定 > システム > サウンド > 入力）
----------------------------------------------------------------
 終了時: Translator を閉じ、プロキシのウィンドウで Ctrl+C。
 会議内容は Translator フォルダの translation_history.db に残ります。
 不要なら Translator の履歴画面から削除してください。
"@
