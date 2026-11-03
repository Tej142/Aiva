// =====================================================================
// AIVA DEVICE AGENT — CLIENT RENDERER SCRIPT (renderer.js)
// Unified design system controller for frameless desktop companion.
// =====================================================================

// Toast Notification Engine
function showToast(message, type = 'info') {
  if (!message || typeof message !== 'string' || message.includes('undefined')) return;
  const container = document.getElementById('toast-root');
  if (!container) return;

  // Deduplicate active toasts
  const existing = Array.from(container.children);
  if (existing.some(t => t.textContent.replace(/\s+/g, ' ').trim() === message.replace(/\s+/g, ' ').trim())) {
    return;
  }

  const toast = document.createElement('div');
  toast.className = 'toast';

  let iconSvg = '';
  if (type === 'success') {
    iconSvg = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#10B981" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>`;
  } else if (type === 'error') {
    iconSvg = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#EF4444" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`;
  } else {
    iconSvg = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#6366F1" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`;
  }

  toast.innerHTML = `
    ${iconSvg}
    <div style="flex: 1; font-size: 13px;">${message}</div>
  `;

  container.appendChild(toast);
  setTimeout(() => {
    toast.style.transition = 'all 0.25s ease-out';
    toast.style.opacity = '0';
    toast.style.transform = 'translateY(10px)';
    setTimeout(() => toast.remove(), 260);
  }, 4000);
}

// Window Titlebar Controls Setup
function setupTitleBarControls() {
  const minBtn = document.getElementById('win-min');
  const maxBtn = document.getElementById('win-max');
  const closeBtn = document.getElementById('win-close');

  if (minBtn) minBtn.addEventListener('click', () => window.aivaAgent.windowControl('minimize'));
  if (maxBtn) maxBtn.addEventListener('click', () => window.aivaAgent.windowControl('maximize'));
  if (closeBtn) closeBtn.addEventListener('click', () => window.aivaAgent.windowControl('close'));
}

// Helper: Mask email address (e.g., charanpc1420091@gmail.com -> c***1@gmail.com)
function maskEmail(email) {
  if (!email || !email.includes('@')) return email;
  const parts = email.split('@');
  const user = parts[0];
  const domain = parts[1];
  if (user.length <= 2) return `${user[0]}***@${domain}`;
  return `${user[0]}***${user[user.length - 1]}@${domain}`;
}

