// =====================================================================
// AIVA HUD — JAVASCRIPT CLIENT CONTROLLER
// =====================================================================

document.addEventListener('DOMContentLoaded', () => {
    initClock();
    initNavigation();
    initChat();
    initSpeechRecognition();
    initVoiceModeControls();
    loadMemoryData();
    pollSystemStatus();
});

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
// 2. Tab Navigation
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
// 3. Chat & Command Handling
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
    // Switch to chat view if not already there
    const navChat = document.getElementById('nav-chat');
    if (navChat) navChat.click();

    sendUserMessage(cmd);
    if (promptInput) promptInput.value = '';
};

async function sendUserMessage(query) {
    appendChatMessage('user', query);
    setNeuralCoreState('thinking');

    // Create a temporary "AIVA is processing..." bubble
    const tempMsgId = 'temp-' + Date.now();
    appendChatMessage('aiva', '<div class="typing-dots"><span></span><span></span><span></span></div>', tempMsgId);

    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message: query })
        });

        const data = await response.json();
        removeTempMessage(tempMsgId);

        if (data.status === 'success' || data.response) {
            const reply = data.response || "Command executed successfully.";
            appendChatMessage('aiva', reply, null, data.route);
            setNeuralCoreState('speaking');

            // Render DAG node if workflow info was returned
            if (data.route) {
                updateWorkflowVisualizer(query, data.route, reply);
            }

            // If a screen inspection or image was involved, update vision preview
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
        appendChatMessage('aiva', `⚠️ Network error: Could not reach AIVA server. Check if server is running.`);
        setNeuralCoreState('idle');
    }
}

