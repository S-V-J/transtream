document.addEventListener('alpine:init', () => {
  Alpine.data('studioRecorder', () => ({
    recording: false,
    connecting: false,
    error: null,
    saved: null,
    sessionId: '',
    nativeScript: false,

    // Phase 3: normalization
    normalizing: false,
    normalized: null,
    normalizeMessage: '',

    // Phase 4: VAD
    vadRunning: false,
    vadSegments: [],
    vadTotalSpeechMs: 0,
    vadMessage: '',

    // Phase 5: transcription
    transcribing: false,
    transcribeMessage: '',
    languageDetected: '',
    transcriptSegments: [],

    chunksSent: 0,
    chunksReceived: 0,
    bytesReceived: 0,
    serverDurationMs: 0,
    elapsedMs: 0,
    maxBytes: 0,

    _ws: null,
    _recorder: null,
    _stream: null,
    _sendQueue: Promise.resolve(),
    _timer: null,
    _startTs: 0,
    _mime: '',

    destroy() {
      if (this.recording) { try { this.stop(); } catch (e) { /* ignore */ } }
      this._closeSocket();
      this._cleanupStream();
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
    },

    async start() {
      if (this.recording || this.connecting) return;
      this.error = null;
      this.saved = null;
      this.normalizing = false; this.normalized = null; this.normalizeMessage = '';
      this.vadRunning = false; this.vadSegments = []; this.vadTotalSpeechMs = 0; this.vadMessage = '';
      this.transcribing = false; this.transcribeMessage = ''; this.languageDetected = ''; this.transcriptSegments = [];
      this.chunksSent = 0; this.chunksReceived = 0; this.bytesReceived = 0;
      this.serverDurationMs = 0; this.elapsedMs = 0;
      this._sendQueue = Promise.resolve();
      this.sessionId = this._newSessionId();
      this.connecting = true;

      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      } catch (err) {
        this.connecting = false; this.error = this._micErrorMessage(err); return;
      }
      this._stream = stream;

      try { await this._openSocket(); } catch (err) {
        this._cleanupStream(); this.connecting = false; this.error = 'WebSocket connection failed: ' + this._errMessage(err); return;
      }

      const mime = this._pickMime();
      this._mime = mime;
      try {
        this._recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      } catch (err) {
        this._closeSocket(); this._cleanupStream(); this.connecting = false; this.error = 'MediaRecorder init failed: ' + this._errMessage(err); return;
      }

      this._recorder.ondataavailable = (e) => {
        if (!e.data || e.data.size === 0) return;
        this.chunksSent += 1;
        this._sendQueue = this._sendQueue
          .then(() => e.data.arrayBuffer())
          .then((buf) => { if (this._ws && this._ws.readyState === WebSocket.OPEN) this._ws.send(buf); })
          .catch(() => { /* socket gone */ });
      };

      this._ws.send(JSON.stringify({ type: 'start', mime: mime || 'unknown', client_ts: Date.now(), session: this.sessionId }));
      this._recorder.start(100);
      this._startTs = performance.now();
      this._timer = setInterval(() => { this.elapsedMs = performance.now() - this._startTs; }, 100);
      this.connecting = false;
      this.recording = true;
    },

    async stop() {
      if (!this.recording) return;
      this.recording = false;
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
      if (this._recorder && this._recorder.state !== 'inactive') {
        const stopped = new Promise((resolve) => { this._recorder.onstop = resolve; });
        try { this._recorder.stop(); } catch (e) { /* already stopped */ }
        await stopped;
      }
      await this._sendQueue;
      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        this._ws.send(JSON.stringify({ type: 'stop', client_ts: Date.now() }));
      }
      this._cleanupStream();
    },

    _openSocket() {
      return new Promise((resolve, reject) => {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = proto + '//' + location.host + '/ws/studio/' + this.sessionId + '/';
        const ws = new WebSocket(url);
        ws.binaryType = 'arraybuffer';
        let settled = false;
        ws.onopen = () => { this._ws = ws; settled = true; resolve(); };
        ws.onerror = () => { if (!settled) { settled = true; reject(new Error('socket error')); } };
        ws.onclose = (e) => {
          this._ws = null;
          if (this.recording) {
            this.recording = false;
            if (this._timer) { clearInterval(this._timer); this._timer = null; }
            this._cleanupStream();
            this.error = 'Connection lost during recording (code ' + e.code + ').';
          }
          if (!settled) { settled = true; reject(new Error('closed before open (code ' + e.code + ')')); }
        };
        ws.onmessage = (evt) => this._handleServerMessage(evt);
        setTimeout(() => {
          if (!settled) { try { ws.close(); } catch (e) { /* ignore */ } settled = true; reject(new Error('handshake timeout')); }
        }, 8000);
      });
    },

    _closeSocket() {
      if (this._ws) { try { this._ws.close(); } catch (e) { /* ignore */ } this._ws = null; }
    },

    _handleServerMessage(evt) {
      let msg;
      try { msg = JSON.parse(evt.data); } catch (e) { return; }
      switch (msg.type) {
        case 'connected': this.maxBytes = msg.max_bytes || this.maxBytes; break;
        case 'started': this.maxBytes = msg.max_bytes || this.maxBytes; break;
        case 'stats':
          this.chunksReceived = msg.chunks || 0;
          this.bytesReceived = msg.bytes || 0;
          this.serverDurationMs = msg.duration_ms || 0;
          break;
        case 'saved': this.saved = msg; break;
        case 'normalizing': this.normalizing = true; this.normalized = null; this.normalizeMessage = msg.message || 'Normalizing audio…'; break;
        case 'normalized': this.normalizing = false; this.normalized = msg; this.normalizeMessage = ''; break;
        case 'vad_started': this.vadRunning = true; this.vadSegments = []; this.vadTotalSpeechMs = 0; this.vadMessage = msg.message || 'Detecting speech segments…'; break;
        case 'vad_complete': this.vadRunning = false; this.vadSegments = msg.segments || []; this.vadTotalSpeechMs = msg.total_speech_ms || 0; this.vadMessage = ''; break;
        case 'transcribing_started': this.transcribing = true; this.transcriptSegments = []; this.languageDetected = ''; this.transcribeMessage = msg.message || 'Transcribing…'; break;
        case 'transcribing_complete':
          this.transcribing = false;
          this.languageDetected = msg.language ? msg.language.toUpperCase() : 'UNKNOWN';
          this.transcriptSegments = msg.segments || [];
          this.transcribeMessage = '';
          break;
        case 'error':
          if (msg.saved) this.saved = msg.saved;
          if (msg.stage === 'normalization') { this.normalizing = false; this.normalizeMessage = ''; }
          if (msg.stage === 'vad') { this.vadRunning = false; this.vadMessage = ''; }
          if (msg.stage === 'transcription') { this.transcribing = false; this.transcribeMessage = ''; }
          this.error = msg.message || 'Server error.';
          if (this.recording) { try { this.stop(); } catch (e) { /* ignore */ } }
          break;
      }
    },

    _newSessionId() {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
      return 'sess-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10);
    },

    _pickMime() {
      const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
      if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return '';
      for (const c of candidates) { if (MediaRecorder.isTypeSupported(c)) return c; }
      return '';
    },

    _errMessage(err) { return err && err.message ? err.message : String(err); },

    _micErrorMessage(err) {
      switch (err && err.name) {
        case 'NotAllowedError': return 'Microphone permission denied. Allow mic access for this site, then try again.';
        case 'NotFoundError': case 'DevicesNotFoundError': return 'No microphone found. Connect one and reload the page.';
        case 'NotReadableError': case 'TrackStartError': return 'Microphone is already in use by another application.';
        case 'SecurityError': return 'Microphone requires a secure context. Open http://localhost:PORT, not an IP address.';
        case 'OverconstrainedError': return 'Microphone does not support the requested audio constraints.';
        default: return 'Microphone error: ' + this._errMessage(err);
      }
    },

    _cleanupStream() {
      if (this._stream) {
        this._stream.getTracks().forEach((track) => { try { track.stop(); } catch (e) { /* ignore */ } });
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

    fmtSegTime(ms) {
      ms = Math.max(0, Math.floor(ms || 0));
      const m = String(Math.floor(ms / 60000) % 60).padStart(2, '0');
      const s = String(Math.floor(ms / 1000) % 60).padStart(2, '0');
      const frac = String(ms % 1000).padStart(3, '0');
      return m + ':' + s + '.' + frac;
    },

    fmtBytes(n) {
      n = Number(n) || 0;
      if (n === 0) return '0 B';
      const units = ['B', 'KB', 'MB', 'GB'];
      let i = 0, v = n;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
      return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + ' ' + units[i];
    },

    fmtSampleRate(hz) {
      hz = Number(hz) || 0;
      if (hz >= 1000) return (hz / 1000).toFixed(hz % 1000 === 0 ? 0 : 1) + ' kHz';
      return hz + ' Hz';
    },

    pipelineLabel() {
      if (this.connecting) return 'Connecting…';
      if (this.recording) return 'Recording…';
      if (this.normalizing) return 'Normalizing…';
      if (this.vadRunning) return 'Detecting speech…';
      if (this.transcribing) return 'Transcribing…';
      if (this.transcriptSegments.length > 0) return 'Ready';
      if (this.saved) return 'Saved';
      return 'Idle';
    },

    pipelineBusy() {
      return this.connecting || this.recording || this.normalizing || this.vadRunning || this.transcribing;
    },
  }));
});
