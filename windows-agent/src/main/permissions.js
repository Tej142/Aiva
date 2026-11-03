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
    // Sane assistant defaults: desktop productivity enabled, power lock guarded
    return {
      open_apps: true,
      open_apps_urls: true,
      media_volume: true,
      keyboard_input: true,
      files: true,
      file_access: true,
      screenshot: true,
      system_power: false,
      power_control: false
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
    const bVal = Boolean(value);
    this.permissions[key] = bVal;
    if (key === 'open_apps') this.permissions.open_apps_urls = bVal;
    if (key === 'open_apps_urls') this.permissions.open_apps = bVal;
    if (key === 'files') this.permissions.file_access = bVal;
    if (key === 'file_access') this.permissions.files = bVal;
    if (key === 'system_power') this.permissions.power_control = bVal;
    if (key === 'power_control') this.permissions.system_power = bVal;
    this._save();
  }

  setAll(newPerms) {
    for (const key of Object.keys(newPerms)) {
      this.permissions[key] = Boolean(newPerms[key]);
    }
    if (newPerms.open_apps !== undefined) this.permissions.open_apps_urls = Boolean(newPerms.open_apps);
    if (newPerms.open_apps_urls !== undefined) this.permissions.open_apps = Boolean(newPerms.open_apps_urls);
    if (newPerms.file_access !== undefined) this.permissions.files = Boolean(newPerms.file_access);
    if (newPerms.files !== undefined) this.permissions.file_access = Boolean(newPerms.files);
    if (newPerms.power_control !== undefined) this.permissions.system_power = Boolean(newPerms.power_control);
    if (newPerms.system_power !== undefined) this.permissions.power_control = Boolean(newPerms.system_power);
    this._save();
  }

  isPermitted(action) {
    switch (action) {
      case 'open_url':
      case 'open_app':
      case 'list_running_apps':
      case 'close_app':
        return Boolean(this.permissions.open_apps ?? this.permissions.open_apps_urls ?? true);

      case 'media_play_pause':
      case 'media_next':
      case 'media_prev':
      case 'volume_set':
      case 'volume_mute':
        return Boolean(this.permissions.media_volume ?? true);

      case 'type_text':
      case 'press_key':
      case 'hotkey':
        return Boolean(this.permissions.keyboard_input);

      case 'screenshot':
        return Boolean(this.permissions.screenshot);

      case 'lock_screen':
      case 'system_power':
        return Boolean(this.permissions.system_power ?? this.permissions.power_control);

      case 'open_file':
      case 'open_folder':
        return Boolean(this.permissions.files ?? this.permissions.file_access);

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

