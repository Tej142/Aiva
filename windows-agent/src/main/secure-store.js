const fs = require('fs');
const path = require('path');
const { app, safeStorage } = require('electron');

const DEFAULT_SERVER_URL = process.env.AIVA_SERVER_URL || (app.isPackaged ? 'https://aiva-hub.duckdns.org' : 'http://localhost:5000');

class SecureStore {
  constructor() {
    this.configPath = path.join(app.getPath('userData'), 'agent-config.json');
    this.data = this._load();
  }

  _load() {
    try {
      if (fs.existsSync(this.configPath)) {
        const raw = fs.readFileSync(this.configPath, 'utf8');
        const parsed = JSON.parse(raw);
        // Ensure production builds enforce HTTPS if packaged
        if (app.isPackaged && parsed.serverUrl && !parsed.serverUrl.startsWith('https://')) {
          parsed.serverUrl = DEFAULT_SERVER_URL;
        }
        return parsed;
      }
    } catch (e) {
      console.error('[SecureStore] Failed to read config:', e);
    }
    return {
      serverUrl: DEFAULT_SERVER_URL,
      userEmail: '',
      userName: '',
      deviceId: '',
      deviceName: '',
      encryptedAccessToken: null,
      encryptedRefreshToken: null,
      encryptedDeviceToken: null,
      startWithWindows: false,
      runInBackground: true
    };
  }

  _save() {
    try {
      fs.writeFileSync(this.configPath, JSON.stringify(this.data, null, 2), 'utf8');
    } catch (e) {
      console.error('[SecureStore] Failed to write config:', e);
    }
  }

  set(key, value) {
    if (key === 'serverUrl') {
      if (app.isPackaged && value && !value.startsWith('https://')) {
        console.warn('[SecureStore] Insecure HTTP URL rejected in production build:', value);
        return;
      }
    }
    this.data[key] = value;
    this._save();
  }

  get(key, defaultValue = null) {
    return this.data[key] !== undefined ? this.data[key] : defaultValue;
  }

  setEncrypted(key, plainText) {
    if (!plainText) {
      this.data[key] = null;
      this._save();
      return;
    }

    if (safeStorage.isEncryptionAvailable()) {
      const encryptedBuffer = safeStorage.encryptString(plainText);
      this.data[key] = encryptedBuffer.toString('base64');
    } else {
      // Fallback base64 obfuscation if safeStorage unavailable in dev
      this.data[key] = Buffer.from(plainText, 'utf8').toString('base64');
    }
    this._save();
  }

  getDecrypted(key) {
    const raw = this.data[key];
    if (!raw) return null;

    try {
      const buffer = Buffer.from(raw, 'base64');
      if (safeStorage.isEncryptionAvailable()) {
        return safeStorage.decryptString(buffer);
      } else {
        return buffer.toString('utf8');
      }
    } catch (e) {
      console.error(`[SecureStore] Decryption failed for key ${key}:`, e);
      return null;
    }
  }

  clearAuth() {
    this.data.encryptedAccessToken = null;
    this.data.encryptedRefreshToken = null;
    this.data.encryptedDeviceToken = null;
    this.data.userEmail = '';
    this.data.deviceId = '';
    this.data.deviceName = '';
    this._save();
  }
}

module.exports = SecureStore;

