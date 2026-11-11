// =====================================================================
// AIVA HUD — JAVASCRIPT CLIENT CONTROLLER (ui/app.js)
// Unified design system & authenticated session management
// =====================================================================

document.addEventListener('DOMContentLoaded', () => {
    syncEmbeddedSession();
    initTheme();
    initClock();
    initNavigation();
    initChat();
    initSpeechRecognition();
    initVoiceModeControls();
    initWebAuthModal();
    initWhatsApp();
    initMemoryControls();
    checkAuthStatus();
    pollSystemStatus();
});

// ---------------------------------------------------------------------
// 0. Theme Manager (Dark / Light / System)
// ---------------------------------------------------------------------
function initTheme() {
    const savedTheme = localStorage.getItem('aiva_theme') || 'dark';
    applyTheme(savedTheme);

    const btn = document.getElementById('btn-theme-toggle');
    if (btn) {
        btn.addEventListener('click', () => {
            const current = localStorage.getItem('aiva_theme') || 'dark';
            let next = 'dark';
            if (current === 'dark') next = 'light';
            else if (current === 'light') next = 'system';
            else next = 'dark';

            localStorage.setItem('aiva_theme', next);
            applyTheme(next);
            showHudToast(`Theme set to ${next.toUpperCase()}`);
        });
    }
}

function applyTheme(theme) {
    if (theme === 'light') {
        document.documentElement.setAttribute('data-theme', 'light');
    } else if (theme === 'dark') {
        document.documentElement.setAttribute('data-theme', 'dark');
    } else {
        document.documentElement.removeAttribute('data-theme');
    }
}

function showHudToast(message, type = 'info') {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.className = 'toast-container';
        document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    toast.className = 'toast';
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        toast.style.transform = 'translateY(10px)';
        setTimeout(() => toast.remove(), 250);
    }, 2500);
}

// ---------------------------------------------------------------------
// 1. Live HUD Clock
// ---------------------------------------------------------------------
function initClock() {
    const clockEl = document.getElementById('live-clock');
    function update() {
        const now = new Date();
        const hrs = String(now.getHours()).padStart(2, '0');
        const mins = String(now.getMinutes()).padStart(2, '0');
        const secs = String(now.getSeconds()).padStart(2, '0');
        if (clockEl) {
            clockEl.textContent = `${hrs}:${mins}:${secs}`;
        }
    }
    update();
    setInterval(update, 1000);
}

// ---------------------------------------------------------------------
// 2. Authentication & Session Management
// ---------------------------------------------------------------------
function syncEmbeddedSession() {
    const isEmbedded = window.self !== window.top;
    if (isEmbedded) {
        document.body.classList.add('is-embedded');
        const overlay = document.getElementById('web-auth-overlay');
        if (overlay) overlay.style.display = 'none';
    }

    if (window.location.hash) {
        try {
            const hash = window.location.hash.substring(1);
            const params = new URLSearchParams(hash);
            const token = params.get('token');
            const refresh = params.get('refresh');
            if (token) {
                setTokens({ access_token: token, refresh_token: refresh || '' });
                history.replaceState(null, '', window.location.pathname + window.location.search);
            }
        } catch (e) {
            console.warn('Failed to parse auth hash:', e);
        }
    }

    if (isEmbedded && !getAccessToken()) {
        try {
            window.parent.postMessage({ type: 'AIVA_REQUEST_AUTH' }, '*');
        } catch {}
    }
}

window.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'AIVA_AUTH_SESSION' && event.data.tokens) {
        setTokens(event.data.tokens);
        checkAuthStatus();
    } else if (event.data && event.data.type === 'AIVA_NAVIGATE_TAB' && event.data.tab) {
        const navBtn = document.querySelector(`.nav-item[data-tab="${event.data.tab}"]`);
        if (navBtn) navBtn.click();
    }
});

function getAccessToken() {
    let token = localStorage.getItem('aiva_access_token') || '';
    if (!token && document.cookie) {
        const m = document.cookie.match(/(?:^|;\s*)aiva_access_token=([^;]+)/);
        if (m) {
            token = decodeURIComponent(m[1]);
            localStorage.setItem('aiva_access_token', token);
        }
    }
    return token;
}

async function refreshAccessToken() {
    let refreshToken = localStorage.getItem('aiva_refresh_token');
    if (!refreshToken && (window.self !== window.top || document.body.classList.contains('is-embedded'))) {
        try {
            window.parent.postMessage({ type: 'AIVA_REQUEST_AUTH' }, '*');
            await new Promise(r => setTimeout(r, 350));
            refreshToken = localStorage.getItem('aiva_refresh_token');
        } catch {}
    }
    if (!refreshToken) return null;
    try {
        const resp = await fetch('/api/auth/refresh', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ refresh_token: refreshToken })
        });
        if (resp.status === 200) {
            const data = await resp.json();
            if (data.tokens && data.tokens.access_token) {
                setTokens(data.tokens);
                return data.tokens.access_token;
            }
        }
    } catch (e) {
        console.warn('Silent token refresh failed:', e);
    }
    return null;
}

function setTokens(tokens) {
    if (tokens && tokens.access_token) {
        localStorage.setItem('aiva_access_token', tokens.access_token);
        if (tokens.refresh_token) {
            localStorage.setItem('aiva_refresh_token', tokens.refresh_token);
        }
    }
}

function authHeaders() {
    const token = getAccessToken();
    const headers = { 'Content-Type': 'application/json' };
    if (token) {
        headers['Authorization'] = `Bearer ${token}`;
    }
    return headers;
}

function onAuthSuccess(user) {
    const overlay = document.getElementById('web-auth-overlay');
    if (overlay) overlay.style.display = 'none';

    const logoutBtn = document.getElementById('btn-web-logout');
    if (logoutBtn) logoutBtn.style.display = 'inline-flex';

    const userNameDisplay = document.getElementById('user-display-name');
    if (userNameDisplay) {
        userNameDisplay.textContent = user.name || user.email.split('@')[0];
    }

    const userAvatarInitials = document.getElementById('user-avatar-initials');
    if (userAvatarInitials) {
        const initial = (user.name || user.email.split('@')[0] || 'C').charAt(0).toUpperCase();
        userAvatarInitials.textContent = initial;
    }

    // Load fresh data for this authenticated user
    loadMemoryData();
    loadConversationHistory();
    connectHudWebSocket();
    loadWhatsAppStatus();
    startWhatsAppStatusPolling();
}

function showWebAuthModal() {
    const isEmbedded = window.self !== window.top || document.body.classList.contains('is-embedded');
    const overlay = document.getElementById('web-auth-overlay');
    if (isEmbedded) {
        // Inside desktop companion app, never show web auth modal overlay
        if (overlay) overlay.style.display = 'none';
        return;
    }
    if (overlay) overlay.style.display = 'flex';

    const logoutBtn = document.getElementById('btn-web-logout');
    if (logoutBtn) logoutBtn.style.display = 'none';

    const userNameDisplay = document.getElementById('user-display-name');
    if (userNameDisplay) userNameDisplay.textContent = 'Guest';
}

async function checkAuthStatus() {
    try {
        const resp = await fetch('/api/auth/me', { headers: authHeaders() });
        if (resp.status === 200) {
            const data = await resp.json();
            if (data.user) {
                onAuthSuccess(data.user);
                return true;
            }
        }
    } catch (e) {
        console.warn('Auth check failed:', e);
    }
    showWebAuthModal();
    return false;
}

