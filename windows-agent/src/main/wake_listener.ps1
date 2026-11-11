# AIVA Windows Local Wake Word Listener
# Uses native System.Speech.Recognition for 100% offline, zero-latency wake word detection
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech

try {
    $rec = New-Object System.Speech.Recognition.SpeechRecognitionEngine
    $choices = New-Object System.Speech.Recognition.Choices
    $choices.Add([string[]]@('Hey AIVA', 'AIVA', 'Hi AIVA', 'Hey Jarvis', 'Jarvis', 'Ok AIVA'))
    $gb = New-Object System.Speech.Recognition.GrammarBuilder($choices)
    $g = New-Object System.Speech.Recognition.Grammar($gb)
    $rec.LoadGrammar($g)
    $rec.SetInputToDefaultAudioDevice()

    Register-ObjectEvent -InputObject $rec -EventName SpeechRecognized -Action {
        $recognized = $EventArgs.Result.Text
        $confidence = [math]::Round($EventArgs.Result.Confidence, 2)
        if ($confidence -ge 0.45) {
            [Console]::WriteLine("WAKE_DETECTED|$recognized|$confidence")
            [Console]::Out.Flush()
        }
    } | Out-Null

    $rec.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Multiple)
    [Console]::WriteLine("WAKE_LISTENER_ACTIVE")
    [Console]::Out.Flush()

    # Keep background process alive
    while ($true) {
        Start-Sleep -Seconds 2
    }
} catch {
    [Console]::WriteLine("WAKE_ERROR|" + $_.Exception.Message)
    [Console]::Out.Flush()
}

