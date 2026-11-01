const fs = require('fs');
const path = require('path');
const os = require('os');
const { app } = require('electron');

class PermissionsManager {
  constructor() {
    this.permsPath = path.join(app.getPath('userData'), 'permissions.json');
    this.permissions = this._load();
    this.allowedRoots = [
      path.resolve(os.homedir()),
      path.resolve(app.getPath('desktop')),
      path.resolve(app.getPath('documents')),
      path.resolve(app.getPath('downloads'))
    ];
  }

  _load() {
    try {
      if (fs.existsSync(this.permsPath)) {
        const raw = fs.readFileSync(this.permsPath, 'utf8');
        return JSON.parse(raw);
      }
    } catch (e) {
      console.error('[Permissions] Failed to read permissions config:', e);
    }
    // SECURE DEFAULTS: Dangerous capabilities OFF by default
    return {
      open_apps_urls: true,
      media_volume: true,
      keyboard_input: false,
      files: false,
      screenshot: false,
      system_power: false
    };
  }

  _save() {
    try {
      fs.writeFileSync(this.permsPath, JSON.stringify(this.permissions, null, 2), 'utf8');
    } catch (e) {
      console.error('[Permissions] Failed to save permissions config:', e);
    }
  }

  getAll() {
    return { ...this.permissions };
  }

  set(key, value) {
    if (this.permissions.hasOwnProperty(key)) {
      this.permissions[key] = Boolean(value);
      this._save();
    }
  }

  setAll(newPerms) {
    for (const key of Object.keys(this.permissions)) {
      if (newPerms.hasOwnProperty(key)) {
        this.permissions[key] = Boolean(newPerms[key]);
      }
    }
    this._save();
  }

  isPermitted(action) {
    switch (action) {
      case 'open_url':
      case 'open_app':
      case 'list_running_apps':
      case 'close_app':
        return this.permissions.open_apps_urls;

      case 'media_play_pause':
      case 'media_next':
      case 'media_prev':
      case 'volume_set':
      case 'volume_mute':
        return this.permissions.media_volume;

      case 'type_text':
      case 'press_key':
      case 'hotkey':
        return this.permissions.keyboard_input;

      case 'screenshot':
        return this.permissions.screenshot;

      case 'lock_screen':
      case 'system_power':
        return this.permissions.system_power;

      case 'open_file':
      case 'open_folder':
        return this.permissions.files;

      default:
        return false;
    }
  }

  isPathAllowed(filePath) {
    if (!filePath || typeof filePath !== 'string') return false;
    try {
      const normalized = path.resolve(filePath);
      return this.allowedRoots.some(root => normalized.startsWith(root));
    } catch {
      return false;
    }
  }
}

module.exports = PermissionsManager;