function initWebAuthModal() {
    const tabLogin = document.getElementById('web-tab-login');
    const tabRegister = document.getElementById('web-tab-register');
    const paneLogin = document.getElementById('web-pane-login');
    const paneRegister = document.getElementById('web-pane-register');
    const paneOtp = document.getElementById('web-pane-otp');
    const errorBox = document.getElementById('web-auth-error');

    const emailLoginInput = document.getElementById('web-login-email');
    const btnLoginSubmit = document.getElementById('web-btn-login-submit');

    const nameRegInput = document.getElementById('web-reg-name');
    const emailRegInput = document.getElementById('web-reg-email');
    const termsRegCheckbox = document.getElementById('web-reg-terms');
    const btnRegisterSubmit = document.getElementById('web-btn-register-submit');

    const otpBoxes = [1, 2, 3, 4, 5, 6].map(i => document.getElementById(`web-otp-${i}`));
    const btnOtpSubmit = document.getElementById('web-btn-otp-submit');
    const btnChangeEmail = document.getElementById('web-btn-change-email');
    const btnResendOtp = document.getElementById('web-btn-resend-otp');
    const otpHint = document.getElementById('web-otp-hint');

    const btnLogout = document.getElementById('btn-web-logout');

    let activeEmail = '';
    let activeName = '';

    function showError(msg) {
        if (errorBox) {
            errorBox.textContent = msg;
            errorBox.style.display = 'block';
        }
    }

    function clearError() {
        if (errorBox) errorBox.style.display = 'none';
    }

    // Tab Switching
    if (tabLogin && tabRegister) {
        tabLogin.addEventListener('click', () => {
            tabLogin.classList.add('active');
            tabRegister.classList.remove('active');
            paneLogin.style.display = 'block';
            paneRegister.style.display = 'none';
            paneOtp.style.display = 'none';
            clearError();
            if (emailRegInput && emailRegInput.value) emailLoginInput.value = emailRegInput.value;
        });

        tabRegister.addEventListener('click', () => {
            tabRegister.classList.add('active');
            tabLogin.classList.remove('active');
            paneRegister.style.display = 'block';
            paneLogin.style.display = 'none';
            paneOtp.style.display = 'none';
            clearError();
            if (emailLoginInput && emailLoginInput.value) emailRegInput.value = emailLoginInput.value;
        });
    }

    // Login Submit
    if (btnLoginSubmit) {
        btnLoginSubmit.addEventListener('click', async () => {
            const email = emailLoginInput.value.trim();
            if (!email || !email.includes('@')) {
                showError('Please enter a valid email address.');
                return;
            }

            clearError();
            btnLoginSubmit.classList.add('loading');

            try {
                const resp = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email })
                });
                const data = await resp.json();
                btnLoginSubmit.classList.remove('loading');

                if (data.status === 'success') {
                    activeEmail = email;
                    activeName = '';
                    transitionToWebOtp();
                } else {
                    showError(data.message || 'Failed to send login code.');
                }
            } catch (err) {
                btnLoginSubmit.classList.remove('loading');
                showError('Server connection error. Please try again.');
            }
        });
    }

    // Register Submit
    if (btnRegisterSubmit) {
        btnRegisterSubmit.addEventListener('click', async () => {
            const name = nameRegInput.value.trim();
            const email = emailRegInput.value.trim();

            if (!name) {
                showError('Please enter your full name.');
                return;
            }
            if (!email || !email.includes('@')) {
                showError('Please enter a valid email address.');
                return;
            }
            if (!termsRegCheckbox.checked) {
                showError('Please agree to the Terms and Privacy Policy.');
                return;
            }

            clearError();
            btnRegisterSubmit.classList.add('loading');

            try {
                const resp = await fetch('/api/auth/register', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name, email })
                });
                const data = await resp.json();
                btnRegisterSubmit.classList.remove('loading');

                if (data.status === 'success') {
                    activeEmail = email;
                    activeName = name;
                    transitionToWebOtp();
                } else {
                    showError(data.message || 'Registration failed.');
                }
            } catch (err) {
                btnRegisterSubmit.classList.remove('loading');
                showError('Server connection error. Please try again.');
            }
        });
    }

    function transitionToWebOtp() {
        if (paneLogin) paneLogin.style.display = 'none';
        if (paneRegister) paneRegister.style.display = 'none';
        if (paneOtp) paneOtp.style.display = 'block';
        document.getElementById('web-auth-tabs').style.display = 'none';
        clearError();

        if (otpHint) otpHint.textContent = `Enter the 6-digit code sent to ${activeEmail}`;
        otpBoxes.forEach(b => { if (b) b.value = ''; });
        if (otpBoxes[0]) otpBoxes[0].focus();
    }

    // OTP 6-box input handling
    otpBoxes.forEach((box, index) => {
        if (!box) return;
        box.addEventListener('input', (e) => {
            const val = e.target.value.replace(/[^0-9]/g, '');
            box.value = val ? val[0] : '';
            if (val && index < 5 && otpBoxes[index + 1]) {
                otpBoxes[index + 1].focus();
            }
            const fullCode = otpBoxes.map(b => b.value).join('');
            if (fullCode.length === 6) {
                btnOtpSubmit.click();
            }
        });

        box.addEventListener('keydown', (e) => {
            if (e.key === 'Backspace' && !box.value && index > 0 && otpBoxes[index - 1]) {
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
                btnOtpSubmit.click();
            }
        });
    });

    if (btnChangeEmail) {
        btnChangeEmail.addEventListener('click', () => {
            document.getElementById('web-auth-tabs').style.display = 'flex';
            if (paneOtp) paneOtp.style.display = 'none';
            if (paneLogin) paneLogin.style.display = 'block';
            clearError();
        });
    }

    if (btnResendOtp) {
        btnResendOtp.addEventListener('click', async () => {
            btnResendOtp.disabled = true;
            btnResendOtp.textContent = 'Sending...';
            try {
                const resp = await fetch('/api/auth/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email: activeEmail })
                });
                const data = await resp.json();
                btnResendOtp.disabled = false;
                btnResendOtp.textContent = 'Resend Code';
                if (data.status === 'success') {
                    showError('New verification code sent to your email.');
                }
            } catch {
                btnResendOtp.disabled = false;
                btnResendOtp.textContent = 'Resend Code';
            }
        });
    }

    // Verify OTP Submit
    if (btnOtpSubmit) {
        btnOtpSubmit.addEventListener('click', async () => {
            const otp = otpBoxes.map(b => b.value).join('');
            if (otp.length !== 6) {
                showError('Please enter all 6 digits.');
                return;
            }

            clearError();
            btnOtpSubmit.classList.add('loading');

            try {
                const resp = await fetch('/api/auth/verify-otp', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ email: activeEmail, otp: otp, name: activeName })
                });
                const data = await resp.json();
                btnOtpSubmit.classList.remove('loading');

                if (data.status === 'success') {
                    setTokens(data.tokens);
                    onAuthSuccess(data.user);
                } else {
                    const container = document.getElementById('web-otp-container');
                    if (container) {
                        container.classList.add('error-shake');
                        setTimeout(() => container.classList.remove('error-shake'), 450);
                    }
                    showError(data.message || 'Invalid or expired code.');
                    otpBoxes.forEach(b => { if (b) b.value = ''; });
                    if (otpBoxes[0]) otpBoxes[0].focus();
                }
            } catch (err) {
                btnOtpSubmit.classList.remove('loading');
                showError('Network error verifying code.');
            }
        });
    }

    // Logout
    if (btnLogout) {
        btnLogout.addEventListener('click', async () => {
            const ok = confirm('Log out of AIVA?');
            if (ok) {
                try {
                    await fetch('/api/auth/logout', { method: 'POST', headers: authHeaders() });
                } catch {}
                localStorage.clear();
                sessionStorage.clear();
                showWebAuthModal();
            }
        });
    }
}

// ---------------------------------------------------------------------
// 3. Tab Navigation
// ---------------------------------------------------------------------
function initNavigation() {
    const navButtons = document.querySelectorAll('.nav-item');
    const tabPanes = document.querySelectorAll('.tab-pane');

    navButtons.forEach(btn => {
        btn.addEventListener('click', () => {
            const targetTab = btn.getAttribute('data-tab');

            navButtons.forEach(b => b.classList.remove('active'));
            tabPanes.forEach(p => p.classList.remove('active'));

            btn.classList.add('active');
            const activePane = document.getElementById(targetTab);
            if (activePane) {
                activePane.classList.add('active');
            }

            if (targetTab === 'memory-view') {
                loadMemoryData();
            }
            if (targetTab === 'whatsapp-view') {
                loadWhatsAppStatus();
                loadWhatsAppContacts();
            }
        });
    });
}

// ---------------------------------------------------------------------
// 4. Chat & Command Handling
// ---------------------------------------------------------------------
function initChat() {
    const promptInput = document.getElementById('user-prompt-input');
    const sendBtn = document.getElementById('btn-send-prompt');
    const inspectBtn = document.getElementById('btn-quick-inspect');
    const screenshotBtn = document.getElementById('btn-quick-screenshot');
    const screenContextBtn = document.getElementById('btn-screen-context');

    if (sendBtn && promptInput) {
        sendBtn.addEventListener('click', () => {
            const text = promptInput.value.trim();
            if (text) {
                sendUserMessage(text);
                promptInput.value = '';
            }
        });

        promptInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                const text = promptInput.value.trim();
                if (text) {
                    sendUserMessage(text);
                    promptInput.value = '';
                }
            }
        });
    }

    if (inspectBtn) {
        inspectBtn.addEventListener('click', () => {
            triggerQuickCommand('what is currently on my screen?');
        });
    }

    if (screenshotBtn) {
        screenshotBtn.addEventListener('click', () => {
            triggerQuickCommand('take a screenshot');
        });
    }

    if (screenContextBtn) {
        screenContextBtn.addEventListener('click', () => {
            if (promptInput) {
                promptInput.value = 'Based on my screen, ' + promptInput.value;
                promptInput.focus();
            }
        });
    }
}

window.triggerQuickCommand = function(cmd) {
    const promptInput = document.getElementById('user-prompt-input');
    if (promptInput) {
        promptInput.value = cmd;
    }
    const navChat = document.getElementById('nav-chat');
    if (navChat) navChat.click();

    sendUserMessage(cmd);
    if (promptInput) promptInput.value = '';
};

async function sendUserMessage(query) {
    appendChatMessage('user', query);
    setNeuralCoreState('thinking');

    const tempMsgId = 'temp-' + Date.now();
    appendChatMessage('aiva', '<div class="typing-dots"><span></span><span></span><span></span></div>', tempMsgId);

    try {
        let response = await fetch('/api/chat', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({ message: query })
        });

        if (response.status === 401) {
            const refreshed = await refreshAccessToken();
            if (refreshed) {
                response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: authHeaders(),
                    body: JSON.stringify({ message: query })
                });
            }
        }

        if (response.status === 401) {
            removeTempMessage(tempMsgId);
            appendChatMessage('aiva', '🔒 Authentication required. Please log in to command AIVA.');
            setNeuralCoreState('idle');
            showWebAuthModal();
            return;
        }

        const data = await response.json();
        removeTempMessage(tempMsgId);

        if (data.status === 'success' || data.response) {
            const reply = data.response || "Command executed successfully.";
            appendChatMessage('aiva', reply, null, data.route);
            setNeuralCoreState('speaking');

            if (data.route) {
                updateWorkflowVisualizer(query, data.route, reply);
            }

            if (data.screenshot_base64) {
                updateVisionSnapshot(data.screenshot_base64);
            }

            // Play neural TTS audio output
            playVoiceOutput(reply, data.audio_base64);
        } else {
            appendChatMessage('aiva', `⚠️ Error: ${data.error || 'Failed to execute command.'}`);
            setNeuralCoreState('idle');
        }
    } catch (err) {
        console.error('Chat error:', err);
        removeTempMessage(tempMsgId);
        appendChatMessage('aiva', `⚠️ Network error: Could not reach AIVA server.`);
        setNeuralCoreState('idle');
    }
}

