// =====================================================================
// AIVA DEVICE AGENT — RENDERER LOGIC
// =====================================================================

function showMessage(elId, text, isError = false) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = text;
  el.className = isError ? 'auth-message error' : 'auth-message info';
  el.style.display = 'block';
}

function clearMessage(elId) {
  const el = document.getElementById(elId);
  if (el) el.style.display = 'none';
}

// ---------------------------------------------------------------------
// 1. LOGIN VIEW CONTROLLER
// ---------------------------------------------------------------------
async function initLoginView() {
  const serverInput = document.getElementById('input-server-url');
  const emailInput = document.getElementById('input-email');
  const btnRequest = document.getElementById('btn-request-otp');
  const stepEmail = document.getElementById('step-email');
  const stepOtp = document.getElementById('step-otp');
  const displayEmail = document.getElementById('display-email');
  const btnVerify = document.getElementById('btn-verify-otp');
  const btnBack = document.getElementById('btn-back-email');
  const btnResend = document.getElementById('btn-resend-otp');

  // Load existing server URL if configured
  try {
    const cfg = await window.aivaAgent.getConfig();
    if (cfg && cfg.serverUrl) {
      serverInput.value = cfg.serverUrl;
    }
  } catch {}

  // Request OTP
  btnRequest.addEventListener('click', async () => {
    const email = emailInput.value.trim();
    const serverUrl = serverInput.value.trim();

    if (!email || !email.includes('@')) {
      showMessage('auth-msg', 'Please enter a valid email address.', true);
      return;
    }

    await window.aivaAgent.saveConfig({ serverUrl });
    btnRequest.disabled = true;
    btnRequest.textContent = 'Sending code...';
    clearMessage('auth-msg');

    const res = await window.aivaAgent.requestOtp(email);
    btnRequest.disabled = false;
    btnRequest.textContent = 'Send Verification Code';

    if (res.status === 'success') {
      displayEmail.textContent = email;
      stepEmail.style.display = 'none';
      stepOtp.style.display = 'block';
      clearMessage('auth-msg');
      document.getElementById('otp-1').focus();
    } else {
      showMessage('auth-msg', res.message || 'Failed to send verification code.', true);
    }
  });

  // OTP 6-box input auto-navigation
  const otpBoxes = [1, 2, 3, 4, 5, 6].map(i => document.getElementById(`otp-${i}`));
  otpBoxes.forEach((box, index) => {
    box.addEventListener('input', (e) => {
      const val = e.target.value;
      if (val.length === 1 && index < 5) {
        otpBoxes[index + 1].focus();
      }
      if (getEnteredOtp().length === 6) {
        btnVerify.click();
      }
    });

    box.addEventListener('keydown', (e) => {
      if (e.key === 'Backspace' && !box.value && index > 0) {
        otpBoxes[index - 1].focus();
      }
    });

    box.addEventListener('paste', (e) => {
      e.preventDefault();
      const pasteData = (e.clipboardData || window.clipboardData).getData('text').trim();
      if (/^\d{6}$/.test(pasteData)) {
        pasteData.split('').forEach((ch, idx) => {
          if (otpBoxes[idx]) otpBoxes[idx].value = ch;
        });
        otpBoxes[5].focus();
        btnVerify.click();
      }
    });
  });

  function getEnteredOtp() {
    return otpBoxes.map(b => b.value).join('');
  }

  // Verify OTP
  btnVerify.addEventListener('click', async () => {
    const email = emailInput.value.trim();
    const otp = getEnteredOtp();

    if (otp.length !== 6) {
      showMessage('auth-msg', 'Please enter all 6 digits.', true);
      return;
    }

    btnVerify.disabled = true;
    btnVerify.textContent = 'Verifying...';
    clearMessage('auth-msg');

    const res = await window.aivaAgent.verifyOtp(email, otp);
    btnVerify.disabled = false;
    btnVerify.textContent = 'Verify & Connect Device';

    if (res.status === 'success') {
      // Navigate to embedded HUD
      window.aivaAgent.switchView('hud');
    } else {
      showMessage('auth-msg', res.message || 'Verification failed.', true);
    }
  });

  btnBack.addEventListener('click', () => {
    stepOtp.style.display = 'none';
    stepEmail.style.display = 'block';
    clearMessage('auth-msg');
  });

  btnResend.addEventListener('click', async () => {
    const email = emailInput.value.trim();
    const res = await window.aivaAgent.requestOtp(email);
    if (res.status === 'success') {
      showMessage('auth-msg', 'A new verification code was sent to your email.');
    } else {
      showMessage('auth-msg', res.message || 'Failed to resend code.', true);
    }
  });
}

// ---------------------------------------------------------------------
// 2. MAIN VIEW CONTROLLER (EMBEDDED HUD)
// ---------------------------------------------------------------------
async function initMainView() {
  const iframe = document.getElementById('hud-iframe');
  const btnSettings = document.getElementById('btn-nav-settings');
  const btnReconnect = document.getElementById('btn-reconnect');
  const statusDot = document.getElementById('status-dot-indicator');
  const statusText = document.getElementById('nav-status-text');

  try {
    const cfg = await window.aivaAgent.getConfig();
    if (cfg && cfg.serverUrl) {
      iframe.src = cfg.serverUrl;
    }
  } catch {}

  btnSettings.addEventListener('click', () => {
    window.aivaAgent.switchView('settings');
  });

  btnReconnect.addEventListener('click', () => {
    window.aivaAgent.reconnectDevice();
  });

  // Listen to live WebSocket status changes
  window.aivaAgent.onStatusChange((status) => {
    if (statusText) statusText.textContent = status.state.toUpperCase();
    if (statusDot) {
      statusDot.className = `status-dot ${status.state}`;
    }
  });
}

