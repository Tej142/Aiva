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
const perms = new PermissionsManager();
let executor = null;
let wsClient = null;

let currentDeviceStatus = { state: 'disconnected', message: 'Not connected.' };
const activityLogs = [];

function createTrayIcon() {
  // Generate a clean 16x16 tray icon programmatically (Cyan dot for AIVA)
  const size = 16;
  const canvas = Buffer.alloc(size * size * 4);
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const idx = (y * size + x) * 4;
      const dx = x - 8;
      const dy = y - 8;
      if (dx * dx + dy * dy <= 36) {
        canvas[idx] = 56;     // B
        canvas[idx + 1] = 189; // G
        canvas[idx + 2] = 248; // R
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

  const contextMenu = Menu.buildFromTemplate([
    { label: 'AIVA Device Agent', enabled: false },
    { label: statusText, enabled: false },
    { type: 'separator' },
    {
      label: 'Open AIVA HUD',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
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
      label: 'Quit AIVA Agent',
      click: () => {
        isQuitting = true;
        app.quit();
      }
    }
  ]);

  tray.setContextMenu(contextMenu);
  tray.setToolTip(`AIVA Device Agent (${currentDeviceStatus.state})`);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 900,
    minHeight: 600,
    title: 'AIVA Device Agent',
    backgroundColor: '#0a0e17',
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
  // If user has a valid device token stored, open main UI with embedded HUD;
  // Otherwise open login screen.
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
  // Auth: Request OTP
  ipcMain.handle('auth:requestOtp', async (_event, email) => {
    const serverUrl = store.get('serverUrl', 'http://localhost:5000');
    try {
      const resp = await fetch(`${serverUrl}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email })
      });
      const data = await resp.json();
      return data;
    } catch (err) {
      return { status: 'error', message: `Server connection failed: ${err.message}` };
    }
  });

  // Auth: Verify OTP and Register Device
  ipcMain.handle('auth:verifyOtp', async (_event, { email, otp }) => {
    const serverUrl = store.get('serverUrl', 'http://localhost:5000');
    try {
      // 1. Verify OTP
      const verifyResp = await fetch(`${serverUrl}/api/auth/verify-otp`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, otp })
      });
      const verifyData = await verifyResp.json();
      if (verifyData.status !== 'success') {
        return verifyData;
      }

      const accessToken = verifyData.tokens.access_token;
      const refreshToken = verifyData.tokens.refresh_token;
      const userId = verifyData.user.id;

      store.set('userEmail', email);
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

  // Auth: Get Me
  ipcMain.handle('auth:getMe', async () => {
    const serverUrl = store.get('serverUrl', 'http://localhost:5000');
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

  // Auth: Logout
  ipcMain.handle('auth:logout', async () => {
    const serverUrl = store.get('serverUrl', 'http://localhost:5000');
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
    if (mainWindow) {
      mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'login.html'));
    }
    return { status: 'success' };
  });

  // Config: Get / Save
  ipcMain.handle('config:get', () => {
    return {
      serverUrl: store.get('serverUrl', 'http://localhost:5000'),
      userEmail: store.get('userEmail', ''),
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

  // If already registered, initiate WebSocket connection
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