function renderMarkdown(text) {
    if (!text) return '';
    if (typeof text !== 'string') text = String(text);
    if (text.startsWith('<div class="typing-dots"')) return text;

    const isError = text.trim().startsWith('⚠️') || text.trim().startsWith('Error:');

    let cleanText = text
        .replace(/\s*---\s*/g, '\n\n---\n\n')
        .replace(/\s*(#{1,6}\s+)/g, '\n\n$1')
        .replace(/([.!?]\s+)(\d+\.\s+)/g, '$1\n$2')
        .replace(/(\]\([^\)]+\)\s*)(\d+\.\s+)/g, '$1\n$2');

    let escaped = cleanText
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');

    if (isError) {
        return `<div class="chat-error-msg"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg><span>${escaped.replace(/^⚠️\s*/, '')}</span></div>`;
    }

    // Fenced code blocks
    escaped = escaped.replace(/```(\w*)\n([\s\S]*?)```/g, (_m, _lang, code) => {
        return `<pre class="chat-code-block"><code>${code.trim()}</code></pre>`;
    });

    // Inline code
    escaped = escaped.replace(/`([^`]+)`/g, '<code class="chat-inline-code">$1</code>');

    // Horizontal rules (--- or ***)
    escaped = escaped.replace(/^(\s*[-*_]{3,}\s*)$/gm, '<hr class="chat-hr">');

    // Headers
    escaped = escaped.replace(/^#### (.*?)$/gm, '<h6 class="chat-h6">$1</h6>');
    escaped = escaped.replace(/^### (.*?)$/gm, '<h5 class="chat-h5">$1</h5>');
    escaped = escaped.replace(/^## (.*?)$/gm, '<h4 class="chat-h4">$1</h4>');
    escaped = escaped.replace(/^# (.*?)$/gm, '<h3 class="chat-h3">$1</h3>');

    // Bold + Italic
    escaped = escaped.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    // Bold
    escaped = escaped.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    // Italic
    escaped = escaped.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    escaped = escaped.replace(/_([^_]+)_/g, '<em>$1</em>');

    // Markdown Links: [title](url)
    escaped = escaped.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer" class="chat-link">$1 <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:middle;margin-left:2px"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg></a>');

    // Split paragraphs and format lists
    const paragraphs = escaped.split(/\n\s*\n/);
    const formatted = paragraphs.map(p => {
        p = p.trim();
        if (!p) return '';
        if (p.startsWith('<pre') || p.startsWith('<hr') || p.startsWith('<h')) {
            return p;
        }
        const lines = p.split('\n');
        let inList = false;
        let listHtml = '';
        const outLines = [];
        for (const line of lines) {
            const bulletMatch = line.match(/^\s*[-*]\s+(.*)$/);
            const numMatch = line.match(/^\s*(\d+)\.\s+(.*)$/);
            if (bulletMatch || numMatch) {
                const content = bulletMatch ? bulletMatch[1] : numMatch[2];
                if (!inList) {
                    inList = true;
                    listHtml = `<ul class="chat-ul"><li class="chat-list-item">${content}</li>`;
                } else {
                    listHtml += `<li class="chat-list-item">${content}</li>`;
                }
            } else {
                if (inList) {
                    listHtml += '</ul>';
                    outLines.push(listHtml);
                    inList = false;
                }
                outLines.push(line);
            }
        }
        if (inList) {
            listHtml += '</ul>';
            outLines.push(listHtml);
        }
        return `<div class="chat-p">${outLines.join('<br>')}</div>`;
    }).filter(Boolean).join('');

    return formatted || `<div class="chat-p">${escaped}</div>`;
}

function appendChatMessage(sender, text, customId = null, routeTag = null) {
    const container = document.getElementById('chat-messages');
    if (!container) return;

    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${sender === 'user' ? 'user-msg' : 'aiva-msg'}`;
    if (customId) msgDiv.id = customId;

    const now = new Date();
    const timeStr = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;

    const userInitial = document.getElementById('user-avatar-initials')?.textContent?.trim() || 'C';
    const avatarText = sender === 'user' ? userInitial : 'AI';
    const senderName = sender === 'user' ? 'You' : 'AIVA';

    let tagHtml = '';
    if (routeTag) {
        tagHtml = `<span class="tag-pill">${routeTag}</span>`;
    }

    const formattedContent = sender === 'user'
        ? `<div class="msg-text">${text.replace(/</g, '&lt;').replace(/>/g, '&gt;')}</div>`
        : `<div class="msg-text">${renderMarkdown(text)}</div>`;

    msgDiv.innerHTML = `
        <div class="msg-avatar">${avatarText}</div>
        <div class="msg-body">
            <div class="msg-header">
                <span class="msg-sender">${senderName} ${tagHtml}</span>
                <span class="msg-time">${timeStr}</span>
            </div>
            ${formattedContent}
        </div>
    `;

    container.appendChild(msgDiv);
    container.scrollTop = container.scrollHeight;
}

function removeTempMessage(id) {
    const el = document.getElementById(id);
    if (el) el.remove();
}

function setNeuralCoreState(state) {
    const core = document.getElementById('neural-core');
    if (!core) return;
    core.classList.remove('thinking', 'speaking', 'idle');
    core.classList.add(state);
}

// ---------------------------------------------------------------------
// 5. Enterprise Unified Voice Runtime & State Machine Integration
// ---------------------------------------------------------------------
let isVoiceMuted = false;

function playVoiceOutput(text, audioBase64) {
    if (isVoiceMuted) {
        setNeuralCoreState('idle');
        return;
    }

    if (window.AivaVoice && window.AivaVoice.sessionManager) {
        window.AivaVoice.sessionManager.playAudio(audioBase64, () => {
            setNeuralCoreState('idle');
        });
        return;
    }

    // Fallback if voice manager unavailable
    fallbackBrowserTts(text);
}