// ---------------------------------------------------------------------
// 1. AUTH SCREEN VIEW CONTROLLER (Login / Register / OTP / Loading)
// ---------------------------------------------------------------------
async function initAuthView() {
  setupTitleBarControls();

  const tabLogin = document.getElementById('tab-login');
  const tabRegister = document.getElementById('tab-register');
  const viewLogin = document.getElementById('view-login');
  const viewRegister = document.getElementById('view-register');
  const viewOtp = document.getElementById('view-otp');
  const viewLoading = document.getElementById('view-loading');
  const errorBanner = document.getElementById('auth-error-banner');

  const loginEmailInput = document.getElementById('login-email');
  const btnLoginSubmit = document.getElementById('btn-login-submit');

  const regNameInput = document.getElementById('reg-name');
  const regEmailInput = document.getElementById('reg-email');
  const regTermsCheckbox = document.getElementById('reg-terms');
  const btnRegisterSubmit = document.getElementById('btn-register-submit');

  const otpContainer = document.getElementById('otp-container');
  const otpMaskedEmail = document.getElementById('otp-masked-email');
  const btnOtpSubmit = document.getElementById('btn-otp-submit');
  const btnOtpChangeEmail = document.getElementById('btn-otp-change-email');
  const btnOtpResend = document.getElementById('btn-otp-resend');
  const loadingGreeting = document.getElementById('loading-greeting');

  let activeEmail = '';
  let activeName = '';
  let resendCountdownTimer = null;
  let countdownSec = 0;

  function showError(msg) {
    if (errorBanner) {
      errorBanner.innerHTML = `
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <span>${msg}</span>
      `;
      errorBanner.style.display = 'flex';
    }
  }

  function clearError() {
    if (errorBanner) errorBanner.style.display = 'none';
  }

  // Preload remembered email and server URL
  const serverLabel = document.getElementById('current-server-label');
  const btnChangeServer = document.getElementById('btn-change-server');

  try {
    const cfg = await window.aivaAgent.getConfig();
    if (cfg) {
      if (cfg.userEmail) {
        loginEmailInput.value = cfg.userEmail;
        regEmailInput.value = cfg.userEmail;
      }
      if (serverLabel && cfg.serverUrl) {
        serverLabel.textContent = cfg.serverUrl;
      }
    }
  } catch {}

  if (btnChangeServer) {
    btnChangeServer.addEventListener('click', async () => {
      const current = serverLabel ? serverLabel.textContent : 'https://aivaserve.duckdns.org';
      const newUrl = prompt('Enter AIVA Server URL (e.g. https://aivaserve.duckdns.org):', current);
      if (newUrl && newUrl.trim()) {
        const clean = newUrl.trim().replace(/\/+$/, '');
        await window.aivaAgent.saveConfig({ serverUrl: clean });
        if (serverLabel) serverLabel.textContent = clean;
        showToast(`Server updated to ${clean}`, 'success');
      }
    });
  }

  // Tab Switching
  tabLogin.addEventListener('click', () => {
    tabLogin.classList.add('active');
    tabRegister.classList.remove('active');
    viewLogin.style.display = 'block';
    viewRegister.style.display = 'none';
    viewOtp.style.display = 'none';
    clearError();
    if (regEmailInput.value) loginEmailInput.value = regEmailInput.value;
    loginEmailInput.focus();
  });

  tabRegister.addEventListener('click', () => {
    tabRegister.classList.add('active');
    tabLogin.classList.remove('active');
    viewRegister.style.display = 'block';
    viewLogin.style.display = 'none';
    viewOtp.style.display = 'none';
    clearError();
    if (loginEmailInput.value) regEmailInput.value = loginEmailInput.value;
    regNameInput.focus();
  });

  // Login Submit
  btnLoginSubmit.addEventListener('click', async () => {
    const email = loginEmailInput.value.trim();
    if (!email || !email.includes('@')) {
      showError('Please enter a valid email address.');
      return;
    }

    clearError();
    btnLoginSubmit.classList.add('loading');

    const res = await window.aivaAgent.requestOtp(email);
    btnLoginSubmit.classList.remove('loading');

    if (res.status === 'success') {
      activeEmail = email;
      activeName = '';
      transitionToOtp();
    } else {
      showError(res.message || 'Failed to send verification code. Please try again.');
    }
  });

  // Register Submit
  btnRegisterSubmit.addEventListener('click', async () => {
    const name = regNameInput.value.trim();
    const email = regEmailInput.value.trim();

    if (!name) {
      showError('Please enter your full name.');
      regNameInput.focus();
      return;
    }
    if (!email || !email.includes('@')) {
      showError('Please enter a valid email address.');
      regEmailInput.focus();
      return;
    }
    if (!regTermsCheckbox.checked) {
      showError('You must agree to the Terms and Privacy Policy to continue.');
      return;
    }

    clearError();
    btnRegisterSubmit.classList.add('loading');

    const res = await window.aivaAgent.register({ name, email });
    btnRegisterSubmit.classList.remove('loading');

    if (res.status === 'success') {
      activeEmail = email;
      activeName = name;
      transitionToOtp();
    } else {
      showError(res.message || 'Registration failed. Please try again.');
    }
  });

  // Transition to OTP screen
  function transitionToOtp() {
    viewLogin.style.display = 'none';
    viewRegister.style.display = 'none';
    viewOtp.style.display = 'block';
    document.getElementById('auth-tab-bar').style.display = 'none';
    clearError();

    otpMaskedEmail.textContent = `Sent to ${maskEmail(activeEmail)}`;
    resetOtpBoxes();
    startResendCountdown(60);
    document.getElementById('otp-1').focus();
  }

  // OTP Box Auto-Advance, Backspace, and Paste
  const otpBoxes = [1, 2, 3, 4, 5, 6].map(i => document.getElementById(`otp-${i}`));

  function resetOtpBoxes() {
    otpBoxes.forEach(b => { if (b) b.value = ''; });
  }

  function getEnteredOtp() {
    return otpBoxes.map(b => b.value).join('');
  }

  otpBoxes.forEach((box, index) => {
    box.addEventListener('input', (e) => {
      const val = e.target.value.replace(/[^0-9]/g, '');
      box.value = val ? val[0] : '';
      if (val && index < 5) {
        otpBoxes[index + 1].focus();
      }
      if (getEnteredOtp().length === 6) {
        btnOtpSubmit.click();
      }
    });

    box.addEventListener('keydown', (e) => {
      if (e.key === 'Backspace' && !box.value && index > 0) {
        otpBoxes[index - 1].focus();
      }
    });

    box.addEventListener('paste', (e) => {
      e.preventDefault();
      const pasteData = (e.clipboardData || window.clipboardData).getData('text').trim().replace(/[^0-9]/g, '');
      if (pasteData.length === 6) {
        pasteData.split('').forEach((ch, idx) => {
          if (otpBoxes[idx]) otpBoxes[idx].value = ch;
        });
        otpBoxes[5].focus();
        btnOtpSubmit.click();
      }
    });
  });

  // 60-Second Countdown Timer
  function startResendCountdown(seconds) {
    if (resendCountdownTimer) clearInterval(resendCountdownTimer);
    countdownSec = seconds;
    btnOtpResend.disabled = true;
    btnOtpResend.textContent = `Resend in ${countdownSec}s`;

    resendCountdownTimer = setInterval(() => {
      countdownSec--;
      if (countdownSec <= 0) {
        clearInterval(resendCountdownTimer);
        btnOtpResend.disabled = false;
        btnOtpResend.textContent = 'Resend Code';
      } else {
        btnOtpResend.textContent = `Resend in ${countdownSec}s`;
      }
    }, 1000);
  }

  btnOtpResend.addEventListener('click', async () => {
    if (btnOtpResend.disabled) return;
    clearError();
    btnOtpResend.disabled = true;
    btnOtpResend.textContent = 'Sending...';

    const res = await window.aivaAgent.requestOtp(activeEmail);
    if (res.status === 'success') {
      showToast('New verification code sent to your email.', 'success');
      startResendCountdown(60);
      resetOtpBoxes();
      document.getElementById('otp-1').focus();
    } else {
      showError(res.message || 'Failed to resend code.');
      btnOtpResend.disabled = false;
      btnOtpResend.textContent = 'Resend Code';
    }
  });

  btnOtpChangeEmail.addEventListener('click', () => {
    if (resendCountdownTimer) clearInterval(resendCountdownTimer);
    document.getElementById('auth-tab-bar').style.display = 'flex';
    viewOtp.style.display = 'none';
    viewLogin.style.display = 'block';
    clearError();
    loginEmailInput.focus();
  });

  // Verify OTP Submission
  btnOtpSubmit.addEventListener('click', async () => {
    const otp = getEnteredOtp();
    if (otp.length !== 6) {
      showError('Please enter all 6 digits of your code.');
      return;
    }

    clearError();
    btnOtpSubmit.classList.add('loading');

    const res = await window.aivaAgent.verifyOtp({
      email: activeEmail,
      otp: otp,
      name: activeName
    });

    btnOtpSubmit.classList.remove('loading');

    if (res.status === 'success') {
      // Transition to branded loading screen
      viewOtp.style.display = 'none';
      viewLoading.style.display = 'flex';

      const userName = (res.user && res.user.name) ? res.user.name : activeName || 'User';
      loadingGreeting.textContent = `Welcome, ${userName}!`;

      setTimeout(() => {
        window.aivaAgent.switchView('hud');
      }, 1200);

    } else {
      // Trigger error shake animation on OTP boxes
      otpContainer.classList.add('error-shake');
      setTimeout(() => otpContainer.classList.remove('error-shake'), 450);

      showError(res.message || 'Invalid or expired code. Please verify and try again.');
      resetOtpBoxes();
      document.getElementById('otp-1').focus();
    }
  });
}


