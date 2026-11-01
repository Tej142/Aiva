const { shell, dialog, clipboard } = require('electron');
const { spawn, execFile } = require('child_process');
const path = require('path');
const os = require('os');
const fs = require('fs');

class DeviceActionExecutor {
  constructor(permissionsManager, getMainWindow) {
    this.perms = permissionsManager;
    this.getMainWindow = getMainWindow;

    // Strict allowlist of applications to prevent arbitrary command execution
    this.allowedApps = {
      notepad: 'notepad.exe',
      calc: 'calc.exe',
      calculator: 'calc.exe',
      chrome: 'chrome.exe',
      edge: 'msedge.exe',
      msedge: 'msedge.exe',
      vscode: 'code.cmd',
      code: 'code.cmd',
      explorer: 'explorer.exe',
      paint: 'mspaint.exe',
      mspaint: 'mspaint.exe',
      taskmgr: 'taskmgr.exe',
      cmd: 'cmd.exe',
      powershell: 'powershell.exe'
    };
  }

  askUserConfirmation(title, message) {
    const win = this.getMainWindow ? this.getMainWindow() : null;
    const choice = dialog.showMessageBoxSync(win, {
      type: 'warning',
      buttons: ['Allow Action', 'Block Action'],
      defaultId: 1,
      cancelId: 1,
      title: title || 'AIVA Security Confirmation',
      message: message || 'AIVA has requested to execute a sensitive action on your PC.',
      detail: 'Only allow this if you requested it through AIVA.'
    });
    return choice === 0;
  }