function fallbackBrowserTts(text) {
    if (!window.speechSynthesis || isVoiceMuted) {
        setTimeout(() => setNeuralCoreState('idle'), 2000);
        return;
    }
    try {
        window.speechSynthesis.cancel();
        const clean = (text || '')
            .replace(/[*_#`~\[\]>]+/g, '')
            .replace(/https?:\/\/\S+/g, 'link on screen')
            .replace(/\b(?:PDF|Video|Website|Blog)\b/g, '');
        const utterance = new SpeechSynthesisUtterance(clean);
        utterance.rate = 1.05;
        utterance.onend = () => setNeuralCoreState('idle');
        utterance.onerror = () => setNeuralCoreState('idle');
        window.speechSynthesis.speak(utterance);
    } catch {
        setTimeout(() => setNeuralCoreState('idle'), 2000);
    }
}

function stopVoiceOutput() {
    if (window.AivaVoice && window.AivaVoice.sessionManager) {
        window.AivaVoice.sessionManager.stopPlayback();
    }
    if (window.speechSynthesis) {
        try { window.speechSynthesis.cancel(); } catch {}
    }
    setNeuralCoreState('idle');
}

function initSpeechRecognition() {
    const micBtn = document.getElementById('btn-mic-input');
    const promptInput = document.getElementById('user-prompt-input');
    if (!micBtn) return;

    if (!window.AivaVoice) {
        console.warn('AivaVoice runtime not loaded');
        return;
    }

    const { stateMachine, sessionManager, VoiceState, VoiceErrorCode, VoiceErrorMessages } = window.AivaVoice;

    // React to state machine changes
    stateMachine.onStateChange((state, prevState, meta) => {
        if (state === VoiceState.LISTENING) {
            micBtn.classList.add('listening');
            if (promptInput) promptInput.placeholder = "Listening... Speak now";
            setNeuralCoreState('thinking');
        } else if (state === VoiceState.PROCESSING) {
            micBtn.classList.remove('listening');
            if (promptInput) promptInput.placeholder = "Processing speech with Whisper...";
            setNeuralCoreState('thinking');
        } else if (state === VoiceState.SPEAKING) {
            micBtn.classList.remove('listening');
            setNeuralCoreState('speaking');
        } else if (state === VoiceState.FOLLOW_UP) {
            micBtn.classList.add('listening');
            if (promptInput) promptInput.placeholder = "Listening for follow-up... (or click to cancel)";
            setNeuralCoreState('listening');
        } else if (state === VoiceState.ERROR) {
            micBtn.classList.remove('listening');
            const code = meta.code || (stateMachine.lastError && stateMachine.lastError.code);
            const msg = VoiceErrorMessages[code] || 'Voice operation failed.';
            showHudToast(msg, 'error');
            resetInputState();
        } else if (state === VoiceState.IDLE) {
            resetInputState();
        }
    });

    // Handle transcript result
    sessionManager.onTranscriptReady = (text) => {
        if (promptInput) promptInput.value = text;
        sendUserMessage(text);
        if (promptInput) promptInput.value = '';
    };

    // Click handler for mic button
    micBtn.addEventListener('click', async () => {
        const currentState = stateMachine.getState();
        if (currentState === VoiceState.LISTENING || currentState === VoiceState.FOLLOW_UP) {
            sessionManager.stopListening();
        } else if (currentState === VoiceState.SPEAKING) {
            // Barge-in: interrupt speech and start listening
            sessionManager.bargeIn();
        } else {
            // Start listening
            await sessionManager.startListening();
        }
    });

    function resetInputState() {
        micBtn.classList.remove('listening');
        if (promptInput) {
            promptInput.placeholder = "Type a command or ask anything...";
        }
        setNeuralCoreState('idle');
    }
}
// ---------------------------------------------------------------------
// 6. Voice Mode Toggles
// ---------------------------------------------------------------------
function initVoiceModeControls() {
    const btnAdaptive = document.getElementById('btn-mode-adaptive');
    const btnWake     = document.getElementById('btn-mode-wakeword');
    const btnAll      = document.getElementById('btn-mode-allvoice');
    const btnToggle   = document.getElementById('btn-toggle-voice');

    syncVoiceStatus();
    setInterval(syncVoiceStatus, 30000);

    function setActiveModeButton(activeBtn) {
        [btnAdaptive, btnWake, btnAll].forEach(b => {
            if (b) b.classList.remove('active');
        });
        if (activeBtn) activeBtn.classList.add('active');
    }

    if (btnAdaptive) {
        btnAdaptive.addEventListener('click', async () => {
            if (window.AivaVoice) window.AivaVoice.setMode('adaptive');
            setActiveModeButton(btnAdaptive);
            if (btnToggle) btnToggle.classList.remove('muted');
            await sendVoiceControl('start_adaptive');
            syncVoiceStatus();
        });
    }

    if (btnWake) {
        btnWake.addEventListener('click', async () => {
            if (window.AivaVoice) window.AivaVoice.setMode('wakeword');
            setActiveModeButton(btnWake);
            if (btnToggle) btnToggle.classList.remove('muted');
            await sendVoiceControl('start_wakeword');
            syncVoiceStatus();
        });
    }

    if (btnAll) {
        btnAll.addEventListener('click', async () => {
            if (window.AivaVoice) window.AivaVoice.setMode('allvoice');
            setActiveModeButton(btnAll);
            if (btnToggle) btnToggle.classList.remove('muted');
            await sendVoiceControl('start_allvoice');
            syncVoiceStatus();
        });
    }

    if (btnToggle) {
        btnToggle.addEventListener('click', async () => {
            const isMuted = btnToggle.classList.contains('muted');
            if (isMuted) {
                if (window.AivaVoice) window.AivaVoice.setMode('adaptive');
                btnToggle.classList.remove('muted');
                await sendVoiceControl('start_adaptive');
                syncVoiceStatus();
            } else {
                if (window.AivaVoice) window.AivaVoice.setMode('muted');
                btnToggle.classList.add('muted');
                const voiceBadge = document.getElementById('voice-indicator-badge');
                if (voiceBadge) {
                    voiceBadge.textContent = 'MUTED';
                    voiceBadge.className = 'badge-mini idle';
                }
                await sendVoiceControl('stop');
            }
        });
    }
}

async function sendVoiceControl(action) {
    try {
        const resp = await fetch('/api/voice/control', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({ action })
        });
        const data = await resp.json();
        return data.status !== 'error';
    } catch {
        return false;
    }
}

async function syncVoiceStatus() {
    try {
        const resp = await fetch('/api/voice/status');
        if (!resp.ok) return;
        const data = await resp.json();
        const voiceBadge = document.getElementById('voice-indicator-badge');
        const btnToggle  = document.getElementById('btn-toggle-voice');

        if (!data.running) {
            if (voiceBadge) { voiceBadge.textContent = 'MUTED'; voiceBadge.className = 'badge-mini idle'; }
            if (btnToggle) btnToggle.classList.add('muted');
            return;
        }

        if (btnToggle) btnToggle.classList.remove('muted');
        if (voiceBadge) {
            voiceBadge.textContent = (data.mode || 'ADAPTIVE').toUpperCase();
            voiceBadge.className = 'badge-mini active';
        }
    } catch {}
}

// ---------------------------------------------------------------------
// 7. Dynamic DAG Workflows Visualizer
// ---------------------------------------------------------------------
function updateWorkflowVisualizer(query, route, resultSummary) {
    const treeDisplay = document.getElementById('workflow-tree-display');
    if (!treeDisplay) return;

    treeDisplay.innerHTML = `
        <div class="workflow-node-card glass-card">
            <div class="node-badge success">INPUT PARSED</div>
            <h4>Input Query Pipeline</h4>
            <p>Prompt: <code>"${query}"</code></p>
        </div>
        <div class="workflow-connector"></div>
        <div class="workflow-node-card glass-card">
            <div class="node-badge success">RESOLVED ROUTE</div>
            <h4>Route: ${route.toUpperCase()}</h4>
            <p>Dispatcher: Kernel Router Engine</p>
        </div>
        <div class="workflow-connector"></div>
        <div class="workflow-node-card glass-card">
            <div class="node-badge running">COMPLETED</div>
            <h4>Execution Output</h4>
            <p>${resultSummary.slice(0, 120)}${resultSummary.length > 120 ? '...' : ''}</p>
        </div>
    `;
}

// ---------------------------------------------------------------------
// 8. Conversation History Loader
// ---------------------------------------------------------------------
async function loadConversationHistory() {
    try {
        const resp = await fetch('/api/history', { headers: authHeaders() });
        if (!resp.ok) return;
        const data = await resp.json();
        const turns = data.history || [];
        if (turns.length === 0) return;

        const container = document.getElementById('chat-messages');
        if (!container) return;

        container.innerHTML = '';
        turns.forEach(t => {
            appendChatMessage(t.role, t.message, null, t.route);
        });
    } catch {}
}

// ---------------------------------------------------------------------
// 9. Unified Memory, Entities, Reminders & Proactive HUD WebSocket
// ---------------------------------------------------------------------
let _memFilterType = '';
let _memSearchQuery = '';
let _memSearchTimer = null;
let _hudWs = null;
let _hudWsReconnectTimer = null;

function escapeHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function initMemoryControls() {
    const searchInput = document.getElementById('mem-search-input');
    const filterWrap = document.getElementById('mem-filter-pills');
    const exportBtn = document.getElementById('btn-mem-export');
    const forgetAllBtn = document.getElementById('btn-mem-forget-all');
    const toggleEnabled = document.getElementById('mem-toggle-enabled');
    const togglePaused = document.getElementById('mem-toggle-paused');
    const toggleBriefing = document.getElementById('mem-toggle-briefing');
    const quietStart = document.getElementById('mem-quiet-start');
    const quietEnd = document.getElementById('mem-quiet-end');
    const prefChannel = document.getElementById('mem-pref-channel');
    const addRemBtn = document.getElementById('btn-mem-add-rem');

    if (searchInput) {
        searchInput.addEventListener('input', () => {
            _memSearchQuery = searchInput.value.trim();
            clearTimeout(_memSearchTimer);
            _memSearchTimer = setTimeout(() => loadMemoryData(), 220);
        });
    }

    if (filterWrap) {
        filterWrap.querySelectorAll('.mem-pill').forEach(btn => {
            btn.addEventListener('click', () => {
                filterWrap.querySelectorAll('.mem-pill').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                _memFilterType = btn.getAttribute('data-type') || '';
                loadMemoryData();
            });
        });
    }

    async function patchMemorySettings(patch) {
        try {
            const resp = await fetch('/api/memory/settings', {
                method: 'PATCH',
                headers: authHeaders(),
                body: JSON.stringify(patch)
            });
            if (resp.ok) {
                showHudToast('Memory settings saved');
            }
        } catch (e) {
            console.warn('Settings save error:', e);
        }
    }

    if (toggleEnabled) {
        toggleEnabled.addEventListener('change', () => patchMemorySettings({ memory_enabled: toggleEnabled.checked }));
    }
    if (togglePaused) {
        togglePaused.addEventListener('change', () => patchMemorySettings({ learning_paused: togglePaused.checked }));
    }
    if (toggleBriefing) {
        toggleBriefing.addEventListener('change', () => patchMemorySettings({ morning_briefing_enabled: toggleBriefing.checked }));
    }
    if (quietStart) {
        quietStart.addEventListener('change', () => patchMemorySettings({ quiet_hours_start: quietStart.value.trim() || '23:00' }));
    }
    if (quietEnd) {
        quietEnd.addEventListener('change', () => patchMemorySettings({ quiet_hours_end: quietEnd.value.trim() || '07:00' }));
    }
    if (prefChannel) {
        prefChannel.addEventListener('change', () => patchMemorySettings({ preferred_reminder_channel: prefChannel.value }));
    }

    if (exportBtn) {
        exportBtn.addEventListener('click', async () => {
            try {
                const resp = await fetch('/api/memory/export', { headers: authHeaders() });
                if (!resp.ok) return;
                const blob = await resp.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = 'aiva-memory-export.json';
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);
                showHudToast('Memory export downloaded');
            } catch (e) {
                console.warn('Memory export error:', e);
            }
        });
    }

    if (forgetAllBtn) {
        forgetAllBtn.addEventListener('click', async () => {
            if (!confirm('Permanently erase ALL memories, entities, and reminders for your account? This cannot be undone.')) {
                return;
            }
            try {
                const resp = await fetch('/api/memory/forget-all', {
                    method: 'POST',
                    headers: authHeaders(),
                    body: JSON.stringify({ confirm: true })
                });
                if (resp.ok) {
                    showHudToast('All memories erased');
                    loadMemoryData();
                }
            } catch (e) {
                console.warn('Forget-all error:', e);
            }
        });
    }

    if (addRemBtn) {
        addRemBtn.addEventListener('click', async () => {
            const textEl = document.getElementById('mem-rem-text');
            const dueEl = document.getElementById('mem-rem-due');
            const text = (textEl?.value || '').trim();
            const due_at = (dueEl?.value || '').trim();
            if (!text || !due_at) {
                showHudToast('Enter reminder text and due time');
                return;
            }
            try {
                const resp = await fetch('/api/reminders', {
                    method: 'POST',
                    headers: authHeaders(),
                    body: JSON.stringify({
                        text,
                        due_at,
                        channel: prefChannel?.value || 'hud'
                    })
                });
                const data = await resp.json();
                if (resp.ok && data.status === 'success') {
                    if (textEl) textEl.value = '';
                    if (dueEl) dueEl.value = '';
                    showHudToast('Reminder scheduled');
                    loadMemoryData();
                } else {
                    showHudToast(data.message || data.error || 'Could not schedule reminder');
                }
            } catch (e) {
                console.warn('Create reminder error:', e);
            }
        });
    }
}

