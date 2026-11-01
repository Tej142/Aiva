const { app, BrowserWindow } = require('electron');
const path = require('path');
const fs = require('fs');

const outDir = path.join(__dirname, '..', 'docs', 'screenshots');
if (!fs.existsSync(outDir)) {
  fs.mkdirSync(outDir, { recursive: true });
}

async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    show: false,
    frame: false,
    webPreferences: {
      preload: path.join(__dirname, '..', 'windows-agent', 'src', 'preload', 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  const loginPath = path.join(__dirname, '..', 'windows-agent', 'src', 'renderer', 'login.html');
  const mainPath = path.join(__dirname, '..', 'windows-agent', 'src', 'renderer', 'main.html');

  async function snap(name) {
    await sleep(350);
    const image = await win.webContents.capturePage();
    const dest = path.join(outDir, name);
    fs.writeFileSync(dest, image.toPNG());
    console.log(`Saved screenshot: ${name} (${image.getSize().width}x${image.getSize().height})`);
  }

  // ==========================================
  // AUTH VIEWS: login, register, otp, loading, error
  // ==========================================
  await win.loadFile(loginPath);
  await sleep(600);

  // 1. Login Dark
  await snap('login_dark.png');

  // 1b. Login Light
  await win.webContents.executeJavaScript(`document.documentElement.setAttribute('data-theme', 'light');`);
  await snap('login_light.png');
  await win.webContents.executeJavaScript(`document.documentElement.removeAttribute('data-theme');`);

  // 2. Register Dark & Light
  await win.webContents.executeJavaScript(`document.getElementById('tab-register').click();`);
  await snap('register_dark.png');
  await win.webContents.executeJavaScript(`document.documentElement.setAttribute('data-theme', 'light');`);
  await snap('register_light.png');
  await win.webContents.executeJavaScript(`document.documentElement.removeAttribute('data-theme');`);

  // 3. OTP Dark & Light
  await win.webContents.executeJavaScript(`
    document.getElementById('view-login').style.display = 'none';
    document.getElementById('view-register').style.display = 'none';
    document.getElementById('view-otp').style.display = 'block';
    document.getElementById('auth-tab-bar').style.display = 'none';
    document.getElementById('otp-masked-email').textContent = 'Sent to c***@gmail.com';
    document.getElementById('otp-1').value = '4';
    document.getElementById('otp-2').value = '9';
    document.getElementById('otp-3').value = '2';
    document.getElementById('otp-4').value = '8';
  `);
  await snap('otp_dark.png');
  await win.webContents.executeJavaScript(`document.documentElement.setAttribute('data-theme', 'light');`);
  await snap('otp_light.png');
  await win.webContents.executeJavaScript(`document.documentElement.removeAttribute('data-theme');`);

  // 4. Loading Dark & Light
  await win.webContents.executeJavaScript(`
    document.getElementById('view-otp').style.display = 'none';
    document.getElementById('view-loading').style.display = 'flex';
    document.getElementById('loading-greeting').textContent = 'Welcome, Charan!';
  `);
  await snap('loading_dark.png');
  await win.webContents.executeJavaScript(`document.documentElement.setAttribute('data-theme', 'light');`);
  await snap('loading_light.png');
  await win.webContents.executeJavaScript(`document.documentElement.removeAttribute('data-theme');`);

  // 5. Error State Dark & Light
  await win.webContents.executeJavaScript(`
    document.getElementById('view-loading').style.display = 'none';
    document.getElementById('view-otp').style.display = 'block';
    const err = document.getElementById('auth-error-banner');
    err.textContent = 'Invalid verification code. 4 attempts remaining.';
    err.style.display = 'flex';
    document.getElementById('otp-container').classList.add('error-shake');
  `);
  await snap('error_dark.png');
  await win.webContents.executeJavaScript(`document.documentElement.setAttribute('data-theme', 'light');`);
  await snap('error_light.png');
  await win.webContents.executeJavaScript(`document.documentElement.removeAttribute('data-theme');`);

  // ==========================================
  // MAIN APP VIEWS: home, devices, activity, settings
  // ==========================================
  await win.loadFile(mainPath);
  await sleep(600);

  // Set fake user & device info in main view
  await win.webContents.executeJavaScript(`
    document.getElementById('title-device-name').textContent = 'Zenith-PC';
    document.getElementById('title-user-email').textContent = 'Charan';
    document.getElementById('title-avatar-initials').textContent = 'C';
  `);

  // 6. Home (Assistant) Dark & Light
  await snap('home_dark.png');
  await win.webContents.executeJavaScript(`document.documentElement.setAttribute('data-theme', 'light');`);
  await snap('home_light.png');
  await win.webContents.executeJavaScript(`document.documentElement.removeAttribute('data-theme');`);

  // 7. Devices View Dark & Light
  await win.webContents.executeJavaScript(`
    document.getElementById('nav-devices').click();
    const grid = document.getElementById('devices-grid');
    grid.innerHTML = \`
      <div class="device-card">
        <div>
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span class="status-dot online"></span>
              <strong style="font-size: var(--text-base);">Zenith-PC</strong>
            </div>
            <span class="badge badge-accent">This Device</span>
          </div>
          <p style="font-size: var(--text-xs); color: var(--text-secondary); margin-bottom: 4px;">
            Platform: WINDOWS • App v1.0.0
          </p>
          <p style="font-size: var(--text-xs); color: var(--text-tertiary);">Status: Online</p>
        </div>
        <div style="display: flex; gap: 8px; margin-top: 16px;">
          <button class="btn btn-secondary" style="flex: 1; font-size: var(--text-xs);">Rename</button>
        </div>
      </div>
      <div class="device-card">
        <div>
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <div style="display: flex; align-items: center; gap: 8px;">
              <span class="status-dot offline"></span>
              <strong style="font-size: var(--text-base);">Living Room Mini</strong>
            </div>
          </div>
          <p style="font-size: var(--text-xs); color: var(--text-secondary); margin-bottom: 4px;">
            Platform: WINDOWS • App v1.0.0
          </p>
          <p style="font-size: var(--text-xs); color: var(--text-tertiary);">Status: Last seen 2h ago</p>
        </div>
        <div style="display: flex; gap: 8px; margin-top: 16px;">
          <button class="btn btn-secondary" style="flex: 1; font-size: var(--text-xs);">Rename</button>
          <button class="btn btn-danger" style="font-size: var(--text-xs);">Revoke</button>
        </div>
      </div>
    \`;
  `);
  await snap('devices_dark.png');
  await win.webContents.executeJavaScript(`document.documentElement.setAttribute('data-theme', 'light');`);
  await snap('devices_light.png');
  await win.webContents.executeJavaScript(`document.documentElement.removeAttribute('data-theme');`);

  // 8. Activity View Dark & Light
  await win.webContents.executeJavaScript(`
    document.getElementById('nav-activity').click();
    const tbody = document.getElementById('activity-table-body');
    tbody.innerHTML = \`
      <tr style="border-bottom: 1px solid var(--border-subtle);">
        <td style="padding: 10px 16px; color: var(--text-tertiary);">19:42:10</td>
        <td style="padding: 10px 16px; font-weight: 500;">open_url</td>
        <td style="padding: 10px 16px; font-family: var(--font-mono); color: var(--text-secondary);">{"url":"https://youtube.com"}</td>
        <td style="padding: 10px 16px;"><span class="badge badge-success">SUCCESS</span></td>
        <td style="padding: 10px 16px; color: var(--text-tertiary);">18ms</td>
      </tr>
      <tr style="border-bottom: 1px solid var(--border-subtle);">
        <td style="padding: 10px 16px; color: var(--text-tertiary);">19:40:02</td>
        <td style="padding: 10px 16px; font-weight: 500;">launch_app</td>
        <td style="padding: 10px 16px; font-family: var(--font-mono); color: var(--text-secondary);">{"app_name":"notepad"}</td>
        <td style="padding: 10px 16px;"><span class="badge badge-success">SUCCESS</span></td>
        <td style="padding: 10px 16px; color: var(--text-tertiary);">24ms</td>
      </tr>
      <tr style="border-bottom: 1px solid var(--border-subtle);">
        <td style="padding: 10px 16px; color: var(--text-tertiary);">19:35:48</td>
        <td style="padding: 10px 16px; font-weight: 500;">volume_control</td>
        <td style="padding: 10px 16px; font-family: var(--font-mono); color: var(--text-secondary);">{"level":30}</td>
        <td style="padding: 10px 16px;"><span class="badge badge-success">SUCCESS</span></td>
        <td style="padding: 10px 16px; color: var(--text-tertiary);">12ms</td>
      </tr>
      <tr style="border-bottom: 1px solid var(--border-subtle);">
        <td style="padding: 10px 16px; color: var(--text-tertiary);">19:30:15</td>
        <td style="padding: 10px 16px; font-weight: 500;">power_control</td>
        <td style="padding: 10px 16px; font-family: var(--font-mono); color: var(--text-secondary);">{"action":"lock"}</td>
        <td style="padding: 10px 16px;"><span class="badge badge-danger">DENIED</span></td>
        <td style="padding: 10px 16px; color: var(--text-tertiary);">6ms</td>
      </tr>
    \`;
  `);
  await snap('activity_dark.png');
  await win.webContents.executeJavaScript(`document.documentElement.setAttribute('data-theme', 'light');`);
  await snap('activity_light.png');
  await win.webContents.executeJavaScript(`document.documentElement.removeAttribute('data-theme');`);

  // 9. Settings View Dark & Light
  await win.webContents.executeJavaScript(`
    document.getElementById('nav-settings').click();
    document.getElementById('cfg-user-name').value = 'Charan';
    document.getElementById('cfg-user-email').value = 'charanpc1420091@gmail.com';
    document.getElementById('cfg-server-url').value = 'https://aiva-hub.duckdns.org';
    document.getElementById('perm-open-apps').checked = true;
    document.getElementById('perm-media-volume').checked = true;
    document.getElementById('perm-keyboard').checked = false;
    document.getElementById('perm-screenshot').checked = false;
    document.getElementById('perm-power').checked = false;
  `);
  await snap('settings_dark.png');
  await win.webContents.executeJavaScript(`document.documentElement.setAttribute('data-theme', 'light');`);
  await snap('settings_light.png');
  await win.webContents.executeJavaScript(`document.documentElement.removeAttribute('data-theme');`);

  console.log('ALL SCREENSHOTS CAPTURED SUCCESSFULLY.');
  app.quit();
});
