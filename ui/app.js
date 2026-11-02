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
}

window.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'AIVA_AUTH_SESSION' && event.data.tokens) {
        setTokens(event.data.tokens);
        checkAuthStatus();
    }
});

function getAccessToken() {
    return localStorage.getItem('aiva_access_token') || '';
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
}

function showWebAuthModal() {
    const overlay = document.getElementById('web-auth-overlay');
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
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({ message: query })
        });

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

            setTimeout(() => setNeuralCoreState('idle'), 3500);
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
// 5. Web Speech API (In-Browser Mic)
// ---------------------------------------------------------------------
function initSpeechRecognition() {
    const micBtn = document.getElementById('btn-mic-input');
    const promptInput = document.getElementById('user-prompt-input');

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
        if (micBtn) micBtn.title = "Speech recognition not supported in this browser.";
        return;
    }

    const recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = true;
    recognition.lang = 'en-US';

    let isListening = false;

    if (micBtn) {
        micBtn.addEventListener('click', () => {
            if (!isListening) {
                try {
                    recognition.start();
                    micBtn.classList.add('listening');
                    isListening = true;
                } catch (e) {
                    console.warn(e);
                }
            } else {
                recognition.stop();
                micBtn.classList.remove('listening');
                isListening = false;
            }
        });
    }

    recognition.onresult = (event) => {
        const transcript = Array.from(event.results)
            .map(result => result[0])
            .map(result => result.transcript)
            .join('');

        if (promptInput) promptInput.value = transcript;

        if (event.results[0].isFinal) {
            micBtn.classList.remove('listening');
            isListening = false;
            if (transcript.trim()) {
                sendUserMessage(transcript.trim());
                if (promptInput) promptInput.value = '';
            }
        }
    };

    recognition.onerror = () => {
        micBtn.classList.remove('listening');
        isListening = false;
    };

    recognition.onend = () => {
        micBtn.classList.remove('listening');
        isListening = false;
    };
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
            const ok = await sendVoiceControl('start_adaptive');
            if (ok) {
                setActiveModeButton(btnAdaptive);
                if (btnToggle) btnToggle.classList.remove('muted');
                syncVoiceStatus();
            }
        });
    }

    if (btnWake) {
        btnWake.addEventListener('click', async () => {
            const ok = await sendVoiceControl('start_wakeword');
            if (ok) {
                setActiveModeButton(btnWake);
                if (btnToggle) btnToggle.classList.remove('muted');
                syncVoiceStatus();
            }
        });
    }

    if (btnAll) {
        btnAll.addEventListener('click', async () => {
            const ok = await sendVoiceControl('start_allvoice');
            if (ok) {
                setActiveModeButton(btnAll);
                if (btnToggle) btnToggle.classList.remove('muted');
                syncVoiceStatus();
            }
        });
    }

    if (btnToggle) {
        btnToggle.addEventListener('click', async () => {
            const isMuted = btnToggle.classList.contains('muted');
            if (isMuted) {
                const ok = await sendVoiceControl('start_adaptive');
                if (ok) {
                    btnToggle.classList.remove('muted');
                    syncVoiceStatus();
                }
            } else {
                const ok = await sendVoiceControl('stop');
                if (ok) {
                    btnToggle.classList.add('muted');
                    const voiceBadge = document.getElementById('voice-indicator-badge');
                    if (voiceBadge) {
                        voiceBadge.textContent = 'MUTED';
                        voiceBadge.className = 'badge-mini idle';
                    }
                }
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
// 9. Memory & Patterns Loader (Per Authenticated User)
// ---------------------------------------------------------------------
async function loadMemoryData() {
    try {
        const resp = await fetch('/api/memory', { headers: authHeaders() });
        if (!resp.ok) return;
        const data = await resp.json();

        // 1. Render User Memory
        const userFactsContainer = document.getElementById('user-facts-list');
        const userNameDisplay = document.getElementById('user-display-name');

        if (data.user_memory) {
            const u = data.user_memory;
            if (u.name && userNameDisplay) {
                userNameDisplay.textContent = u.name;
            }

            if (userFactsContainer) {
                userFactsContainer.innerHTML = '';
                const entries = Object.entries(u);
                if (entries.length === 0) {
                    userFactsContainer.innerHTML = '<div style="color:var(--text-tertiary); font-size:12px; padding:12px;">No stored facts for this account.</div>';
                } else {
                    for (const [k, v] of entries) {
                        const card = document.createElement('div');
                        card.className = 'memory-entry glass-card';
                        card.innerHTML = `<strong>${formatKey(k)}</strong><p>${JSON.stringify(v)}</p>`;
                        userFactsContainer.appendChild(card);
                    }
                }
            }
        }

        // 2. Render Task Patterns
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
                        <strong>${patternName}</strong>
                        <span class="cap-badge">${action}</span>
                    </div>
                    <small style="color:var(--text-muted);">${JSON.stringify(details)}</small>
                `;
                recipesContainer.appendChild(card);
            }
        }
    } catch (e) {
        console.warn('Memory load error:', e);
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