async function loadMemoryData() {
    try {
        const params = new URLSearchParams();
        if (_memFilterType) params.set('type', _memFilterType);
        if (_memSearchQuery) params.set('q', _memSearchQuery);
        const qs = params.toString() ? `?${params.toString()}` : '';

        const resp = await fetch(`/api/memory${qs}`, { headers: authHeaders() });
        if (!resp.ok) return;
        const data = await resp.json();

        // 0. Sync Settings Controls
        if (data.settings) {
            const s = data.settings;
            const toggleEnabled = document.getElementById('mem-toggle-enabled');
            const togglePaused = document.getElementById('mem-toggle-paused');
            const toggleBriefing = document.getElementById('mem-toggle-briefing');
            const quietStart = document.getElementById('mem-quiet-start');
            const quietEnd = document.getElementById('mem-quiet-end');
            const prefChannel = document.getElementById('mem-pref-channel');
            if (toggleEnabled) toggleEnabled.checked = s.memory_enabled !== false;
            if (togglePaused) togglePaused.checked = !!s.learning_paused;
            if (toggleBriefing) toggleBriefing.checked = !!s.morning_briefing_enabled;
            if (quietStart && s.quiet_hours_start) quietStart.value = s.quiet_hours_start;
            if (quietEnd && s.quiet_hours_end) quietEnd.value = s.quiet_hours_end;
            if (prefChannel && s.preferred_reminder_channel) prefChannel.value = s.preferred_reminder_channel;
        }

        // 1. Render Memories Grouped by Type
        const memories = data.memories || [];
        const grouped = data.grouped || {};
        const memContainer = document.getElementById('memories-grouped-list');
        const memCountEl = document.getElementById('mem-total-count');
        if (memCountEl) memCountEl.textContent = String(memories.length);

        if (memContainer) {
            memContainer.innerHTML = '';
            const typeOrder = ['semantic', 'procedural', 'episodic', 'prospective', 'knowledge', 'working'];
            const keys = typeOrder.filter(t => (grouped[t] || []).length > 0);
            Object.keys(grouped).forEach(k => { if (!keys.includes(k)) keys.push(k); });

            if (keys.length === 0) {
                memContainer.innerHTML = '<div class="mem-muted-label" style="padding:12px;">No stored memories match this view.</div>';
            } else {
                for (const mType of keys) {
                    const heading = document.createElement('div');
                    heading.className = 'mem-group-heading';
                    heading.textContent = `${mType} (${grouped[mType].length})`;
                    memContainer.appendChild(heading);

                    for (const item of grouped[mType]) {
                        const card = document.createElement('div');
                        card.className = 'memory-entry glass-card';
                        const impPct = Math.round((item.importance ?? 0.5) * 100);
                        card.innerHTML = `
                            <div class="mem-entry-top">
                                <div class="mem-meta-row">
                                    <span class="mem-badge mem-badge-accent">${escapeHtml(item.type)}</span>
                                    <span class="mem-badge">imp ${impPct}%</span>
                                    <span class="mem-badge">${escapeHtml(item.source || 'user_said')}</span>
                                    <span class="mem-badge">${escapeHtml(item.channel || 'hud')}</span>
                                </div>
                                <div class="mem-entry-actions">
                                    <button class="mem-icon-btn mem-btn-edit" data-id="${escapeHtml(item.id)}">Edit</button>
                                    <button class="mem-icon-btn danger mem-btn-del" data-id="${escapeHtml(item.id)}">Forget</button>
                                </div>
                            </div>
                            <p class="mem-content-text">${escapeHtml(item.content)}</p>
                        `;
                        memContainer.appendChild(card);
                    }
                }

                memContainer.querySelectorAll('.mem-btn-del').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const id = btn.getAttribute('data-id');
                        const res = await fetch(`/api/memory/${encodeURIComponent(id)}`, {
                            method: 'DELETE',
                            headers: authHeaders()
                        });
                        if (res.ok) {
                            showHudToast('Memory forgotten');
                            loadMemoryData();
                        }
                    });
                });

                memContainer.querySelectorAll('.mem-btn-edit').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const id = btn.getAttribute('data-id');
                        const item = memories.find(m => m.id === id);
                        if (!item) return;
                        const nextText = prompt('Edit memory content:', item.content);
                        if (nextText === null || !nextText.trim() || nextText.trim() === item.content) return;
                        const res = await fetch(`/api/memory/${encodeURIComponent(id)}`, {
                            method: 'PATCH',
                            headers: authHeaders(),
                            body: JSON.stringify({ content: nextText.trim() })
                        });
                        if (res.ok) {
                            showHudToast('Memory updated');
                            loadMemoryData();
                        }
                    });
                });
            }
        }

        // 2. Render Reminders
        const reminders = data.reminders || [];
        const remContainer = document.getElementById('reminders-list');
        const remCountEl = document.getElementById('mem-reminders-count');
        if (remCountEl) remCountEl.textContent = String(reminders.length);

        if (remContainer) {
            remContainer.innerHTML = '';
            if (reminders.length === 0) {
                remContainer.innerHTML = '<div class="mem-muted-label" style="padding:8px;">No reminders yet.</div>';
            } else {
                for (const r of reminders) {
                    const card = document.createElement('div');
                    card.className = 'memory-entry glass-card';
                    const dueStr = r.due_at ? new Date(r.due_at * 1000).toLocaleString() : '—';
                    const statusBadgeClass = r.status === 'pending' || r.status === 'snoozed'
                        ? 'mem-badge mem-badge-warn'
                        : 'mem-badge mem-badge-success';
                    card.innerHTML = `
                        <div class="mem-entry-top">
                            <div class="mem-meta-row">
                                <span class="${statusBadgeClass}">${escapeHtml(r.status)}</span>
                                <span class="mem-badge">${escapeHtml(r.channel || 'hud')}</span>
                                ${r.recurrence ? `<span class="mem-badge mem-badge-accent">${escapeHtml(r.recurrence)}</span>` : ''}
                            </div>
                            <div class="mem-entry-actions">
                                ${r.status !== 'done' && r.status !== 'cancelled' ? `<button class="mem-icon-btn mem-rem-snooze" data-id="${escapeHtml(r.id)}">+10m</button>` : ''}
                                ${r.status !== 'done' ? `<button class="mem-icon-btn mem-rem-done" data-id="${escapeHtml(r.id)}">Done</button>` : ''}
                                <button class="mem-icon-btn danger mem-rem-cancel" data-id="${escapeHtml(r.id)}">✕</button>
                            </div>
                        </div>
                        <strong>${escapeHtml(r.text)}</strong>
                        <p>Due: ${escapeHtml(dueStr)}</p>
                    `;
                    remContainer.appendChild(card);
                }

                remContainer.querySelectorAll('.mem-rem-snooze').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const id = btn.getAttribute('data-id');
                        const res = await fetch(`/api/reminders/${encodeURIComponent(id)}`, {
                            method: 'PATCH',
                            headers: authHeaders(),
                            body: JSON.stringify({ snooze_seconds: 600 })
                        });
                        if (res.ok) {
                            showHudToast('Reminder snoozed 10 min');
                            loadMemoryData();
                        }
                    });
                });

                remContainer.querySelectorAll('.mem-rem-done').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const id = btn.getAttribute('data-id');
                        const res = await fetch(`/api/reminders/${encodeURIComponent(id)}`, {
                            method: 'PATCH',
                            headers: authHeaders(),
                            body: JSON.stringify({ status: 'done' })
                        });
                        if (res.ok) {
                            showHudToast('Reminder marked done');
                            loadMemoryData();
                        }
                    });
                });

                remContainer.querySelectorAll('.mem-rem-cancel').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const id = btn.getAttribute('data-id');
                        const res = await fetch(`/api/reminders/${encodeURIComponent(id)}`, {
                            method: 'DELETE',
                            headers: authHeaders()
                        });
                        if (res.ok) {
                            showHudToast('Reminder cancelled');
                            loadMemoryData();
                        }
                    });
                });
            }
        }

        // 3. Render Entities & Linked WhatsApp Contacts
        const entities = data.entities || [];
        const entContainer = document.getElementById('entities-list');
        const entCountEl = document.getElementById('mem-entities-count');
        if (entCountEl) entCountEl.textContent = String(entities.length);

        if (entContainer) {
            entContainer.innerHTML = '';
            if (entities.length === 0) {
                entContainer.innerHTML = '<div class="mem-muted-label" style="padding:8px;">No entities tracked yet.</div>';
            } else {
                for (const ent of entities) {
                    const card = document.createElement('div');
                    card.className = 'memory-entry glass-card';
                    const aliases = (ent.aliases || []).join(', ');
                    const waBadge = ent.whatsapp_contact_id
                        ? '<span class="mem-badge mem-badge-success">WhatsApp Linked</span>'
                        : '';
                    card.innerHTML = `
                        <div class="mem-entry-top">
                            <strong>${escapeHtml(ent.name)}</strong>
                            <div class="mem-meta-row">
                                <span class="mem-badge mem-badge-accent">${escapeHtml(ent.kind)}</span>
                                ${waBadge}
                            </div>
                        </div>
                        ${aliases ? `<p>Aliases: ${escapeHtml(aliases)}</p>` : ''}
                    `;
                    entContainer.appendChild(card);
                }
            }
        }

        // 4. Render Learned Task Patterns
        const recipesContainer = document.getElementById('task-recipes-list');
        const patternCountEl = document.getElementById('stat-patterns-count');

        if (data.task_patterns && recipesContainer) {
            const patterns = data.task_patterns;
            const count = Object.keys(patterns).length;
            if (patternCountEl) patternCountEl.textContent = count;

            recipesContainer.innerHTML = '';
            for (const [patternName, details] of Object.entries(patterns)) {
                const card = document.createElement('div');
                card.className = 'memory-entry glass-card';
                const action = details.action || details.intent || 'Workflow';
                card.innerHTML = `
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <strong>${escapeHtml(patternName)}</strong>
                        <span class="cap-badge">${escapeHtml(action)}</span>
                    </div>
                    <small style="color:var(--text-muted);">${escapeHtml(JSON.stringify(details))}</small>
                `;
                recipesContainer.appendChild(card);
            }
        }
    } catch (e) {
        console.warn('Memory load error:', e);
    }
}

