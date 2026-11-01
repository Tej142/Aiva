const WebSocket = require('ws');

class DeviceWebSocketClient {
  constructor(secureStore, executor, onStatusChange, onActivityLog) {
    this.store = secureStore;
    this.executor = executor;
    this.onStatusChange = onStatusChange || (() => {});
    this.onActivityLog = onActivityLog || (() => {});

    this.ws = null;
    this.heartbeatTimer = null;
    this.reconnectTimer = null;
    this.backoffMs = 1000;
    this.maxBackoffMs = 30000;
    this.isManualDisconnect = false;
  }

  connect() {
    this.isManualDisconnect = false;
    this._clearTimers();

    const serverUrl = this.store.get('serverUrl', 'http://localhost:5000');
    const deviceToken = this.store.getDecrypted('encryptedDeviceToken');

    if (!deviceToken) {
      this.onStatusChange('unregistered', 'No device token registered. Please log in.');
      return;
    }

    // Convert http(s) to ws(s)
    let wsUrl = serverUrl.replace(/^http:\/\//i, 'ws://').replace(/^https:\/\//i, 'wss://');
    wsUrl = wsUrl.replace(/\/+$/, '') + `/ws/device?token=${encodeURIComponent(deviceToken)}`;

    this.onStatusChange('connecting', `Connecting to ${serverUrl}...`);

    try {
      this.ws = new WebSocket(wsUrl);

      this.ws.on('open', () => {
        this.backoffMs = 1000; // Reset backoff
        this.onStatusChange('connected', 'Connected to AIVA Cloud Engine.');
        this._startHeartbeat();
      });

      this.ws.on('message', async (data) => {
        try {
          const msg = JSON.parse(data.toString());
          await this._handleMessage(msg);
        } catch (err) {
          console.error('[DeviceWS] Error parsing message:', err);
        }
      });

      this.ws.on('close', (code, reason) => {
        this._clearTimers();
        const reasonStr = reason ? reason.toString() : '';
        if (code === 1008 || reasonStr.toLowerCase().includes('unauthorized') || reasonStr.toLowerCase().includes('revoked')) {
          this.onStatusChange('revoked', 'Device token revoked or unauthorized.');
          return;
        }

        if (!this.isManualDisconnect) {
          this.onStatusChange('reconnecting', `Connection lost. Reconnecting in ${Math.round(this.backoffMs / 1000)}s...`);
          this._scheduleReconnect();
        } else {
          this.onStatusChange('disconnected', 'Disconnected.');
        }
      });

      this.ws.on('error', (err) => {
        console.error('[DeviceWS] Socket error:', err.message);
        this.onStatusChange('error', `Connection error: ${err.message}`);
      });

    } catch (err) {
      console.error('[DeviceWS] Connection exception:', err);
      this._scheduleReconnect();
    }
  }

  disconnect() {
    this.isManualDisconnect = true;
    this._clearTimers();
    if (this.ws) {
      try { this.ws.close(1000, 'Client closed'); } catch {}
      this.ws = null;
    }
    this.onStatusChange('disconnected', 'Disconnected.');
  }

  _startHeartbeat() {
    this.heartbeatTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({
          v: 1,
          type: 'heartbeat',
          timestamp: Date.now()
        }));
      }
    }, 20000); // Send heartbeat every 20s
  }

  _scheduleReconnect() {
    this._clearTimers();
    this.reconnectTimer = setTimeout(() => {
      this.backoffMs = Math.min(this.backoffMs * 2, this.maxBackoffMs);
      this.connect();
    }, this.backoffMs);
  }

  _clearTimers() {
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.heartbeatTimer = null;
    this.reconnectTimer = null;
  }

  async _handleMessage(msg) {
    if (msg.type === 'connected') {
      this.onActivityLog({
        type: 'system',
        message: `Registered on server as '${msg.device_name}' (${msg.device_id})`,
        time: new Date().toLocaleTimeString()
      });
      return;
    }

    if (msg.type === 'heartbeat_ack') {
      return;
    }

    if (msg.type === 'command') {
      const { id, action, params } = msg;

      // 1. Send Ack immediately
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({
          v: 1,
          type: 'ack',
          id: id
        }));
      }

      this.onActivityLog({
        type: 'command_received',
        id: id,
        action: action,
        params: params,
        time: new Date().toLocaleTimeString()
      });

      // 2. Execute local safe action
      const result = await this.executor.execute(action, params);

      this.onActivityLog({
        type: 'command_result',
        id: id,
        action: action,
        status: result.status,
        result: result.output,
        time: new Date().toLocaleTimeString()
      });

      // 3. Send result back to cloud
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({
          v: 1,
          type: 'result',
          id: id,
          status: result.status,
          output: result.output
        }));
      }
    }
  }
}

module.exports = DeviceWebSocketClient;

