param([Parameter(Mandatory = $true)][string]$CacheRoot)

# One owned offline synthesizer. Input and output are UTF-8 JSON lines; no
# prompt is passed through PowerShell source, SSML, or command-line arguments.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$synth = $null

function Send-Event($Event) {
    [Console]::Out.WriteLine(($Event | ConvertTo-Json -Compress -Depth 8))
    [Console]::Out.Flush()
}

try {
    Add-Type -AssemblyName System.Speech
    $root = [IO.Path]::GetFullPath($CacheRoot).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
    $voices = @($synth.GetInstalledVoices() | Where-Object {
        $_.Enabled -and $_.VoiceInfo.Culture.TwoLetterISOLanguageName -eq 'en'
    } | ForEach-Object { $_.VoiceInfo.Name })
    Send-Event @{ event = 'voices'; voices = $voices }
    $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
        22050, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)

    while ($null -ne ($line = [Console]::In.ReadLine())) {
        $request = $null
        try {
            $request = $line | ConvertFrom-Json
            if ($request.command -eq 'shutdown') { break }
            if ($request.command -ne 'synthesize') { continue }
            $requestId = [string]$request.request_id
            if ($requestId -notmatch '^[0-9a-f]{32}$') { throw 'Invalid request' }
            $output = [IO.Path]::GetFullPath([string]$request.output)
            if ([IO.Path]::GetDirectoryName($output) -ne $root -or
                [IO.Path]::GetFileName($output) -notmatch '^pending-[0-9a-f]{32}\.wav$') {
                throw 'Invalid output'
            }
            $text = [string]$request.text
            $rate = [int]$request.rate
            if ([string]::IsNullOrWhiteSpace($text) -or $text.Length -gt 4000 -or
                $rate -lt -10 -or $rate -gt 10) { throw 'Invalid request' }
            if ($voices.Count -eq 0) {
                Send-Event @{ event = 'failed'; request_id = $requestId; code = 'no_english_voice' }
                continue
            }
            $voice = [string]$request.voice
            if ($voice -eq 'auto') {
                $voice = @('Microsoft Zira Desktop', 'Microsoft David Desktop',
                    'Microsoft Hazel Desktop') | Where-Object { $voices -contains $_ } | Select-Object -First 1
                if ([string]::IsNullOrWhiteSpace($voice)) { $voice = $voices[0] }
            }
            if ($voices -notcontains $voice) {
                Send-Event @{ event = 'failed'; request_id = $requestId; code = 'voice_unavailable' }
                continue
            }
            $watch = [Diagnostics.Stopwatch]::StartNew()
            $synth.SelectVoice($voice)
            $synth.Rate = $rate
            $synth.Volume = 100
            $synth.SetOutputToWaveFile($output, $format)
            try {
                # Speak(string) treats translated captions as plain text.
                $synth.Speak($text)
            } finally {
                # Releases the WAV handle and completes the RIFF header.
                $synth.SetOutputToNull()
            }
            $watch.Stop()
            Send-Event @{ event = 'ready'; request_id = $requestId; path = $output;
                voice = $voice; render_ms = $watch.ElapsedMilliseconds }
        } catch {
            # Fixed diagnostics deliberately exclude subtitle text and native
            # exception details. An incomplete WAV stays outside the cache key.
            Send-Event @{ event = 'failed'; request_id = [string]$request.request_id;
                code = 'render_failed' }
        }
    }
} catch {
    Send-Event @{ event = 'fatal'; code = 'engine_unavailable' }
    exit 1
} finally {
    if ($null -ne $synth) { $synth.Dispose() }
}
