const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('aivaAgent', {
  // Authentication
  requestOtp: (email) => ipcRenderer.invoke('auth:requestOtp', email),
  verifyOtp: (email, otp) => ipcRenderer.invoke('auth:verifyOtp', { email, otp }),
  getMe: () => ipcRenderer.invoke('auth:getMe'),
  logout: () => ipcRenderer.invoke('auth:logout'),

  // Config & Status
  getConfig: () => ipcRenderer.invoke('config:get'),
  saveConfig: (data) => ipcRenderer.invoke('config:save', data),
  getDeviceStatus: () => ipcRenderer.invoke('device:getStatus'),
  reconnectDevice: () => ipcRenderer.invoke('device:reconnect'),

  // Permissions
  getPermissions: () => ipcRenderer.invoke('permissions:get'),
  savePermissions: (perms) => ipcRenderer.invoke('permissions:save', perms),

  // Navigation
  switchView: (viewName) => ipcRenderer.invoke('view:switch', viewName),

  // Event Listeners
  onStatusChange: (callback) => {
    ipcRenderer.on('device:status', (_event, data) => callback(data));
  },
  onActivityLog: (callback) => {
    ipcRenderer.on('device:activity', (_event, data) => callback(data));
  }
});