function connectHudWebSocket() {
    const token = getAccessToken();
    if (!token) return;
    if (_hudWs && (_hudWs.readyState === WebSocket.OPEN || _hudWs.readyState === WebSocket.CONNECTING)) {
        return;
    }
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${proto}//${window.location.host}/ws/hud?token=${encodeURIComponent(token)}`;
    try {
        const ws = new WebSocket(wsUrl);
        _hudWs = ws;

        ws.onmessage = (event) => {
            try {
                const payload = JSON.parse(event.data);
                if (payload.type === 'connected') return;
                if (payload.type === 'memory_notification' || payload.type === 'reminder' || payload.type === 'morning_briefing' || payload.type === 'workflow_result') {
                    const text = payload.text || 'Proactive update';
                    const speechText = payload.speech_text || text;
                    showHudToast(speechText.slice(0, 120));
                    const tag = payload.type === 'morning_briefing'
                        ? 'briefing'
                        : (payload.type === 'workflow_result' ? 'task' : 'reminder');
                    const sources = Array.isArray(payload.sources) && payload.sources.length ? payload.sources : null;
                    appendChatMessage('aiva', text, sources, tag);
                    if (payload.speak && !isVoiceMuted) {
                        fallbackBrowserTts(speechText);
                    }
                    const memPane = document.getElementById('memory-view');
                    if (memPane && memPane.classList.contains('active')) {
                        loadMemoryData();
                    }
                }
            } catch {}
        };

        ws.onclose = () => {
            _hudWs = null;
            clearTimeout(_hudWsReconnectTimer);
            if (getAccessToken()) {
                _hudWsReconnectTimer = setTimeout(() => connectHudWebSocket(), 8000);
            }
        };
    } catch (e) {
        console.warn('HUD WebSocket error:', e);
    }
}

function formatKey(k) {
    return k.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
}

function updateVisionSnapshot(base64Data) {
    const holder = document.getElementById('screen-preview-holder');
    if (holder) {
        holder.innerHTML = `<img src="data:image/jpeg;base64,${base64Data}" style="max-width:100%; max-height:100%; border-radius:8px; object-fit:contain;" alt="Desktop Snapshot">`;
    }
}

async function pollSystemStatus() {
    try {
        const resp = await fetch('/api/status');
        if (resp.ok) {
            const data = await resp.json();
            const apiLabel = document.getElementById('api-status-label');
            if (apiLabel && data.key_count) {
                apiLabel.textContent = `Pools: ${data.key_count} Keys Active`;
            }
        }
    } catch {}
}

// ---------------------------------------------------------------------
// 10. WhatsApp Channel, Pairing Mode & Contact Management
// ---------------------------------------------------------------------
let _waPairingMode = 'qr';
let _waSlowPollTimer = null;
let _waFastPollTimer = null;
let _waWasPairing = false;

