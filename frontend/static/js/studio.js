// ─── TranStream Studio recorder component (Phase 2) ────────────────────────
//
// Browser microphone -> MediaRecorder 100 ms slices -> WebSocket binary frames
// -> Django Channels AudioConsumer -> incremental temp file -> raw recording.
//
// Ordering guarantee:
// Every slice is pushed through a serialized promise queue so binary frames
// reach the server in capture order. The JSON "stop" control message is sent
// only after the queue has fully drained. This keeps the raw container valid.

document.addEventListener('alpine:init', () => {
  Alpine.data('studioRecorder', () => ({
    // ── public state ──
    recording: false,
    connecting: false,
    error: null,
    saved: null,
    sessionId: '',
    nativeScript: false,

    // ── counters ─
    chunksSent: 0,
    chunksReceived: 0,
    bytesReceived: 0,
    serverDurationMs: 0,
    elapsedMs: 0,
    maxBytes: 0,

    // ── internals ─
    _ws: null,
    _recorder: null,
    _stream: null,
    _sendQueue: Promise.resolve(),
    _timer: null,
    _startTs: 0,
    _mime: '',

    // ── Alpine lifecycle ──

    destroy() {
      if (this.recording) {
        try {
          this.stop();
        } catch (e) {
          // ignore cleanup errors
        }
      }

      this._closeSocket();
      this._cleanupStream();

      if (this._timer) {
        clearInterval(this._timer);
        this._timer = null;
      }
    },

    // ── controls ──

    async start() {
      if (this.recording || this.connecting) {
        return;
      }

      this.error = null;
      this.saved = null;

      this.chunksSent = 0;
      this.chunksReceived = 0;
      this.bytesReceived = 0;
      this.serverDurationMs = 0;
      this.elapsedMs = 0;

      this._sendQueue = Promise.resolve();

      this.sessionId = this._newSessionId();
      this.connecting = true;

      // 1. microphone permission + stream
      let stream;

      try {
        stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
          },
        });
      } catch (err) {
        this.connecting = false;
        this.error = this._micErrorMessage(err);
        return;
      }

      this._stream = stream;

      // 2. websocket
      try {
        await this._openSocket();
      } catch (err) {
        this._cleanupStream();
        this.connecting = false;
        this.error = 'WebSocket connection failed: ' + this._errMessage(err);
        return;
      }

      // 3. MediaRecorder
      const mime = this._pickMime();
      this._mime = mime;

      try {
        this._recorder = new MediaRecorder(
          stream,
          mime ? { mimeType: mime } : undefined
        );
      } catch (err) {
        this._closeSocket();
        this._cleanupStream();
        this.connecting = false;
        this.error = 'MediaRecorder init failed: ' + this._errMessage(err);
        return;
      }

      this._recorder.ondataavailable = (e) => {
        if (!e.data || e.data.size === 0) {
          return;
        }

        this.chunksSent += 1;

        // Serialize sends to preserve slice order.
        this._sendQueue = this._sendQueue
          .then(() => e.data.arrayBuffer())
          .then((buf) => {
            if (this._ws && this._ws.readyState === WebSocket.OPEN) {
              this._ws.send(buf);
            }
          })
          .catch(() => {
            // Socket gone; onclose handles UI state.
          });
      };

      // 4. control message, then capture
      this._ws.send(JSON.stringify({
        type: 'start',
        mime: mime || 'unknown',
        client_ts: Date.now(),
        session: this.sessionId,
      }));

      this._recorder.start(100); // 100 ms slices

      this._startTs = performance.now();

      this._timer = setInterval(() => {
        this.elapsedMs = performance.now() - this._startTs;
      }, 100);

      this.connecting = false;
      this.recording = true;
    },

    async stop() {
      if (!this.recording) {
        return;
      }

      this.recording = false;

      if (this._timer) {
        clearInterval(this._timer);
        this._timer = null;
      }

      // Finish recorder and wait for final dataavailable event.
      if (this._recorder && this._recorder.state !== 'inactive') {
        const stopped = new Promise((resolve) => {
          this._recorder.onstop = resolve;
        });

        try {
          this._recorder.stop();
        } catch (e) {
          // already stopped
        }

        await stopped;
      }

      // Drain every queued binary frame before sending stop control message.
      await this._sendQueue;

      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        this._ws.send(JSON.stringify({
          type: 'stop',
          client_ts: Date.now(),
        }));
      }

      this._cleanupStream();
    },

    // ── websocket ──

    _openSocket() {
      return new Promise((resolve, reject) => {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = proto + '//' + location.host + '/ws/studio/' + this.sessionId + '/';

        const ws = new WebSocket(url);
        ws.binaryType = 'arraybuffer';

        let settled = false;

        ws.onopen = () => {
          this._ws = ws;
          settled = true;
          resolve();
        };

        ws.onerror = () => {
          if (!settled) {
            settled = true;
            reject(new Error('socket error'));
          }
        };

        ws.onclose = (e) => {
          this._ws = null;

          if (this.recording) {
            this.recording = false;

            if (this._timer) {
              clearInterval(this._timer);
              this._timer = null;
            }

            this._cleanupStream();
            this.error = 'Connection lost during recording (code ' + e.code + ').';
          }

          if (!settled) {
            settled = true;
            reject(new Error('closed before open (code ' + e.code + ')'));
          }
        };

        ws.onmessage = (evt) => this._handleServerMessage(evt);

        setTimeout(() => {
          if (!settled) {
            try {
              ws.close();
            } catch (e) {
              // ignore
            }

            settled = true;
            reject(new Error('handshake timeout'));
          }
        }, 8000);
      });
    },

    _closeSocket() {
      if (this._ws) {
        try {
          this._ws.close();
        } catch (e) {
          // ignore
        }

        this._ws = null;
      }
    },

    _handleServerMessage(evt) {
      let msg;

      try {
        msg = JSON.parse(evt.data);
      } catch (e) {
        return;
      }

      switch (msg.type) {
        case 'connected':
          this.maxBytes = msg.max_bytes || this.maxBytes;
          break;

        case 'started':
          this.maxBytes = msg.max_bytes || this.maxBytes;
          break;

        case 'stats':
          this.chunksReceived = msg.chunks || 0;
          this.bytesReceived = msg.bytes || 0;
          this.serverDurationMs = msg.duration_ms || 0;
          break;

        case 'saved':
          this.saved = msg;
          break;

        case 'error':
          if (msg.saved) {
            this.saved = msg.saved;
          }

          this.error = msg.message || 'Server error.';

          if (this.recording) {
            try {
              this.stop();
            } catch (e) {
              // ignore
            }
          }

          break;
      }
    },

    // ── helpers ──

    _newSessionId() {
      if (window.crypto && crypto.randomUUID) {
        return crypto.randomUUID();
      }

      return 'sess-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10);
    },

    _pickMime() {
      const candidates = [
        'audio/webm;codecs=opus',
        'audio/webm',
        'audio/ogg;codecs=opus',
        'audio/mp4',
      ];

      if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) {
        return '';
      }

      for (const candidate of candidates) {
        if (MediaRecorder.isTypeSupported(candidate)) {
          return candidate;
        }
      }

      return '';
    },

    _errMessage(err) {
      return err && err.message ? err.message : String(err);
    },

    _micErrorMessage(err) {
      switch (err && err.name) {
        case 'NotAllowedError':
          return 'Microphone permission denied. Allow mic access for this site, then try again.';

        case 'NotFoundError':
        case 'DevicesNotFoundError':
          return 'No microphone found. Connect one and reload the page.';

        case 'NotReadableError':
        case 'TrackStartError':
          return 'Microphone is already in use by another application.';

        case 'SecurityError':
          return 'Microphone requires a secure context. Open http://localhost:PORT, not an IP address.';

        case 'OverconstrainedError':
          return 'Microphone does not support the requested audio constraints.';

        default:
          return 'Microphone error: ' + this._errMessage(err);
      }
    },

    _cleanupStream() {
      if (this._stream) {
        this._stream.getTracks().forEach((track) => {
          try {
            track.stop();
          } catch (e) {
            // ignore
          }
        });

        this._stream = null;
      }
    },

    fmtTime(ms) {
      ms = Math.max(0, Math.floor(ms || 0));

      const h = String(Math.floor(ms / 3600000)).padStart(2, '0');
      const m = String(Math.floor(ms / 60000) % 60).padStart(2, '0');
      const s = String(Math.floor(ms / 1000) % 60).padStart(2, '0');
      const d = String(Math.floor(ms / 100) % 10);

      return h + ':' + m + ':' + s + '.' + d;
    },

    fmtBytes(n) {
      n = Number(n) || 0;

      if (n === 0) {
        return '0 B';
      }

      const units = ['B', 'KB', 'MB', 'GB'];
      let i = 0;
      let v = n;

      while (v >= 1024 && i < units.length - 1) {
        v /= 1024;
        i += 1;
      }

      return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + ' ' + units[i];
    },
  }));
});
