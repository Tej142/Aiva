const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('aivaAgent', {
  // Window Controls
  windowControl: (action) => ipcRenderer.invoke('window:control', action),

  // Authentication
  register: ({ name, email }) => ipcRenderer.invoke('auth:register', { name, email }),
  requestOtp: (email) => ipcRenderer.invoke('auth:requestOtp', email),
  verifyOtp: ({ email, otp, name }) => ipcRenderer.invoke('auth:verifyOtp', { email, otp, name }),
  completeOnboarding: (name) => ipcRenderer.invoke('auth:onboarding', name),
  getMe: () => ipcRenderer.invoke('auth:getMe'),
  logout: () => ipcRenderer.invoke('auth:logout'),
  getTokens: () => ipcRenderer.invoke('auth:getTokens'),

  // Config & Status
  getConfig: () => ipcRenderer.invoke('config:get'),
  saveConfig: (data) => ipcRenderer.invoke('config:save', data),
  getDeviceStatus: () => ipcRenderer.invoke('device:getStatus'),
  reconnectDevice: () => ipcRenderer.invoke('device:reconnect'),

  // Data & History
  getHistory: () => ipcRenderer.invoke('data:getHistory'),
  clearHistory: () => ipcRenderer.invoke('data:clearHistory'),
  getSettings: () => ipcRenderer.invoke('data:getSettings'),
  saveSettings: (settings) => ipcRenderer.invoke('data:saveSettings', settings),
  getAuditLogs: (limit) => ipcRenderer.invoke('data:getAuditLogs', limit),

  // Permissions
  getPermissions: () => ipcRenderer.invoke('permissions:get'),
  savePermissions: (perms) => ipcRenderer.invoke('permissions:save', perms),

  // Navigation
  switchView: (viewName) => ipcRenderer.invoke('view:switch', viewName),

  // Event Listeners
  onStatusChange: (callback) => {
    ipcRenderer.removeAllListeners('device:status');
    ipcRenderer.on('device:status', (_event, data) => callback(data));
  },
  onActivityLog: (callback) => {
    ipcRenderer.removeAllListeners('device:activity');
    ipcRenderer.on('device:activity', (_event, data) => callback(data));
  }
});
