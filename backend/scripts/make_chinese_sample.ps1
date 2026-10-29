$ErrorActionPreference = 'Stop'
# 使用本机 Windows 自带中文语音和字体，内容由本项目自编。
$studyRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$studyOutput = Join-Path $studyRoot 'data/validation/chinese'
New-Item -ItemType Directory -Path $studyOutput -Force | Out-Null
Add-Type -AssemblyName System.Speech
Add-Type -AssemblyName System.Drawing
$studySynth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$studySynth.SelectVoice('Microsoft Huihui Desktop')
$studySynth.Rate = -1
$studyHeadings = @('TCP 拥塞控制', '慢启动', '拥塞避免')
$studyBodies = @("根据网络状况调整发送速度`n避免发送过多数据", "拥塞窗口从较小值开始`n随着确认到达而增长", "更谨慎地增加拥塞窗口`n丢包后降低发送速度")
$studyNarrations = @(
    '欢迎学习计算机网络。本节介绍传输控制协议的拥塞控制。发送方需要根据网络状况调整发送速度，避免一次发送过多数据。我们接下来分别介绍慢启动和拥塞避免。请把总结和原始讲解对照，检查是否遗漏了条件。',
    '现在介绍慢启动。拥塞窗口从一个较小的值开始。随着确认消息不断到达，发送方逐步扩大拥塞窗口。慢启动阶段的窗口增长可以很快，不能仅凭名称就认为增长总是缓慢。达到阈值后，进入后续阶段。',
    '最后介绍拥塞避免。这个阶段采用更加谨慎的方式增加拥塞窗口。目标是在利用可用带宽的同时，避免让网络过载。丢包可能提示网络拥塞，此时发送方需要降低发送速度。学习时需要区分资料提到概念和自己掌握概念。'
)
try {
    for ($studyIndex = 0; $studyIndex -lt 3; $studyIndex++) {
        $studySynth.SetOutputToWaveFile((Join-Path $studyOutput "voice-$studyIndex.wav"))
        $studySynth.Speak($studyNarrations[$studyIndex])
        $studySynth.SetOutputToNull()
        $studyBitmap = New-Object System.Drawing.Bitmap 1280,720
        $studyGraphics = [System.Drawing.Graphics]::FromImage($studyBitmap)
        $studyGraphics.Clear([System.Drawing.Color]::FromArgb(24,59,53))
        $studyFont = New-Object System.Drawing.Font 'Microsoft YaHei',42
        $studyBodyFont = New-Object System.Drawing.Font 'Microsoft YaHei',28
        $studyGraphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
        $studyGraphics.DrawString($studyHeadings[$studyIndex], $studyFont, [System.Drawing.Brushes]::White, 100, 170)
        $studyGraphics.DrawString($studyBodies[$studyIndex], $studyBodyFont, [System.Drawing.Brushes]::White, 100, 300)
        $studyBitmap.Save((Join-Path $studyOutput "slide-$studyIndex.png"), [System.Drawing.Imaging.ImageFormat]::Png)
        $studyFont.Dispose(); $studyBodyFont.Dispose(); $studyGraphics.Dispose(); $studyBitmap.Dispose()
    }
} finally { $studySynth.Dispose() }
Write-Output "已生成中文测试音频和画面：$studyOutput"