// ---------------------------------------------------------------------
// 3. SETTINGS & PERMISSIONS CONTROLLER
// ---------------------------------------------------------------------
async function initSettingsView() {
  const btnBack = document.getElementById('btn-back-hud');
  const btnLogout = document.getElementById('btn-logout');
  const btnReconnect = document.getElementById('btn-reconnect-ws');
  const btnClearLogs = document.getElementById('btn-clear-logs');
  const btnSaveUrl = document.getElementById('btn-save-server-url');
  const inputServerUrl = document.getElementById('cfg-server-url');
  const inputDeviceName = document.getElementById('cfg-device-name');
  const inputUserEmail = document.getElementById('cfg-user-email');

  const toggleAutostart = document.getElementById('toggle-autostart');
  const toggleBackground = document.getElementById('toggle-background');

  const permToggles = {
    open_apps_urls: document.getElementById('perm-open-apps'),
    media_volume: document.getElementById('perm-media-volume'),
    keyboard_input: document.getElementById('perm-keyboard'),
    screenshot: document.getElementById('perm-screenshot'),
    files: document.getElementById('perm-files'),
    system_power: document.getElementById('perm-power')
  };

  const statusDot = document.getElementById('settings-status-dot');
  const statusText = document.getElementById('settings-status-text');
  const logTableBody = document.getElementById('activity-log-body');

  // Load Config
  const cfg = await window.aivaAgent.getConfig();
  if (cfg) {
    inputServerUrl.value = cfg.serverUrl || 'http://localhost:5000';
    inputDeviceName.value = cfg.deviceName || 'Windows PC';
    inputUserEmail.value = cfg.userEmail || '';
    toggleAutostart.checked = Boolean(cfg.startWithWindows);
    toggleBackground.checked = Boolean(cfg.runInBackground);
  }

  // Load Permissions
  const currentPerms = await window.aivaAgent.getPermissions();
  if (currentPerms) {
    for (const [key, el] of Object.entries(permToggles)) {
      if (el && currentPerms.hasOwnProperty(key)) {
        el.checked = Boolean(currentPerms[key]);
      }
    }
  }

  // Permission Change Listeners
  for (const [key, el] of Object.entries(permToggles)) {
    if (el) {
      el.addEventListener('change', async () => {
        const update = {};
        update[key] = el.checked;
        await window.aivaAgent.savePermissions(update);
      });
    }
  }

  // Desktop Behavior Toggles
  toggleAutostart.addEventListener('change', async () => {
    await window.aivaAgent.saveConfig({ startWithWindows: toggleAutostart.checked });
  });

  toggleBackground.addEventListener('change', async () => {
    await window.aivaAgent.saveConfig({ runInBackground: toggleBackground.checked });
  });

  btnSaveUrl.addEventListener('click', async () => {
    const newUrl = inputServerUrl.value.trim();
    if (newUrl) {
      await window.aivaAgent.saveConfig({ serverUrl: newUrl });
      window.aivaAgent.reconnectDevice();
      alert('Cloud server URL updated.');
    }
  });

  btnReconnect.addEventListener('click', () => {
    window.aivaAgent.reconnectDevice();
  });

  btnBack.addEventListener('click', () => {
    window.aivaAgent.switchView('hud');
  });

  btnLogout.addEventListener('click', async () => {
    if (confirm('Are you sure you want to log out and disconnect this device?')) {
      await window.aivaAgent.logout();
    }
  });

  btnClearLogs.addEventListener('click', () => {
    logTableBody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: #64748b; padding: 20px;">No commands received yet.</td></tr>';
  });

  // Status updates
  window.aivaAgent.onStatusChange((status) => {
    if (statusText) statusText.textContent = status.state.toUpperCase();
    if (statusDot) statusDot.className = `status-dot ${status.state}`;
  });

  // Fetch initial device status and logs
  const statusObj = await window.aivaAgent.getDeviceStatus();
  if (statusObj) {
    if (statusText) statusText.textContent = statusObj.state.toUpperCase();
    if (statusDot) statusDot.className = `status-dot ${statusObj.state}`;
    if (statusObj.activityLogs && statusObj.activityLogs.length > 0) {
      logTableBody.innerHTML = '';
      statusObj.activityLogs.forEach(renderLogRow);
    }
  }

  // Activity Log Live Updates
  window.aivaAgent.onActivityLog((item) => {
    renderLogRow(item);
  });

  function renderLogRow(item) {
    // If empty placeholder exists, clear it
    if (logTableBody.querySelector('td[colspan]')) {
      logTableBody.innerHTML = '';
    }

    const row = document.createElement('tr');
    let badgeClass = 'badge-dispatched';
    let statusLabel = item.status || 'Received';

    if (item.status === 'success') badgeClass = 'badge-success';
    else if (item.status === 'denied') badgeClass = 'badge-denied';
    else if (item.status === 'error') badgeClass = 'badge-error';

    const paramsStr = item.params ? JSON.stringify(item.params) : '-';
    const resultStr = item.result ? (item.result.message || JSON.stringify(item.result)) : (item.message || '-');

    row.innerHTML = `
      <td style="color: #94a3b8;">${item.time || new Date().toLocaleTimeString()}</td>
      <td style="font-weight: 600; color: #38bdf8;">${item.action || item.type}</td>
      <td style="color: #cbd5e1; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${paramsStr}</td>
      <td><span class="badge-tag ${badgeClass}">${statusLabel}</span></td>
      <td style="color: #e2e8f0; max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${resultStr}</td>
    `;

    logTableBody.prepend(row);
  }
}