function escapeWaHtml(str) {
    return String(str ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function startWhatsAppStatusPolling() {
    if (_waSlowPollTimer) return;
    _waSlowPollTimer = setInterval(() => {
        if (getAccessToken()) {
            loadWhatsAppStatus();
        }
    }, 15000);
}

function startFastPairingPoll() {
    stopFastPairingPoll();
    _waFastPollTimer = setInterval(() => {
        loadWhatsAppStatus();
    }, 1500);
}

function stopFastPairingPoll() {
    if (_waFastPollTimer) {
        clearInterval(_waFastPollTimer);
        _waFastPollTimer = null;
    }
}

function updatePairingModeUi(mode) {
    _waPairingMode = mode === 'code' ? 'code' : 'qr';
    const tabQr = document.getElementById('wa-pair-tab-qr');
    const tabCode = document.getElementById('wa-pair-tab-code');
    const phoneRow = document.getElementById('wa-pair-phone-row');
    const startBtn = document.getElementById('wa-btn-pair-start');

    if (tabQr) tabQr.classList.toggle('active', _waPairingMode === 'qr');
    if (tabCode) tabCode.classList.toggle('active', _waPairingMode === 'code');
    if (phoneRow) phoneRow.style.display = _waPairingMode === 'code' ? 'flex' : 'none';
    if (startBtn && !startBtn.disabled) {
        startBtn.textContent = _waPairingMode === 'code' ? 'Generate 8-Digit Code' : 'Start QR Pairing';
    }
}

function renderPairingState(pairing, isConnected, isOwner) {
    const phaseBadge = document.getElementById('wa-pair-phase-badge');
    const cancelBtn = document.getElementById('wa-btn-pair-cancel');
    const startBtn = document.getElementById('wa-btn-pair-start');
    const logoutBtn = document.getElementById('wa-btn-pair-logout');
    const ownerNote = document.getElementById('wa-pair-owner-note');
    const outputBox = document.getElementById('wa-pair-output');
    const statusMsg = document.getElementById('wa-pair-status-msg');
    const qrBox = document.getElementById('wa-pair-qr-box');
    const qrImg = document.getElementById('wa-pair-qr-img');
    const codeBox = document.getElementById('wa-pair-code-box');
    const codeText = document.getElementById('wa-pair-code-text');

    const p = pairing || {};
    const active = Boolean(p.active);
    const phase = p.phase || 'idle';

    if (ownerNote) {
        if (isOwner === false) {
            ownerNote.style.display = 'block';
            ownerNote.textContent = 'Only the primary AIVA account owner can pair or reset the bridge device. You can still link your personal number below.';
        } else {
            ownerNote.style.display = 'none';
        }
    }

    if (startBtn) startBtn.disabled = isOwner === false;
    if (logoutBtn) logoutBtn.disabled = isOwner === false;
    if (cancelBtn) {
        cancelBtn.style.display = active && isOwner !== false ? 'inline-flex' : 'none';
        cancelBtn.disabled = isOwner === false;
    }

    if (phaseBadge) {
        phaseBadge.className = 'wa-phase-badge';
        if (active) {
            phaseBadge.classList.add('active');
            phaseBadge.textContent = phase === 'restarting' ? 'Connecting' : 'Pairing Active';
        } else if (isConnected) {
            phaseBadge.classList.add('connected');
            phaseBadge.textContent = 'Paired';
        } else if (phase === 'error') {
            phaseBadge.classList.add('danger');
            phaseBadge.textContent = 'Error';
        } else {
            phaseBadge.textContent = 'Idle';
        }
    }

    if (startBtn && !startBtn.dataset.loading) {
        if (isConnected) {
            startBtn.textContent = _waPairingMode === 'code' ? 'Re-Pair via Code' : 'Re-Pair via QR';
        } else {
            startBtn.textContent = _waPairingMode === 'code' ? 'Generate 8-Digit Code' : 'Start QR Pairing';
        }
    }

    if (!outputBox) return;

    if (active) {
        outputBox.style.display = 'flex';
        startFastPairingPoll();
        _waWasPairing = true;

        if (phase === 'qr_ready' && p.qr_data_url) {
            if (qrBox) qrBox.style.display = 'flex';
            if (codeBox) codeBox.style.display = 'none';
            if (qrImg && qrImg.src !== p.qr_data_url) qrImg.src = p.qr_data_url;
            if (statusMsg) {
                statusMsg.className = 'wa-pair-status-msg';
                statusMsg.textContent = 'Waiting for QR scan (refreshes automatically if needed)...';
            }
        } else if (phase === 'code_ready' && p.pairing_code) {
            if (qrBox) qrBox.style.display = 'none';
            if (codeBox) codeBox.style.display = 'flex';
            if (codeText) codeText.textContent = p.pairing_code;
            if (statusMsg) {
                statusMsg.className = 'wa-pair-status-msg';
                statusMsg.textContent = `Enter this code in WhatsApp Mobile${p.target_phone_masked ? ' for ' + p.target_phone_masked : ''}.`;
            }
        } else if (phase === 'restarting') {
            if (qrBox) qrBox.style.display = 'none';
            if (codeBox) codeBox.style.display = 'none';
            if (statusMsg) {
                statusMsg.className = 'wa-pair-status-msg';
                statusMsg.textContent = 'Device accepted! Finishing secure WhatsApp Web handshake...';
            }
        } else {
            if (qrBox) qrBox.style.display = 'none';
            if (codeBox) codeBox.style.display = 'none';
            if (statusMsg) {
                statusMsg.className = 'wa-pair-status-msg';
                statusMsg.textContent = 'Initializing WhatsApp pairing socket...';
            }
        }
        return;
    }

    stopFastPairingPoll();

    if (_waWasPairing && isConnected) {
        _waWasPairing = false;
        showHudToast('WhatsApp bridge paired and connected!');
    } else if (!active && phase !== 'error') {
        _waWasPairing = false;
    }

    if (phase === 'error' && p.error) {
        outputBox.style.display = 'flex';
        if (qrBox) qrBox.style.display = 'none';
        if (codeBox) codeBox.style.display = 'none';
        if (statusMsg) {
            statusMsg.className = 'wa-pair-status-msg error';
            statusMsg.textContent = p.error;
        }
    } else {
        outputBox.style.display = 'none';
    }
}

function updateGlobalWhatsAppStatusUi(bridge) {
    const hudPill = document.getElementById('wa-hud-status-pill');
    const hudDot = document.getElementById('wa-hud-dot');
    const hudLabel = document.getElementById('wa-hud-label');
    const navDot = document.getElementById('wa-nav-dot');

    const status = bridge?.status || 'bridge_offline';
    const isConnected = Boolean(bridge?.connected);
    const isPairing = status === 'pairing' || Boolean(bridge?.pairing?.active);

    let stateClass = 'danger';
    let shortText = 'WA: Offline';

    if (isConnected) {
        stateClass = 'connected';
        shortText = 'WA: Connected';
    } else if (isPairing) {
        stateClass = 'pairing';
        shortText = 'WA: Pairing';
    } else if (status === 're_pair_required') {
        stateClass = 'danger';
        shortText = 'WA: Re-Pair';
    } else if (status === 'connecting' || status === 'disconnected') {
        stateClass = 'warning';
        shortText = 'WA: Connecting';
    }

    if (hudPill) {
        hudPill.className = `wa-hud-pill ${stateClass}`;
        hudPill.title = bridge?.error
            ? `WhatsApp (${status}): ${bridge.error}`
            : `WhatsApp: ${status}${bridge?.phone_masked ? ' (' + bridge.phone_masked + ')' : ''}`;
    }
    if (hudDot) hudDot.className = `wa-hud-dot ${stateClass}`;
    if (hudLabel) hudLabel.textContent = shortText;
    if (navDot) {
        navDot.className = `wa-nav-dot ${stateClass}`;
        navDot.title = shortText;
    }

    if (window.parent && window.parent !== window) {
        try {
            window.parent.postMessage({
                type: 'AIVA_WHATSAPP_STATUS',
                connected: isConnected,
                status,
                stateClass,
                shortText,
                phone_masked: bridge?.phone_masked || ''
            }, '*');
        } catch {}
    }
}

function initWhatsApp() {
    const hudPill = document.getElementById('wa-hud-status-pill');
    const refreshBtn = document.getElementById('wa-btn-refresh-status');
    const tabQr = document.getElementById('wa-pair-tab-qr');
    const tabCode = document.getElementById('wa-pair-tab-code');
    const pairStartBtn = document.getElementById('wa-btn-pair-start');
    const pairCancelBtn = document.getElementById('wa-btn-pair-cancel');
    const pairLogoutBtn = document.getElementById('wa-btn-pair-logout');
    const copyPairCodeBtn = document.getElementById('wa-btn-copy-pair-code');

    const startLinkBtn = document.getElementById('wa-btn-start-link');
    const phoneInput = document.getElementById('wa-link-phone-input');
    const addContactBtn = document.getElementById('wa-btn-add-contact');

    if (hudPill) {
        hudPill.addEventListener('click', () => {
            document.getElementById('nav-whatsapp')?.click();
        });
    }

    if (refreshBtn) {
        refreshBtn.addEventListener('click', () => loadWhatsAppStatus());
    }

    if (tabQr) {
        tabQr.addEventListener('click', () => updatePairingModeUi('qr'));
    }
    if (tabCode) {
        tabCode.addEventListener('click', () => updatePairingModeUi('code'));
    }

    if (pairStartBtn) {
        pairStartBtn.addEventListener('click', async () => {
            const pairPhone = document.getElementById('wa-pair-phone-input')?.value.trim() || '';
            if (_waPairingMode === 'code' && !pairPhone) {
                alert('Enter the WhatsApp phone number including country code (e.g. 919493867075).');
                return;
            }
            pairStartBtn.disabled = true;
            pairStartBtn.dataset.loading = '1';
            pairStartBtn.textContent = 'Starting Pairing...';
            try {
                const res = await fetch('/api/whatsapp/pair/start', {
                    method: 'POST',
                    headers: authHeaders(),
                    body: JSON.stringify({ mode: _waPairingMode, phone: pairPhone })
                });
                const data = await res.json();
                if (res.ok && data.status === 'success') {
                    renderPairingState(data.data?.pairing, false, true);
                    startFastPairingPoll();
                    loadWhatsAppStatus();
                } else {
                    alert(data.message || 'Failed to start WhatsApp pairing mode.');
                }
            } catch (err) {
                alert('Network error starting WhatsApp pairing.');
            } finally {
                delete pairStartBtn.dataset.loading;
                pairStartBtn.disabled = false;
                updatePairingModeUi(_waPairingMode);
            }
        });
    }

    if (pairCancelBtn) {
        pairCancelBtn.addEventListener('click', async () => {
            pairCancelBtn.disabled = true;
            try {
                await fetch('/api/whatsapp/pair/cancel', {
                    method: 'POST',
                    headers: authHeaders(),
                    body: JSON.stringify({})
                });
                stopFastPairingPoll();
                _waWasPairing = false;
                loadWhatsAppStatus();
            } catch {
                alert('Failed to cancel pairing.');
            } finally {
                pairCancelBtn.disabled = false;
            }
        });
    }

    if (pairLogoutBtn) {
        pairLogoutBtn.addEventListener('click', async () => {
            if (!confirm('Disconnect AIVA from the currently paired WhatsApp device and clear saved session keys?')) {
                return;
            }
            pairLogoutBtn.disabled = true;
            try {
                const res = await fetch('/api/whatsapp/pair/logout', {
                    method: 'POST',
                    headers: authHeaders(),
                    body: JSON.stringify({})
                });
                const data = await res.json();
                if (res.ok) {
                    showHudToast('WhatsApp session unpaired. Ready for fresh pairing.');
                    loadWhatsAppStatus();
                } else {
                    alert(data.message || 'Failed to unpair WhatsApp bridge.');
                }
            } catch {
                alert('Network error unpairing WhatsApp bridge.');
            } finally {
                pairLogoutBtn.disabled = false;
            }
        });
    }

    if (copyPairCodeBtn) {
        copyPairCodeBtn.addEventListener('click', async () => {
            const val = document.getElementById('wa-pair-code-text')?.textContent.trim();
            if (!val) return;
            try {
                await navigator.clipboard.writeText(val);
                showHudToast('Pairing code copied to clipboard');
            } catch {}
        });
    }

    if (startLinkBtn && phoneInput) {
        startLinkBtn.addEventListener('click', async () => {
            const phone = phoneInput.value.trim();
            if (!phone) {
                alert('Please enter a phone number with country code (e.g. 919876543210).');
                return;
            }
            startLinkBtn.disabled = true;
            startLinkBtn.textContent = 'Generating...';
            try {
                const res = await fetch('/api/whatsapp/link/start', {
                    method: 'POST',
                    headers: authHeaders(),
                    body: JSON.stringify({ phone })
                });
                const data = await res.json();
                if (res.ok && data.status === 'success') {
                    const codeDisplay = document.getElementById('wa-code-display');
                    const codeText = document.getElementById('wa-code-text');
                    if (codeDisplay && codeText) {
                        codeText.textContent = data.data.link_code;
                        codeDisplay.style.display = 'block';
                    }
                    loadWhatsAppStatus();
                } else {
                    alert(data.message || 'Failed to start linking.');
                }
            } catch (err) {
                alert('Network error while starting WhatsApp link.');
            } finally {
                startLinkBtn.disabled = false;
                startLinkBtn.textContent = 'Generate Code';
            }
        });
    }

    if (addContactBtn) {
        addContactBtn.addEventListener('click', async () => {
            const name = document.getElementById('wa-new-name')?.value.trim();
            const phone = document.getElementById('wa-new-phone')?.value.trim();
            const aliasesRaw = document.getElementById('wa-new-aliases')?.value.trim() || '';
            const trusted = document.getElementById('wa-new-trusted')?.checked ?? true;

            if (!name || !phone) {
                alert('Please provide both contact name and phone number.');
                return;
            }

            const aliases = aliasesRaw.split(',').map(s => s.trim()).filter(Boolean);

            addContactBtn.disabled = true;
            try {
                const res = await fetch('/api/whatsapp/contacts', {
                    method: 'POST',
                    headers: authHeaders(),
                    body: JSON.stringify({ display_name: name, phone, aliases, trusted })
                });
                const data = await res.json();
                if (res.ok && data.status === 'success') {
                    if (document.getElementById('wa-new-name')) document.getElementById('wa-new-name').value = '';
                    if (document.getElementById('wa-new-phone')) document.getElementById('wa-new-phone').value = '';
                    if (document.getElementById('wa-new-aliases')) document.getElementById('wa-new-aliases').value = '';
                    loadWhatsAppContacts();
                } else {
                    alert(data.message || 'Failed to add contact.');
                }
            } catch (err) {
                alert('Error adding contact.');
            } finally {
                addContactBtn.disabled = false;
            }
        });
    }
}

async function loadWhatsAppStatus() {
    const badge = document.getElementById('wa-bridge-badge');
    const details = document.getElementById('wa-status-details');
    const activeLinksContainer = document.getElementById('wa-active-links-list');

    try {
        const res = await fetch('/api/whatsapp/status', { headers: authHeaders() });
        if (!res.ok) return;
        const respData = await res.json();
        const data = respData.data || {};
        const bridge = data.bridge || {};
        const pairing = bridge.pairing || {};
        const isConnected = Boolean(bridge.connected);

        updateGlobalWhatsAppStatusUi(bridge);
        renderPairingState(pairing, isConnected, data.is_owner);

        if (badge) {
            if (isConnected) {
                badge.textContent = 'Connected (Baileys)';
                badge.style.background = 'var(--status-success-bg)';
                badge.style.color = 'var(--success)';
            } else if (bridge.status === 'pairing' || pairing.active) {
                badge.textContent = 'Pairing Mode Active';
                badge.style.background = 'var(--status-info-bg)';
                badge.style.color = 'var(--info)';
            } else if (bridge.status === 're_pair_required') {
                badge.textContent = 'Re-Pair Required';
                badge.style.background = 'var(--status-danger-bg)';
                badge.style.color = 'var(--danger)';
            } else if (bridge.status === 'bridge_offline') {
                badge.textContent = 'Bridge Offline';
                badge.style.background = 'var(--status-danger-bg)';
                badge.style.color = 'var(--danger)';
            } else {
                badge.textContent = 'Connecting / Disconnected';
                badge.style.background = 'var(--status-warning-bg)';
                badge.style.color = 'var(--warning)';
            }
        }

        if (details) {
            const pairedPhone = bridge.phone_masked || bridge.configured_phone_masked || 'Not Paired';
            const errorHtml = (!isConnected && bridge.error)
                ? `<div class="wa-error-alert">${escapeWaHtml(bridge.error)}</div>`
                : '';
            details.innerHTML = `
                <div><strong>Bridge Status:</strong> ${escapeWaHtml(bridge.status || 'offline')}</div>
                <div><strong>AIVA Device Phone:</strong> ${escapeWaHtml(pairedPhone)}</div>
                <div><strong>Authorized Phone Links:</strong> ${data.links?.length || 0} active</div>
                ${errorHtml}
            `;
        }

        if (activeLinksContainer) {
            activeLinksContainer.innerHTML = '';
            const links = data.links || [];
            if (links.length > 0) {
                const header = document.createElement('div');
                header.style.fontSize = '0.8rem';
                header.style.fontWeight = '600';
                header.style.color = 'var(--text-2)';
                header.textContent = 'Linked Numbers:';
                activeLinksContainer.appendChild(header);

                links.forEach(l => {
                    const row = document.createElement('div');
                    row.style.display = 'flex';
                    row.style.justifyContent = 'space-between';
                    row.style.alignItems = 'center';
                    row.style.padding = '6px 10px';
                    row.style.background = 'var(--elevated)';
                    row.style.borderRadius = 'var(--radius-sm)';
                    row.style.fontSize = '0.8rem';
                    row.style.border = '1px solid var(--border)';

                    const isVer = l.status === 'verified';
                    const statusPill = isVer
                        ? `<span style="color:var(--success); margin-left:6px;">● Verified</span>`
                        : `<span style="color:var(--warning); margin-left:6px;">● Pending (${escapeWaHtml(l.link_code || '')})</span>`;
                    const phoneDisplay = l.phone_masked || (l.phone_e164 ? `+${l.phone_e164}` : '');

                    row.innerHTML = `
                        <div>
                            <strong>${escapeWaHtml(phoneDisplay)}</strong>
                            ${statusPill}
                        </div>
                        <button class="wa-btn-unlink" data-phone="${escapeWaHtml(l.phone_e164)}" style="background:transparent; border:1px solid var(--border); color:var(--text-2); padding:3px 8px; border-radius:4px; font-size:0.75rem; cursor:pointer;">Unlink</button>
                    `;
                    activeLinksContainer.appendChild(row);
                });

                activeLinksContainer.querySelectorAll('.wa-btn-unlink').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const phone = btn.getAttribute('data-phone');
                        if (confirm('Unlink this WhatsApp number from AIVA?')) {
                            await fetch('/api/whatsapp/link/unlink', {
                                method: 'POST',
                                headers: authHeaders(),
                                body: JSON.stringify({ phone })
                            });
                            loadWhatsAppStatus();
                        }
                    });
                });
            }
        }
    } catch (e) {
        console.warn('Error loading WhatsApp status:', e);
    }
}