function appendChatMessage(sender, text, customId = null, routeTag = null) {
    const container = document.getElementById('chat-messages');
    if (!container) return;

    const msgDiv = document.createElement('div');
    msgDiv.className = `message ${sender === 'user' ? 'user-msg' : 'aiva-msg'} glass-card`;
    if (customId) msgDiv.id = customId;

    const now = new Date();
    const timeStr = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;

    const avatarText = sender === 'user' ? 'U' : 'AI';
    const senderName = sender === 'user' ? 'You' : 'AIVA Intelligence';

    let tagHtml = '';
    if (routeTag) {
        tagHtml = `<span class="tag-pill" style="font-size:0.65rem; margin-left:8px;">${routeTag}</span>`;
    }

    msgDiv.innerHTML = `
        <div class="msg-avatar">${avatarText}</div>
        <div class="msg-body">
            <div class="msg-header">
                <span class="msg-sender">${senderName} ${tagHtml}</span>
                <span class="msg-time">${timeStr}</span>
            </div>
            <div class="msg-text">${text}</div>
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
// 4. Web Speech API (In-Browser Mic)
// ---------------------------------------------------------------------
function initSpeechRecognition() {
    const micBtn = document.getElementById('btn-mic-input');
    const promptInput = document.getElementById('user-prompt-input');

    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
        if (micBtn) {
            micBtn.title = "Speech recognition not supported in this browser.";
        }
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

        if (promptInput) {
            promptInput.value = transcript;
        }

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
// 5. Voice Mode Toggles — wired to /api/voice/control backend
// ---------------------------------------------------------------------
function initVoiceModeControls() {
    const btnAdaptive = document.getElementById('btn-mode-adaptive');
    const btnWake     = document.getElementById('btn-mode-wakeword');
    const btnAll      = document.getElementById('btn-mode-allvoice');
    const btnToggle   = document.getElementById('btn-toggle-voice');

    // Fetch once on load, then only on button interactions + slow 30s heartbeat
    // (no aggressive 2s polling — that spams the server with requests)
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
                const label = document.getElementById('voice-btn-label');
                if (label) label.textContent = 'Voice Active';
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
                const label = document.getElementById('voice-btn-label');
                if (label) label.textContent = 'Voice Active';
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
                const label = document.getElementById('voice-btn-label');
                if (label) label.textContent = 'Voice Active';
                syncVoiceStatus();
            }
        });
    }

    if (btnToggle) {
        btnToggle.addEventListener('click', async () => {
            const isMuted = btnToggle.classList.contains('muted');
            const label = document.getElementById('voice-btn-label');

            if (isMuted) {
                let targetAction = 'start_adaptive';
                if (btnWake && btnWake.classList.contains('active')) targetAction = 'start_wakeword';
                if (btnAll && btnAll.classList.contains('active')) targetAction = 'start_allvoice';

                const ok = await sendVoiceControl(targetAction);
                if (ok) {
                    btnToggle.classList.remove('muted');
                    if (label) label.textContent = 'Voice Active';
                    syncVoiceStatus();
                }
            } else {
                const ok = await sendVoiceControl('stop');
                if (ok) {
                    btnToggle.classList.add('muted');
                    if (label) label.textContent = 'Voice Muted';
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
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action })
        });
        const data = await resp.json();
        if (data.status === 'error') {
            console.warn('[VoiceControl] Server error:', data.message);
            return false;
        }
        console.log(`[VoiceControl] action=${action} → mode=${data.mode}`);
        return true;
    } catch (e) {
        console.warn('[VoiceControl] Network error:', e);
        return false;
    }
}

async function syncVoiceStatus() {
    try {
        const resp = await fetch('/api/voice/status');
        if (!resp.ok) return;
        const data = await resp.json();
        const voiceBadge  = document.getElementById('voice-indicator-badge');
        const btnToggle   = document.getElementById('btn-toggle-voice');
        const btnAdaptive = document.getElementById('btn-mode-adaptive');
        const btnWake     = document.getElementById('btn-mode-wakeword');
        const btnAll      = document.getElementById('btn-mode-allvoice');
        const label       = document.getElementById('voice-btn-label');

        if (!data.running) {
            if (voiceBadge) { voiceBadge.textContent = 'MUTED'; voiceBadge.className = 'badge-mini idle'; }
            if (btnToggle) btnToggle.classList.add('muted');
            if (label) label.textContent = 'Voice Muted';
            return;
        }

        if (btnToggle) btnToggle.classList.remove('muted');
        if (label) label.textContent = 'Voice Active';

        // Update active mode button styling
        if (data.mode === 'allvoice') {
            if (btnAll) btnAll.classList.add('active');
            if (btnWake) btnWake.classList.remove('active');
            if (btnAdaptive) btnAdaptive.classList.remove('active');
        } else if (data.mode === 'wakeword') {
            if (btnWake) btnWake.classList.add('active');
            if (btnAll) btnAll.classList.remove('active');
            if (btnAdaptive) btnAdaptive.classList.remove('active');
        } else {
            if (btnAdaptive) btnAdaptive.classList.add('active');
            if (btnWake) btnWake.classList.remove('active');
            if (btnAll) btnAll.classList.remove('active');
        }

        // Update live runtime state badge
        if (!voiceBadge) return;

        const state = data.state || 'standby';
        if (state === 'follow_up') {
            voiceBadge.textContent = 'FOLLOW-UP';
            voiceBadge.className = 'badge-mini followup';
            setNeuralCoreState('speaking');
        } else if (state === 'listening') {
            voiceBadge.textContent = 'LISTENING';
            voiceBadge.className = 'badge-mini listening';
            setNeuralCoreState('thinking');
        } else if (state === 'processing') {
            voiceBadge.textContent = 'THINKING';
            voiceBadge.className = 'badge-mini thinking';
            setNeuralCoreState('thinking');
        } else if (state === 'speaking') {
            voiceBadge.textContent = 'SPEAKING';
            voiceBadge.className = 'badge-mini active';
            setNeuralCoreState('speaking');
        } else {
            // Standby
            voiceBadge.textContent = (data.mode || 'ADAPTIVE').toUpperCase();
            voiceBadge.className = 'badge-mini active';
        }
    } catch (e) {
        // quiet — server may not be running in dev mode
    }
}


// ---------------------------------------------------------------------
// 6. Dynamic DAG Workflows Visualizer
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
// 7. Memory & Patterns Loader
// ---------------------------------------------------------------------
async function loadMemoryData() {
    try {
        const resp = await fetch('/api/memory');
        if (!resp.ok) return;
        const data = await resp.json();

        // 1. Render User Memory
        const userFactsContainer = document.getElementById('user-facts-list');
        const userNameDisplay = document.getElementById('user-display-name');
        const factNameVal = document.getElementById('fact-name-val');

        if (data.user_memory) {
            const u = data.user_memory;
            if (u.name) {
                if (userNameDisplay) userNameDisplay.textContent = u.name;
                if (factNameVal) factNameVal.textContent = u.name;
            }

            if (userFactsContainer) {
                userFactsContainer.innerHTML = '';
                for (const [k, v] of Object.entries(u)) {
                    const card = document.createElement('div');
                    card.className = 'memory-entry glass-card';
                    card.innerHTML = `<strong>${formatKey(k)}</strong><p>${JSON.stringify(v)}</p>`;
                    userFactsContainer.appendChild(card);
                }
            }
        }

        // 2. Render Task Patterns (Recipes)
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

// ---------------------------------------------------------------------
// 8. Vision Snapshot Preview
// ---------------------------------------------------------------------
function updateVisionSnapshot(base64Data) {
    const holder = document.getElementById('screen-preview-holder');
    if (holder) {
        holder.innerHTML = `<img src="data:image/jpeg;base64,${base64Data}" style="max-width:100%; max-height:100%; border-radius:8px; object-fit:contain;" alt="Desktop Snapshot">`;
    }
}

// ---------------------------------------------------------------------
// 9. System Status Polling
// ---------------------------------------------------------------------
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
    } catch (e) {
        // quiet
    }
}