// ---------------------------------------------------------------------
// 2. MAIN APP VIEW CONTROLLER (Assistant HUD / Devices / Activity / Settings)
// ---------------------------------------------------------------------
async function initMainView() {
  setupTitleBarControls();

  const titleDeviceName = document.getElementById('title-device-name');
  const titleStatusDot = document.getElementById('title-status-dot');
  const titleStatusText = document.getElementById('title-status-text');
  const titleAvatarInitials = document.getElementById('title-avatar-initials');
  const titleUserEmail = document.getElementById('title-user-email');

  const navButtons = document.querySelectorAll('.sidebar-btn[data-target]');
  const viewPanes = document.querySelectorAll('.view-pane');
  const btnSidebarReconnect = document.getElementById('btn-sidebar-reconnect');

  const hudFrame = document.getElementById('embedded-hud-frame');
  const hudLoading = document.getElementById('hud-loading-state');
  const hudError = document.getElementById('hud-error-state');
  const btnHudRetry = document.getElementById('btn-hud-retry');

  const devicesGrid = document.getElementById('devices-grid');
  const btnRefreshDevices = document.getElementById('btn-refresh-devices');

  const activityTableBody = document.getElementById('activity-table-body');
  const btnClearActivity = document.getElementById('btn-clear-activity');
  const filterChips = document.querySelectorAll('.filter-chips .chip');

  let currentTab = 'view-assistant';
  let activeFilter = 'all';
  let activityCache = [];

  // Load User & Config
  try {
    const cfg = await window.aivaAgent.getConfig();
    if (cfg) {
      if (titleDeviceName && cfg.deviceName) titleDeviceName.textContent = cfg.deviceName;
      if (titleUserEmail && cfg.userEmail) {
        titleUserEmail.textContent = cfg.userName || cfg.userEmail.split('@')[0];
        const initial = (cfg.userName || cfg.userEmail)[0].toUpperCase();
        if (titleAvatarInitials) titleAvatarInitials.textContent = initial;
      }

      // Load Settings inputs
      const nameInput = document.getElementById('cfg-user-name');
      const emailInput = document.getElementById('cfg-user-email');
      const serverInput = document.getElementById('cfg-server-url');
      const startToggle = document.getElementById('toggle-startup');
      const bgToggle = document.getElementById('toggle-background');

      if (nameInput) nameInput.value = cfg.userName || '';
      if (emailInput) emailInput.value = cfg.userEmail || '';
      if (serverInput) serverInput.value = cfg.serverUrl || 'https://aivaserve.duckdns.org';
      if (startToggle) startToggle.checked = Boolean(cfg.startWithWindows);
      if (bgToggle) bgToggle.checked = Boolean(cfg.runInBackground);

      // Point iframe to cloud HUD with shared session
      loadEmbeddedHud(cfg.serverUrl);
    }
  } catch (err) {
    console.error('Error loading main view configuration:', err);
  }

  // Load Permissions
  try {
    const perms = await window.aivaAgent.getPermissions();
    if (perms) {
      const openApps = document.getElementById('perm-open-apps');
      const mediaVol = document.getElementById('perm-media-volume');
      const keyboard = document.getElementById('perm-keyboard');
      const screenshot = document.getElementById('perm-screenshot');
      const files = document.getElementById('perm-files');
      const power = document.getElementById('perm-power');

      if (openApps) openApps.checked = Boolean(perms.open_apps);
      if (mediaVol) mediaVol.checked = Boolean(perms.media_volume);
      if (keyboard) keyboard.checked = Boolean(perms.keyboard_input);
      if (screenshot) screenshot.checked = Boolean(perms.screenshot);
      if (files) files.checked = Boolean(perms.file_access);
      if (power) power.checked = Boolean(perms.power_control);

      setupPermissionChangeHandlers();
    }
  } catch {}

  function setupPermissionChangeHandlers() {
    const toggles = [
      { id: 'perm-open-apps', key: 'open_apps', dangerous: false },
      { id: 'perm-media-volume', key: 'media_volume', dangerous: false },
      { id: 'perm-keyboard', key: 'keyboard_input', dangerous: true, label: 'Keyboard Typing Input' },
      { id: 'perm-screenshot', key: 'screenshot', dangerous: true, label: 'Desktop Screenshot Capture' },
      { id: 'perm-files', key: 'file_access', dangerous: true, label: 'Local File Access' },
      { id: 'perm-power', key: 'power_control', dangerous: true, label: 'System Power & Lock' }
    ];

    toggles.forEach(t => {
      const el = document.getElementById(t.id);
      if (!el) return;
      el.addEventListener('change', async () => {
        if (t.dangerous && el.checked) {
          const ok = confirm(`Enable ${t.label}?\n\nThis elevated capability allows Cloud AIVA to interact directly with your Windows desktop.`);
          if (!ok) {
            el.checked = false;
            return;
          }
        }
        await saveAllPermissions();
        showToast('Device capability permissions updated.', 'success');
      });
    });
  }

  async function saveAllPermissions() {
    const openApps = document.getElementById('perm-open-apps');
    const mediaVol = document.getElementById('perm-media-volume');
    const keyboard = document.getElementById('perm-keyboard');
    const screenshot = document.getElementById('perm-screenshot');
    const files = document.getElementById('perm-files');
    const power = document.getElementById('perm-power');

    await window.aivaAgent.savePermissions({
      open_apps: openApps ? openApps.checked : true,
      media_volume: mediaVol ? mediaVol.checked : true,
      keyboard_input: keyboard ? keyboard.checked : false,
      screenshot: screenshot ? screenshot.checked : false,
      file_access: files ? files.checked : false,
      power_control: power ? power.checked : false
    });
  }

  // Sidebar Navigation
  navButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-target');
      navButtons.forEach(b => b.classList.remove('active'));
      viewPanes.forEach(p => p.classList.remove('active'));

      btn.classList.add('active');
      const pane = document.getElementById(target);
      if (pane) pane.classList.add('active');
      currentTab = target;

      if (target === 'view-devices') loadDevicesList();
      if (target === 'view-activity') renderActivityTable();
    });
  });

  // Reconnect Button
  if (btnSidebarReconnect) {
    btnSidebarReconnect.addEventListener('click', async () => {
      showToast('Reconnecting WebSocket command channel...', 'info');
      await window.aivaAgent.reconnectDevice();
    });
  }

  // Embedded HUD Loader with seamless SSO session bridge
  async function loadEmbeddedHud(serverUrl) {
    if (!hudFrame) return;
    const targetUrl = (serverUrl || 'https://aivaserve.duckdns.org').replace(/\/+$/, '');
    if (hudLoading) hudLoading.style.display = 'flex';
    if (hudError) hudError.style.display = 'none';

    let fullUrl = targetUrl;
    try {
      const tokens = await window.aivaAgent.getTokens();
      if (tokens && tokens.accessToken) {
        fullUrl += `#token=${encodeURIComponent(tokens.accessToken)}&refresh=${encodeURIComponent(tokens.refreshToken || "")}`;
      }
    } catch (e) {
      console.warn('Could not retrieve auth tokens for embedded HUD:', e);
    }

    hudFrame.src = fullUrl;

    hudFrame.onload = async () => {
      if (hudLoading) hudLoading.style.display = 'none';
      if (hudError) hudError.style.display = 'none';
      // Post-message backup sync
      try {
        const tokens = await window.aivaAgent.getTokens();
        if (tokens && tokens.accessToken && hudFrame.contentWindow) {
          hudFrame.contentWindow.postMessage({
            type: 'AIVA_AUTH_SESSION',
            tokens: { access_token: tokens.accessToken, refresh_token: tokens.refreshToken }
          }, '*');
        }
      } catch {}
    };

    hudFrame.onerror = () => {
      if (hudLoading) hudLoading.style.display = 'none';
      if (hudError) hudError.style.display = 'flex';
    };
  }

  // Handle token request from embedded frame
  window.addEventListener('message', async (event) => {
    if (event.data && event.data.type === 'AIVA_REQUEST_AUTH') {
      try {
        const tokens = await window.aivaAgent.getTokens();
        if (tokens && tokens.accessToken && hudFrame && hudFrame.contentWindow) {
          hudFrame.contentWindow.postMessage({
            type: 'AIVA_AUTH_SESSION',
            tokens: { access_token: tokens.accessToken, refresh_token: tokens.refreshToken }
          }, '*');
        }
      } catch (err) {
        console.warn('Error bridging auth session to iframe:', err);
      }
    }
  });

  if (btnHudRetry) {
    btnHudRetry.addEventListener('click', async () => {
      const cfg = await window.aivaAgent.getConfig();
      loadEmbeddedHud(cfg ? cfg.serverUrl : null);
    });
  }

  // Devices View Logic
  async function loadDevicesList() {
    if (!devicesGrid) return;
    devicesGrid.innerHTML = `
      <div class="skeleton" style="height: 140px;"></div>
      <div class="skeleton" style="height: 140px;"></div>
    `;

    const me = await window.aivaAgent.getMe();
    if (!me || !me.devices) {
      devicesGrid.innerHTML = `<div style="grid-column: 1/-1; text-align: center; color: var(--text-tertiary); padding: 40px;">Could not retrieve devices from cloud.</div>`;
      return;
    }

    const cfg = await window.aivaAgent.getConfig();
    const myDeviceId = cfg ? cfg.deviceId : null;

    devicesGrid.innerHTML = '';
    me.devices.forEach(dev => {
      const isThisDevice = dev.id === myDeviceId;
      const isOnline = dev.is_online || dev.status === 'online';
      const statusDotClass = isOnline ? 'online' : 'offline';
      const statusText = isOnline ? 'Online' : 'Offline';

      const card = document.createElement('div');
      card.className = 'device-card';
      card.innerHTML = `
        <div>
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span class="status-dot ${statusDotClass}"></span>
              <strong style="font-size: var(--text-base);">${dev.name}</strong>
            </div>
            ${isThisDevice ? '<span class="badge badge-accent">This Device</span>' : ''}
          </div>
          <p style="font-size: var(--text-xs); color: var(--text-secondary); margin-bottom: 4px;">
            Platform: ${dev.platform.toUpperCase()} • App v${dev.app_version || '1.0.0'}
          </p>
          <p style="font-size: var(--text-xs); color: var(--text-tertiary);">
            Status: ${statusText}
          </p>
        </div>
        <div style="display: flex; gap: 8px; margin-top: 16px;">
          <button class="btn btn-secondary btn-rename-dev" data-id="${dev.id}" data-name="${dev.name}" style="flex: 1; font-size: var(--text-xs);">Rename</button>
          ${!isThisDevice ? `<button class="btn btn-danger btn-revoke-dev" data-id="${dev.id}" style="font-size: var(--text-xs);">Revoke</button>` : ''}
        </div>
      `;
      devicesGrid.appendChild(card);
    });
  }

  // Activity Table Logic
  async function loadActivityLogs() {
    const status = await window.aivaAgent.getDeviceStatus();
    if (status && status.activityLogs) {
      activityCache = status.activityLogs;
      renderActivityTable();
    }
  }

  function renderActivityTable() {
    if (!activityTableBody) return;
    let list = activityCache;

    if (activeFilter === 'success') {
      list = list.filter(l => l.status === 'success');
    } else if (activeFilter === 'error') {
      list = list.filter(l => l.status === 'error' || l.status === 'rejected');
    } else if (activeFilter === 'app') {
      list = list.filter(l => l.action && (l.action.includes('app') || l.action.includes('url')));
    } else if (activeFilter === 'media') {
      list = list.filter(l => l.action && (l.action.includes('media') || l.action.includes('volume')));
    }

    if (list.length === 0) {
      activityTableBody.innerHTML = `
        <tr>
          <td colspan="5" style="text-align: center; color: var(--text-tertiary); padding: 40px;">
            No activity matches the selected filter.
          </td>
        </tr>
      `;
      return;
    }

    activityTableBody.innerHTML = '';
    list.slice(-50).reverse().forEach(item => {
      const isSuccess = item.status === 'success';
      const badgeClass = isSuccess ? 'badge-success' : 'badge-danger';
      const latencyStr = item.latencyMs ? `${Math.round(item.latencyMs)}ms` : '18ms';

      const row = document.createElement('tr');
      row.style.borderBottom = '1px solid var(--border-subtle)';
      row.innerHTML = `
        <td style="padding: 10px 16px; color: var(--text-tertiary);">${item.time || 'Just now'}</td>
        <td style="padding: 10px 16px; font-weight: 500;">${item.action || 'command'}</td>
        <td style="padding: 10px 16px; font-family: var(--font-mono); color: var(--text-secondary); max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
          ${JSON.stringify(item.params || {})}
        </td>
        <td style="padding: 10px 16px;">
          <span class="badge ${badgeClass}">${(item.status || 'done').toUpperCase()}</span>
        </td>
        <td style="padding: 10px 16px; color: var(--text-tertiary);">${latencyStr}</td>
      `;
      activityTableBody.appendChild(row);
    });
  }

  filterChips.forEach(chip => {
    chip.addEventListener('click', () => {
      filterChips.forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      activeFilter = chip.getAttribute('data-filter');
      renderActivityTable();
    });
  });

  if (btnClearActivity) {
    btnClearActivity.addEventListener('click', () => {
      activityCache = [];
      renderActivityTable();
      showToast('Activity log cleared.', 'info');
    });
  }

  // Settings Save Handlers
  const btnSaveServerUrl = document.getElementById('btn-save-server-url');
  if (btnSaveServerUrl) {
    btnSaveServerUrl.addEventListener('click', async () => {
      const url = document.getElementById('cfg-server-url').value.trim();
      await window.aivaAgent.saveConfig({ serverUrl: url });
      showToast('Cloud server URL saved. Reloading HUD...', 'success');
      loadEmbeddedHud(url);
    });
  }

  const toggleStartup = document.getElementById('toggle-startup');
  if (toggleStartup) {
    toggleStartup.addEventListener('change', async () => {
      await window.aivaAgent.saveConfig({ startWithWindows: toggleStartup.checked });
      showToast(`Startup on boot ${toggleStartup.checked ? 'enabled' : 'disabled'}.`, 'info');
    });
  }

  const toggleBackground = document.getElementById('toggle-background');
  if (toggleBackground) {
    toggleBackground.addEventListener('change', async () => {
      await window.aivaAgent.saveConfig({ runInBackground: toggleBackground.checked });
      showToast(`Run in system tray ${toggleBackground.checked ? 'enabled' : 'disabled'}.`, 'info');
    });
  }

  // Appearance Themes
  const btnThemeDark = document.getElementById('btn-theme-dark');
  const btnThemeLight = document.getElementById('btn-theme-light');
  const btnThemeSystem = document.getElementById('btn-theme-system');

  function setTheme(theme) {
    if (theme === 'light') {
      document.documentElement.setAttribute('data-theme', 'light');
    } else if (theme === 'dark') {
      document.documentElement.setAttribute('data-theme', 'dark');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    localStorage.setItem('aiva-theme', theme);
  }

  if (btnThemeDark) btnThemeDark.addEventListener('click', () => setTheme('dark'));
  if (btnThemeLight) btnThemeLight.addEventListener('click', () => setTheme('light'));
  if (btnThemeSystem) btnThemeSystem.addEventListener('click', () => setTheme('system'));

  const savedTheme = localStorage.getItem('aiva-theme') || 'dark';
  setTheme(savedTheme);

  // Logout Handler
  const btnLogout = document.getElementById('btn-logout');
  if (btnLogout) {
    btnLogout.addEventListener('click', async () => {
      const ok = confirm('Are you sure you want to log out of this Windows computer?');
      if (ok) {
        localStorage.clear();
        sessionStorage.clear();
        await window.aivaAgent.logout();
      }
    });
  }

  // Event Listeners from WebSocket channel
  window.aivaAgent.onStatusChange((status) => {
    if (titleStatusText && titleStatusDot) {
      if (status.state === 'connected') {
        titleStatusDot.className = 'status-dot online';
        titleStatusText.textContent = 'Connected';
      } else if (status.state === 'connecting' || status.state === 'reconnecting') {
        titleStatusDot.className = 'status-dot connecting';
        titleStatusText.textContent = 'Reconnecting';
      } else {
        titleStatusDot.className = 'status-dot offline';
        titleStatusText.textContent = 'Offline';
      }
    }
  });

  window.aivaAgent.onActivityLog((item) => {
    if (!item) return;
    activityCache.push(item);
    if (currentTab === 'view-activity') {
      renderActivityTable();
    }
    if (item.type === 'command_result' && item.action) {
      const actionName = item.action.replace(/_/g, ' ');
      const title = actionName.charAt(0).toUpperCase() + actionName.slice(1);
      const isSuccess = item.status === 'success';
      showToast(`${title} ${isSuccess ? 'completed' : 'failed'}`, isSuccess ? 'success' : 'error');
    }
  });

  // Initial load
  loadActivityLogs();
}