async function loadWhatsAppContacts() {
    const list = document.getElementById('wa-contacts-list');
    if (!list) return;

    try {
        const res = await fetch('/api/whatsapp/contacts', { headers: authHeaders() });
        if (!res.ok) return;
        const data = await res.json();
        const contacts = data.contacts || [];

        list.innerHTML = '';
        if (contacts.length === 0) {
            list.innerHTML = `<div style="font-size:0.8rem; color:var(--text-muted); text-align:center; padding:12px;">No contacts saved yet. Add one above or via WhatsApp chat.</div>`;
            return;
        }

        contacts.forEach(c => {
            const item = document.createElement('div');
            item.style.display = 'flex';
            item.style.justifyContent = 'space-between';
            item.style.alignItems = 'center';
            item.style.padding = '8px 12px';
            item.style.background = 'var(--elevated)';
            item.style.borderRadius = 'var(--radius-sm)';
            item.style.border = '1px solid var(--border)';
            item.style.fontSize = '0.8rem';

            const aliasesHtml = (c.aliases || []).map(a =>
                `<span style="background:var(--accent-subtle); color:var(--accent-light); padding:1px 6px; border-radius:4px; font-size:0.7rem; margin-right:4px;">${escapeWaHtml(a)}</span>`
            ).join('');

            item.innerHTML = `
                <div style="display:flex; flex-direction:column; gap:2px;">
                    <div><strong>${escapeWaHtml(c.display_name)}</strong> <span style="color:var(--text-2); margin-left:6px;">+${escapeWaHtml(c.phone_e164)}</span></div>
                    <div style="margin-top:2px;">${aliasesHtml}</div>
                </div>
                <div style="display:flex; align-items:center; gap:12px;">
                    <label style="display:flex; align-items:center; gap:4px; cursor:pointer; font-size:0.75rem; color:var(--text-2);" title="Send messages without confirmation prompt">
                        <input type="checkbox" class="wa-toggle-trusted" data-id="${escapeWaHtml(c.id)}" ${c.trusted ? 'checked' : ''}>
                        <span>Trusted</span>
                    </label>
                    <button class="wa-btn-del-contact" data-id="${escapeWaHtml(c.id)}" style="background:transparent; border:none; color:var(--text-muted); cursor:pointer; font-size:0.9rem;" title="Delete Contact">✕</button>
                </div>
            `;

            list.appendChild(item);
        });

        list.querySelectorAll('.wa-toggle-trusted').forEach(chk => {
            chk.addEventListener('change', async () => {
                const id = chk.getAttribute('data-id');
                const trusted = chk.checked;
                await fetch(`/api/whatsapp/contacts/${id}`, {
                    method: 'PUT',
                    headers: authHeaders(),
                    body: JSON.stringify({ trusted })
                });
            });
        });

        list.querySelectorAll('.wa-btn-del-contact').forEach(btn => {
            btn.addEventListener('click', async () => {
                const id = btn.getAttribute('data-id');
                if (confirm('Delete this WhatsApp contact?')) {
                    await fetch(`/api/whatsapp/contacts/${id}`, {
                        method: 'DELETE',
                        headers: authHeaders()
                    });
                    loadWhatsAppContacts();
                }
            });
        });

    } catch (e) {
        console.warn('Error loading WhatsApp contacts:', e);
    }
}
