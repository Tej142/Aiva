const { app, BrowserWindow, Tray, Menu, ipcMain, nativeImage, shell, session } = require('electron');
const path = require('path');
const os = require('os');

const SecureStore = require('./secure-store');
const PermissionsManager = require('./permissions');
const DeviceActionExecutor = require('./executor');
const DeviceWebSocketClient = require('./ws-client');

let mainWindow = null;
let tray = null;
let isQuitting = false;

const store = new SecureStore();

function getServerUrl() {
  const url = store.get('serverUrl');
  if (!url || url === 'http://localhost:5000' || url.includes('aiva-hub')) {
    return 'https://aivaserve.duckdns.org';
  }
  return url.replace(/\/+$/, '');
}
const perms = new PermissionsManager();
let executor = null;
let wsClient = null;

let currentDeviceStatus = { state: 'disconnected', message: 'Not connected.' };
const activityLogs = [];

function createTrayIcon() {
  const iconPath = path.join(__dirname, '..', '..', 'build', 'icon.png');
  const fs = require('fs');
  if (fs.existsSync(iconPath)) {
    return nativeImage.createFromPath(iconPath).resize({ width: 16, height: 16 });
  }
  const size = 16;
  const canvas = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const idx = (y * size + x) * 4;
      const dx = x - 8;
      const dy = y - 8;
      if (dx * dx + dy * dy <= 36) {
        canvas[idx] = 241;     // B
        canvas[idx + 1] = 102; // G
        canvas[idx + 2] = 99;  // R
        canvas[idx + 3] = 255; // A
      } else {
        canvas[idx + 3] = 0;   // Transparent
      }
    }
  }
  return nativeImage.createFromBuffer(canvas, { width: size, height: size });
}

function updateTrayMenu() {
  if (!tray) return;

  const statusText = currentDeviceStatus.state === 'connected'
    ? '🟢 Connected to AIVA'
    : currentDeviceStatus.state === 'connecting'
    ? '🟡 Connecting...'
    : '⚪ Disconnected';

  const isPaused = Boolean(executor && executor.isPaused);

  const contextMenu = Menu.buildFromTemplate([
    { label: 'AIVA Device Agent', enabled: false },
    { label: statusText, enabled: false },
    { type: 'separator' },
    {
      label: 'Open AIVA',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        }
      }
    },
    {
      label: isPaused ? '▶️ Resume Control' : '⏸️ Pause Control',
      type: 'checkbox',
      checked: isPaused,
      click: (item) => {
        if (executor) {
          executor.isPaused = item.checked;
          updateTrayMenu();
        }
      }
    },
    {
      label: 'Permissions & Settings',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
          mainWindow.webContents.send('view:switch', 'settings');
        }
      }
    },
    {
      label: 'Reconnect WebSocket',
      click: () => {
        if (wsClient) wsClient.connect();
      }
    },
    { type: 'separator' },
    {
      label: 'Quit',
      click: () => {
        isQuitting = true;
        app.quit();
      }
    }
  ]);

  tray.setContextMenu(contextMenu);
  tray.setToolTip(`AIVA Device Agent (${currentDeviceStatus.state}${isPaused ? ' - Paused' : ''})`);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    title: 'AIVA Device Agent',
    backgroundColor: '#0B0D12',
    frame: false,
    titleBarStyle: 'hidden',
    icon: createTrayIcon(),
    webPreferences: {
      preload: path.join(__dirname, '..', 'preload', 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  // Tray setup
  if (!tray) {
    tray = new Tray(createTrayIcon());
    tray.on('double-click', () => {
      if (mainWindow.isVisible()) mainWindow.hide();
      else {
        mainWindow.show();
        mainWindow.focus();
      }
    });
    updateTrayMenu();
  }

  // Handle window close -> minimize to tray if runInBackground is enabled
  mainWindow.on('close', (event) => {
    if (!isQuitting && store.get('runInBackground', true)) {
      event.preventDefault();
      mainWindow.hide();
      if (tray) {
        tray.displayBalloon({
          title: 'AIVA Device Agent',
          content: 'AIVA is still running in your system tray to execute device commands.'
        });
      }
    }
  });

  // Decide initial screen:
  const deviceToken = store.getDecrypted('encryptedDeviceToken');
  if (deviceToken) {
    mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'main.html'));
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'login.html'));
  }
}

