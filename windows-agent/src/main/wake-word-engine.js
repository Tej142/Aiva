// =====================================================================
// AIVA Local Windows Wake Word Engine
// Native offline wake word detection for "Hey AIVA" / "Jarvis"
// Uses Windows System.Speech.Recognition via in-memory PowerShell script.
// =====================================================================

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');

const logFile = path.join(os.homedir(), 'AppData', 'Roaming', 'aiva-device-agent', 'wake-word.log');

function logWake(msg) {
  const line = `[${new Date().toISOString()}] ${msg}\n`;
  try {
    fs.appendFileSync(logFile, line);
  } catch {}
  console.log(msg);
}

// PowerShell script with native .NET System.Speech.Recognition
const PS_WAKE_SCRIPT = `
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
try {
    $rec = New-Object System.Speech.Recognition.SpeechRecognitionEngine
    $choices = New-Object System.Speech.Recognition.Choices
    $choices.Add([string[]]@(
        'Hey AIVA', 'AIVA', 'Hi AIVA', 'Ok AIVA',
        'Hey Jarvis', 'Jarvis',
        'Hey Ava', 'Ava',
        'Hey Eva', 'Eva',
        'Hey Iva', 'Iva',
        'Computer'
    ))
    $gb = New-Object System.Speech.Recognition.GrammarBuilder($choices)
    $rec.LoadGrammar((New-Object System.Speech.Recognition.Grammar($gb)))
    
    $rec.add_SpeechRecognized({
        param($s, $e)
        try {
            $txt = $e.Result.Text
            $conf = [math]::Round($e.Result.Confidence, 2)
            if ($conf -ge 0.15) {
                [Console]::WriteLine("WAKE_DETECTED|" + $txt + "|" + $conf)
                [Console]::Out.Flush()
            }
        } catch {}
    })
    
    $rec.SetInputToDefaultAudioDevice()
    $rec.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Multiple)
    [Console]::WriteLine("WAKE_LISTENER_ACTIVE")
    [Console]::Out.Flush()
    
    while ($true) {
        [System.Threading.Thread]::Sleep(1500)
    }
} catch {
    [Console]::WriteLine("WAKE_ERROR|" + $_.Exception.Message)
    [Console]::Out.Flush()
}
`;

const ENCODED_CMD = Buffer.from(PS_WAKE_SCRIPT, 'utf16le').toString('base64');

class LocalWakeWordEngine {
  constructor(onWakeWordDetected) {
    this.onWakeWord = onWakeWordDetected;
    this.process = null;
    this.isStopping = false;
    this.lastTriggerTime = 0;
  }

  start() {
    if (this.process) return;

    this.isStopping = false;
    const psPath = process.env.SystemRoot 
      ? path.join(process.env.SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
      : 'powershell.exe';

    logWake(`Starting local Windows wake listener process via: ${psPath}`);

    try {
      this.process = spawn(psPath, [
        '-NoProfile',
        '-NonInteractive',
        '-EncodedCommand', ENCODED_CMD
      ], {
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe']
      });

      this.process.stdout.on('data', (data) => {
        const text = data.toString();
        const lines = text.split(/\r?\n/).filter(l => l.trim().length > 0);

        for (const line of lines) {
          logWake(`stdout: ${line}`);
          if (line.includes('WAKE_LISTENER_ACTIVE')) {
            logWake('Windows native wake engine active and listening for "Hey AIVA" / "Jarvis"');
          } else if (line.startsWith('WAKE_DETECTED|')) {
            const parts = line.split('|');
            const recognized = parts[1] || 'Hey AIVA';
            const confidence = parts[2] || '1.0';

            const now = Date.now();
            if (now - this.lastTriggerTime > 1500) { // 1.5s debounce
              this.lastTriggerTime = now;
              logWake(`Local wake word detected: "${recognized}" (confidence: ${confidence})`);
              if (this.onWakeWord) {
                this.onWakeWord(recognized, confidence);
              }
            }
          } else if (line.startsWith('WAKE_ERROR|')) {
            logWake(`Wake script error: ${line}`);
          }
        }
      });

      this.process.stderr.on('data', (err) => {
        const msg = err.toString().trim();
        if (msg && !msg.includes('CLIXML')) {
          logWake(`stderr: ${msg}`);
        }
      });

      this.process.on('close', (code) => {
        logWake(`Process closed with code ${code}`);
        this.process = null;
        if (!this.isStopping) {
          logWake('Restarting wake listener in 3s...');
          setTimeout(() => {
            if (!this.isStopping) this.start();
          }, 3000);
        }
      });

      this.process.on('error', (err) => {
        logWake(`Process spawn error: ${err.message}`);
        this.process = null;
      });

    } catch (err) {
      logWake(`Could not initialize wake process: ${err.message}`);
    }
  }

  stop() {
    this.isStopping = true;
    if (this.process) {
      try {
        this.process.kill();
      } catch {}
      this.process = null;
      logWake('Local wake word engine stopped.');
    }
  }
}

module.exports = LocalWakeWordEngine;