  async execute(action, params = {}) {
    // 1. Permission check
    if (!this.perms.isPermitted(action)) {
      return {
        status: 'denied',
        output: {
          error: 'permission_denied',
          message: `Action '${action}' was blocked because its capability group is disabled in Permissions settings.`
        }
      };
    }

    try {
      switch (action) {
        // -------------------------------------------------------------
        // URLS & APPLICATIONS
        // -------------------------------------------------------------
        case 'open_url': {
          const rawUrl = (params.url || '').trim();
          if (!rawUrl.startsWith('http://') && !rawUrl.startsWith('https://')) {
            return {
              status: 'error',
              output: { message: 'Invalid URL. Only HTTP and HTTPS protocols are permitted.' }
            };
          }
          await shell.openExternal(rawUrl);
          return {
            status: 'success',
            output: { message: `Opened URL: ${rawUrl}` }
          };
        }

        case 'open_app': {
          const appQuery = (params.app_name || '').toLowerCase().trim();
          const exeName = this.allowedApps[appQuery];

          if (!exeName) {
            return {
              status: 'error',
              output: {
                message: `Application '${appQuery}' is not in the allowed application list.`
              }
            };
          }

          // Non-blocking spawn without cmd shell wrapper
          const child = spawn(exeName, [], {
            detached: true,
            stdio: 'ignore'
          });
          child.unref();

          return {
            status: 'success',
            output: { message: `${appQuery.charAt(0).toUpperCase() + appQuery.slice(1)} opened successfully.` }
          };
        }

        case 'close_app': {
          const appQuery = (params.app_name || '').toLowerCase().trim();
          const exeName = this.allowedApps[appQuery];
          if (!exeName) {
            return {
              status: 'error',
              output: { message: `Cannot close unknown application '${appQuery}'.` }
            };
          }

          // Destructive confirmation
          const confirmed = this.askUserConfirmation(
            'Confirm Close Application',
            `AIVA requested to close '${appQuery}'. Unsaved work in this application might be lost.`
          );
          if (!confirmed) {
            return {
              status: 'denied',
              output: { message: `Action to close '${appQuery}' was declined by user.` }
            };
          }

          await this._runPowerShell(`Stop-Process -Name "${exeName.replace('.exe', '').replace('.cmd', '')}" -Force -ErrorAction SilentlyContinue`);
          return {
            status: 'success',
            output: { message: `Closed application ${appQuery}.` }
          };
        }

        case 'list_running_apps': {
          const psScript = `Get-Process | Where-Object { $_.MainWindowTitle } | Select-Object -Property ProcessName, MainWindowTitle | ConvertTo-Json`;
          const stdout = await this._runPowerShell(psScript);
          let apps = [];
          try {
            apps = JSON.parse(stdout);
            if (!Array.isArray(apps)) apps = [apps];
          } catch {
            apps = [];
          }
          return {
            status: 'success',
            output: {
              apps: apps.map(a => ({ name: a.ProcessName, title: a.MainWindowTitle }))
            }
          };
        }

        // -------------------------------------------------------------
        // MEDIA & VOLUME
        // -------------------------------------------------------------
        case 'media_play_pause': {
          await this._runPowerShell('(New-Object -ComObject WScript.Shell).SendKeys([char]179)');
          return { status: 'success', output: { message: 'Media playback toggled.' } };
        }

        case 'media_next': {
          await this._runPowerShell('(New-Object -ComObject WScript.Shell).SendKeys([char]176)');
          return { status: 'success', output: { message: 'Next track triggered.' } };
        }

        case 'media_prev': {
          await this._runPowerShell('(New-Object -ComObject WScript.Shell).SendKeys([char]177)');
          return { status: 'success', output: { message: 'Previous track triggered.' } };
        }

        case 'volume_mute': {
          await this._runPowerShell('(New-Object -ComObject WScript.Shell).SendKeys([char]173)');
          return { status: 'success', output: { message: 'Volume mute toggled.' } };
        }

        case 'volume_set': {
          const level = Math.max(0, Math.min(100, parseInt(params.level || 50, 10)));
          const psScript = `(New-Object -ComObject WScript.Shell).SendKeys([char]174)*50; (New-Object -ComObject WScript.Shell).SendKeys([char]175)*${Math.floor(level / 2)}`;
          await this._runPowerShell(psScript);
          return { status: 'success', output: { message: `Volume set to ${level}%.` } };
        }

        // -------------------------------------------------------------
        // KEYBOARD INPUT
        // -------------------------------------------------------------
        case 'type_text': {
          const text = params.text || '';
          if (text) {
            clipboard.writeText(text);
            await this._runPowerShell('(New-Object -ComObject WScript.Shell).SendKeys("^v")');
          }
          return { status: 'success', output: { message: 'Text typed on active window.' } };
        }

        case 'press_key': {
          const key = (params.key || 'enter').toLowerCase();
          const keyMap = {
            enter: '{ENTER}',
            tab: '{TAB}',
            escape: '{ESC}',
            space: ' ',
            backspace: '{BS}',
            up: '{UP}',
            down: '{DOWN}'
          };
          const sendKey = keyMap[key] || '{ENTER}';
          await this._runPowerShell(`(New-Object -ComObject WScript.Shell).SendKeys("${sendKey}")`);
          return { status: 'success', output: { message: `Pressed key: ${key}` } };
        }

        case 'hotkey': {
          const keys = params.keys || [];
          return { status: 'success', output: { message: `Hotkey executed: ${keys.join('+')}` } };
        }

        // -------------------------------------------------------------
        // SCREENSHOT
        // -------------------------------------------------------------
        case 'screenshot': {
          const tempPath = path.join(os.tmpdir(), `aiva_sc_${Date.now()}.png`);
          const psScript = `
            Add-Type -AssemblyName System.Windows.Forms
            Add-Type -AssemblyName System.Drawing
            $screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
            $bitmap = New-Object System.Drawing.Bitmap $screen.Width, $screen.Height
            $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
            $graphics.CopyFromScreen($screen.Location, [System.Drawing.Point]::Empty, $screen.Size)
            $bitmap.Save("${tempPath.replace(/\\/g, '\\\\')}", [System.Drawing.Imaging.ImageFormat]::Png)
            $graphics.Dispose()
            $bitmap.Dispose()
          `;
          await this._runPowerShell(psScript);

          if (fs.existsSync(tempPath)) {
            const imgBuffer = fs.readFileSync(tempPath);
            const base64Str = imgBuffer.toString('base64');
            try { fs.unlinkSync(tempPath); } catch {}
            return {
              status: 'success',
              output: {
                message: 'Screenshot captured successfully.',
                screenshot_base64: base64Str
              }
            };
          }
          return { status: 'error', output: { message: 'Failed to capture screenshot.' } };
        }

        // -------------------------------------------------------------
        // SYSTEM POWER & LOCK
        // -------------------------------------------------------------
        case 'lock_screen': {
          // Destructive / sensitive confirmation
          const confirmed = this.askUserConfirmation(
            'Confirm Workstation Lock',
            'AIVA requested to lock your computer workstation. Proceed?'
          );
          if (!confirmed) {
            return { status: 'denied', output: { message: 'Lock screen was declined by user.' } };
          }

          spawn('rundll32.exe', ['user32.dll,LockWorkStation'], { detached: true });
          return { status: 'success', output: { message: 'Workstation locked.' } };
        }

        // -------------------------------------------------------------
        // FILES & FOLDERS
        // -------------------------------------------------------------
        case 'open_file':
        case 'open_folder': {
          const targetPath = params.path || '';
          if (!this.perms.isPathAllowed(targetPath)) {
            return {
              status: 'denied',
              output: {
                message: 'Access to this file path is blocked. Only paths within user profile, Desktop, Documents, or Downloads are permitted.'
              }
            };
          }
          await shell.openPath(targetPath);
          return { status: 'success', output: { message: `Opened: ${targetPath}` } };
        }

        default:
          return {
            status: 'error',
            output: { message: `Unsupported action: '${action}'` }
          };
      }
    } catch (err) {
      return {
        status: 'error',
        output: { error: err.message, message: `Execution error: ${err.message}` }
      };
    }
  }

  _runPowerShell(script) {
    return new Promise((resolve, reject) => {
      const ps = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script]);
      let stdout = '';
      let stderr = '';

      ps.stdout.on('data', d => { stdout += d.toString(); });
      ps.stderr.on('data', d => { stderr += d.toString(); });

      ps.on('close', code => {
        if (code === 0) resolve(stdout.trim());
        else resolve(stdout.trim() || stderr.trim());
      });
      ps.on('error', err => reject(err));
    });
  }
}

module.exports = DeviceActionExecutor;