// ---------------------------------------------------------------------
// IPC HANDLERS
// ---------------------------------------------------------------------
function setupIpcHandlers() {
  // Window Controls (Frameless Title Bar)
  ipcMain.handle('window:control', (_event, action) => {
    if (!mainWindow) return;
    if (action === 'minimize') mainWindow.minimize();
    else if (action === 'maximize') {
      if (mainWindow.isMaximized()) mainWindow.unmaximize();
      else mainWindow.maximize();
    } else if (action === 'close') {
      mainWindow.close();
    }
  });

  // Auth: Register (name + email)
  ipcMain.handle('auth:register', async (_event, { name, email }) => {
    const serverUrl = getServerUrl();
    try {
      const resp = await fetch(`${serverUrl}/api/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, email })
      });
      return await resp.json();
    } catch (err) {
      return { status: 'error', message: `Server connection failed (${serverUrl}): ${err.message}` };
    }
  });

  // Auth: Request OTP (login only email)
  ipcMain.handle('auth:requestOtp', async (_event, email) => {
    const serverUrl = getServerUrl();
    try {
      const resp = await fetch(`${serverUrl}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email })
      });
      return await resp.json();
    } catch (err) {
      return { status: 'error', message: `Server connection failed (${serverUrl}): ${err.message}` };
    }
  });

  // Auth: Verify OTP and Register Device
  ipcMain.handle('auth:verifyOtp', async (_event, { email, otp, name }) => {
    const serverUrl = getServerUrl();
    try {
      // 1. Verify OTP
      const verifyResp = await fetch(`${serverUrl}/api/auth/verify-otp`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, otp, name })
      });
      const verifyData = await verifyResp.json();
      if (verifyData.status !== 'success') {
        return verifyData;
      }

      const accessToken = verifyData.tokens.access_token;
      const refreshToken = verifyData.tokens.refresh_token;

      store.set('userEmail', email);
      if (verifyData.user && verifyData.user.name) {
        store.set('userName', verifyData.user.name);
      }
      store.setEncrypted('encryptedAccessToken', accessToken);
      store.setEncrypted('encryptedRefreshToken', refreshToken);

      // 2. Register Device on Cloud
      const computerName = os.hostname() || 'Windows PC';
      const devResp = await fetch(`${serverUrl}/api/devices/register`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${accessToken}`
        },
        body: JSON.stringify({
          name: computerName,
          platform: 'windows',
          app_version: app.getVersion() || '1.0.0'
        })
      });
      const devData = await devResp.json();
      if (devData.status !== 'success') {
        return devData;
      }

      store.set('deviceId', devData.device.id);
      store.set('deviceName', devData.device.name);
      store.setEncrypted('encryptedDeviceToken', devData.device_token);

      // 3. Connect WebSocket immediately
      if (wsClient) wsClient.connect();

      return {
        status: 'success',
        user: verifyData.user,
        device: devData.device
      };

    } catch (err) {
      return { status: 'error', message: `Authentication error: ${err.message}` };
    }
  });

  // Auth: Onboarding
  ipcMain.handle('auth:onboarding', async (_event, name) => {
    const serverUrl = getServerUrl();
    const accessToken = store.getDecrypted('encryptedAccessToken');
    if (!accessToken) return { status: 'error', message: 'Not authenticated' };
    try {
      const resp = await fetch(`${serverUrl}/api/auth/onboarding`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${accessToken}`
        },
        body: JSON.stringify({ name })
      });
      if (name) store.set('userName', name);
      return await resp.json();
    } catch (err) {
      return { status: 'error', message: err.message };
    }
  });

  // Auth: Get Me
  ipcMain.handle('auth:getMe', async () => {
    const serverUrl = getServerUrl();
    const accessToken = store.getDecrypted('encryptedAccessToken');
    if (!accessToken) return null;

    try {
      const resp = await fetch(`${serverUrl}/api/auth/me`, {
        headers: { 'Authorization': `Bearer ${accessToken}` }
      });
      if (resp.status === 200) {
        return await resp.json();
      }
    } catch {}
    return null;
  });

  // Auth: Logout (Clears auth tokens and session storage completely)
  ipcMain.handle('auth:logout', async () => {
    const serverUrl = getServerUrl();
    const refreshToken = store.getDecrypted('encryptedRefreshToken');
    if (refreshToken) {
      try {
        await fetch(`${serverUrl}/api/auth/logout`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: refreshToken })
        });
      } catch {}
    }

    if (wsClient) wsClient.disconnect();
    store.clearAuth();

    try {
      await session.defaultSession.clearStorageData();
    } catch {}

    if (mainWindow) {
      mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'login.html'));
    }
    return { status: 'success' };
  });

  // History & User Data
  ipcMain.handle('data:getHistory', async () => {
    const serverUrl = getServerUrl();
    const accessToken = store.getDecrypted('encryptedAccessToken');
    if (!accessToken) return [];
    try {
      const resp = await fetch(`${serverUrl}/api/history`, {
        headers: { 'Authorization': `Bearer ${accessToken}` }
      });
      const data = await resp.json();
      return data.history || [];
    } catch {
      return [];
    }
  });

  ipcMain.handle('data:clearHistory', async () => {
    const serverUrl = getServerUrl();
    const accessToken = store.getDecrypted('encryptedAccessToken');
    if (!accessToken) return false;
    try {
      const resp = await fetch(`${serverUrl}/api/history`, {
        method: 'DELETE',
        headers: { 'Authorization': `Bearer ${accessToken}` }
      });
      return resp.ok;
    } catch {
      return false;
    }
  });

  ipcMain.handle('data:getSettings', async () => {
    const serverUrl = getServerUrl();
    const accessToken = store.getDecrypted('encryptedAccessToken');
    if (!accessToken) return {};
    try {
      const resp = await fetch(`${serverUrl}/api/settings`, {
        headers: { 'Authorization': `Bearer ${accessToken}` }
      });
      const data = await resp.json();
      return data.settings || {};
    } catch {
      return {};
    }
  });

  ipcMain.handle('data:saveSettings', async (_event, settings) => {
    const serverUrl = getServerUrl();
    const accessToken = store.getDecrypted('encryptedAccessToken');
    if (!accessToken) return false;
    try {
      const resp = await fetch(`${serverUrl}/api/settings`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${accessToken}`
        },
        body: JSON.stringify(settings)
      });
      return resp.ok;
    } catch {
      return false;
    }
  });

  ipcMain.handle('data:getAuditLogs', async (_event, limit) => {
    const serverUrl = getServerUrl();
    const accessToken = store.getDecrypted('encryptedAccessToken');
    if (!accessToken) return [];
    try {
      const resp = await fetch(`${serverUrl}/api/devices/audit-logs?limit=${limit || 50}`, {
        headers: { 'Authorization': `Bearer ${accessToken}` }
      });
      const data = await resp.json();
      return data.audit_logs || [];
    } catch {
      return [];
    }
  });

  // Config: Get / Save
  ipcMain.handle('config:get', () => {
    return {
      serverUrl: getServerUrl(),
      userEmail: store.get('userEmail', ''),
      userName: store.get('userName', ''),
      deviceId: store.get('deviceId', ''),
      deviceName: store.get('deviceName', ''),
      startWithWindows: store.get('startWithWindows', false),
      runInBackground: store.get('runInBackground', true)
    };
  });

  ipcMain.handle('config:save', (_event, data) => {
    if (data.serverUrl) store.set('serverUrl', data.serverUrl.trim());
    if (data.runInBackground !== undefined) store.set('runInBackground', Boolean(data.runInBackground));
    if (data.startWithWindows !== undefined) {
      const enabled = Boolean(data.startWithWindows);
      store.set('startWithWindows', enabled);
      app.setLoginItemSettings({
        openAtLogin: enabled,
        path: process.execPath
      });
    }
    return { status: 'success' };
  });

  // Permissions: Get / Save
  ipcMain.handle('permissions:get', () => perms.getAll());
  ipcMain.handle('permissions:save', (_event, newPerms) => {
    perms.setAll(newPerms);
    return { status: 'success', permissions: perms.getAll() };
  });

  // Status & Reconnect
  ipcMain.handle('device:getStatus', () => ({
    ...currentDeviceStatus,
    activityLogs: activityLogs.slice(-50)
  }));

  ipcMain.handle('device:reconnect', () => {
    if (wsClient) wsClient.connect();
    return { status: 'success' };
  });

  // Navigation
  ipcMain.handle('view:switch', (_event, viewName) => {
    if (!mainWindow) return;
    if (viewName === 'hud') {
      mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'main.html'));
    } else if (viewName === 'settings') {
      mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'settings.html'));
    } else if (viewName === 'login') {
      mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'login.html'));
    }
  });
}

// ---------------------------------------------------------------------
// APP LIFECYCLE
// ---------------------------------------------------------------------
app.whenReady().then(() => {
  executor = new DeviceActionExecutor(perms, () => mainWindow);

  wsClient = new DeviceWebSocketClient(
    store,
    executor,
    (state, message) => {
      currentDeviceStatus = { state, message, time: new Date().toLocaleTimeString() };
      updateTrayMenu();
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('device:status', currentDeviceStatus);
      }
    },
    (logItem) => {
      activityLogs.push(logItem);
      if (activityLogs.length > 200) activityLogs.shift();
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('device:activity', logItem);
      }
    }
  );

  setupIpcHandlers();
  createWindow();

  const deviceToken = store.getDecrypted('encryptedDeviceToken');
  if (deviceToken) {
    wsClient.connect();
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
  } else if (mainWindow) {
    mainWindow.show();
    mainWindow.focus();
  }
});

app.on('before-quit', () => {
  isQuitting = true;
  if (wsClient) wsClient.disconnect();
});
