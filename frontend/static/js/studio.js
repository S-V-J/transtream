// ─── TranStream Studio recorder component (Phases 2 + 3 + 4 + 5 + 5.5 + 7) ──
//
// Mic path (unchanged, proven green since 63fd646):
//   Browser mic → MediaRecorder 100 ms slices → WebSocket binary frames
//   → Django Channels AudioConsumer → incremental temp file → raw recording
//   → FFmpeg normalization → 16 kHz mono PCM WAV
//   → Silero VAD segments → Faster-Whisper transcript
//   + live rolling preview ("live_transcript_update") while recording.
//
// Upload path (Phase 7, NEW, fully independent):
//   <input type=file> / drag-drop → XHR POST /studio/upload/ (byte progress)
//   → 202 {job_id} → poll /studio/jobs/<id>/ (pipeline progress from the
//   background worker) → /studio/jobs/<id>/transcript/ → render.
//
// Isolation rule: the upload path reads ONLY the pure formatters
// (fmtTime / fmtSegTime / fmtBytes / fmtSampleRate / _errMessage /
// _csrfToken) and writes ONLY into `this.upload.*`. It never touches
// transcriptSegments, vadSegments, saved, normalized, liveTranscriptText or
// any WebSocket/mic flag. An upload finishing mid-recording therefore cannot
// overwrite a mic result — the two timelines are disjoint by construction,
// mirroring how the VAD and Whisper timelines are kept disjoint in the DB.
//
// Ordering guarantee (Phase 2, unchanged): every slice is pushed through a
// serialized promise queue so binary frames reach the server in capture
// order; the JSON "stop" control is sent only after the queue drains.
// This is what keeps the concatenated WebM container valid.

document.addEventListener('alpine:init', () => {
  Alpine.data('studioRecorder', () => ({
    // ── public state ──
    recording: false,
    connecting: false,
    error: null,
    saved: null,
    sessionId: '',
    nativeScript: false,

    // ── Phase 3: normalization ──
    normalizing: false,
    normalized: null,
    normalizeMessage: '',

    // ── Phase 4: VAD ──
    vadRunning: false,
    vadSegments: [],
    vadTotalSpeechMs: 0,
    vadMessage: '',

    // ── Phase 5: transcription ──
    transcribing: false,
    transcribeMessage: '',
    languageDetected: '',
    languageProbability: 0,
    transcriptSegments: [],

    // ── Phase 5.5: live rolling preview ──
    liveTranscriptText: '',
    liveSegmentCount: 0,
    liveLanguage: '',
    liveActive: false,          // true only while a take is in progress

    // ── counters ─
    chunksSent: 0,
    chunksReceived: 0,
    bytesReceived: 0,
    serverDurationMs: 0,
    elapsedMs: 0,
    maxBytes: 0,

    // ── internals ──
    _ws: null,
    _recorder: null,
    _stream: null,
    _sendQueue: Promise.resolve(),
    _timer: null,
    _startTs: 0,
    _mime: '',

    // ── Phase 7: file upload (own namespace; see isolation rule above) ──
    upload: {
      file: null,
      name: '',
      size: 0,
      dragOver: false,
      uploading: false,   // POST in flight (amber bar)
      progress: 0,        // 0..100 bytes sent to the server
      jobId: null,
      takeId: null,
      state: '',          // queued|normalizing|segmenting|transcribing|done|failed|cancelled
      pct: 0,             // 0..100 server-side pipeline progress (emerald bar)
      error: null,
      code: '',
      segments: [],
      language: '',
      languageProbability: 0,
      durationMs: 0,
      polling: false,
      _timer: null,
      _deadline: 0,
    },

    // ── Alpine lifecycle ──

    destroy() {
      this.stopUploadPolling();
      if (this.recording) {
        try { this.stop(); } catch (e) { /* ignore */ }
      }
      this._closeSocket();
      this._cleanupStream();
      if (this._timer) { clearInterval(this._timer); this._timer = null; }
    },

    // ── controls ──

    async start() {
      if (this.recording || this.connecting) return;

      this.error = null;
      this.saved = null;

      this.normalizing = false; this.normalized = null; this.normalizeMessage = '';
      this.vadRunning = false; this.vadSegments = []; this.vadTotalSpeechMs = 0; this.vadMessage = '';
      this.transcribing = false; this.transcribeMessage = '';
      this.languageDetected = ''; this.languageProbability = 0; this.transcriptSegments = [];
      this.liveTranscriptText = ''; this.liveSegmentCount = 0; this.liveLanguage = '';
      this.liveActive = true;

      this.chunksSent = 0; this.chunksReceived = 0; this.bytesReceived = 0;
      this.serverDurationMs = 0; this.elapsedMs = 0;

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
        this.liveActive = false;
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
        this.liveActive = false;
        this.error = 'WebSocket connection failed: ' + this._errMessage(err);
        return;
      }

      // 3. MediaRecorder
      const mime = this._pickMime();
      this._mime = mime;
      try {
        this._recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      } catch (err) {
        this._closeSocket();
        this._cleanupStream();
        this.connecting = false;
        this.liveActive = false;
        this.error = 'MediaRecorder init failed: ' + this._errMessage(err);
        return;
      }

      this._recorder.ondataavailable = (e) => {
        if (!e.data || e.data.size === 0) return;
        this.chunksSent += 1;
        this._sendQueue = this._sendQueue
          .then(() => e.data.arrayBuffer())
          .then((buf) => {
            if (this._ws && this._ws.readyState === WebSocket.OPEN) this._ws.send(buf);
          })
          .catch(() => { /* socket gone; onclose handles UI */ });
      };

      // 4. control message, then capture
      this._ws.send(JSON.stringify({
        type: 'start',
        mime: mime || 'unknown',
        client_ts: Date.now(),
        session: this.sessionId,
      }));

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

      // Preview is over the moment we stop; clear it so the results view
      // is never visually competing with stale live text.
      this.liveActive = false;
      this.liveTranscriptText = '';
      this.liveSegmentCount = 0;

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

        ws.onopen = () => { this._ws = ws; settled = true; resolve(); };
        ws.onerror = () => { if (!settled) { settled = true; reject(new Error('socket error')); } };

        ws.onclose = (e) => {
          this._ws = null;
          this.liveActive = false;

          if (this.recording) {
            this.recording = false;
            if (this._timer) { clearInterval(this._timer); this._timer = null; }
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
            try { ws.close(); } catch (e) { /* ignore */ }
            settled = true;
            reject(new Error('handshake timeout'));
          }
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

        // ── Phase 5.5: live rolling preview ──
        // Guarded: a late preview frame must never resurrect the live box
        // after Stop, and must never overwrite the authoritative transcript.
        case 'live_transcript_update':
          if (!this.recording) break;
          this.liveTranscriptText = msg.text || '';
          this.liveSegmentCount = msg.segment_count || 0;
          this.liveLanguage = msg.language ? String(msg.language).toUpperCase() : '';
          break;

        // ── Phase 3 ──
        case 'normalizing':
          this.normalizing = true; this.normalized = null;
          this.normalizeMessage = msg.message || 'Normalizing audio\u2026';
          break;

        case 'normalized':
          this.normalizing = false; this.normalized = msg; this.normalizeMessage = '';
          break;

        // ── Phase 4 ──
        case 'vad_started':
          this.vadRunning = true; this.vadSegments = []; this.vadTotalSpeechMs = 0;
          this.vadMessage = msg.message || 'Detecting speech segments\u2026';
          break;

        case 'vad_complete':
          this.vadRunning = false;
          this.vadSegments = msg.segments || [];
          this.vadTotalSpeechMs = msg.total_speech_ms || 0;
          this.vadMessage = '';
          break;

        // ── Phase 5 ──
        case 'transcribing_started':
          this.transcribing = true; this.transcriptSegments = [];
          this.languageDetected = ''; this.languageProbability = 0;
          this.transcribeMessage = msg.message || 'Transcribing\u2026';
          break;

        case 'transcribing_complete':
          this.transcribing = false;
          this.languageDetected = msg.language ? String(msg.language).toUpperCase() : 'UNKNOWN';
          this.languageProbability = msg.language_probability || 0;
          if (Array.isArray(msg.segments) && msg.segments.length > 0) {
            this.transcriptSegments = msg.segments;
          }
          this.transcribeMessage = '';
          // Authoritative text supersedes the preview entirely.
          this.liveTranscriptText = '';
          this.liveSegmentCount = 0;
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

    // ── Phase 7: upload controls ────────────────────────────────────────

    pickUploadFile() {
      const el = document.getElementById('ts-upload-input');
      if (el) el.click();
    },

    onUploadFileChosen(e) {
      const f = e.target.files && e.target.files[0];
      if (f) this.setUploadFile(f);
      // Reset so choosing the same file again still fires a change event.
      e.target.value = '';
    },

    onUploadDragOver(e) {
      e.preventDefault();
      e.stopPropagation();
      this.upload.dragOver = true;
    },

    onUploadDragLeave(e) {
      e.preventDefault();
      e.stopPropagation();
      this.upload.dragOver = false;
    },

    onUploadDrop(e) {
      e.preventDefault();
      e.stopPropagation();
      this.upload.dragOver = false;
      const dt = e.dataTransfer;
      const f = dt && dt.files && dt.files[0];
      if (f) this.setUploadFile(f);
    },

    setUploadFile(f) {
      this.stopUploadPolling();
      const u = this.upload;
      u.file = f;
      u.name = f.name || 'upload';
      u.size = f.size || 0;
      u.uploading = false;
      u.progress = 0;
      u.jobId = null;
      u.takeId = null;
      u.state = '';
      u.pct = 0;
      u.error = null;
      u.code = '';
      u.segments = [];
      u.language = '';
      u.languageProbability = 0;
      u.durationMs = 0;
    },

    clearUpload() {
      this.stopUploadPolling();
      const u = this.upload;
      u.file = null; u.name = ''; u.size = 0; u.dragOver = false;
      u.uploading = false; u.progress = 0;
      u.jobId = null; u.takeId = null;
      u.state = ''; u.pct = 0; u.error = null; u.code = '';
      u.segments = []; u.language = ''; u.languageProbability = 0; u.durationMs = 0;
    },

    async startUpload() {
      const u = this.upload;
      if (!u.file || u.uploading || u.polling) return;

      u.error = null; u.code = '';
      u.segments = []; u.language = ''; u.languageProbability = 0; u.durationMs = 0;
      u.progress = 0; u.pct = 0; u.state = 'uploading';
      u.uploading = true;

      const form = new FormData();
      form.append('file', u.file, u.name);

      try {
        const res = await this._xhrUpload('/studio/upload/', form, (pct) => { u.progress = pct; });
        if (!res.ok) {
          u.state = '';
          u.error = res.body.error || ('Upload failed (HTTP ' + res.status + ').');
          u.code = res.body.code || '';
          return;
        }
        u.jobId = res.body.job_id || null;
        u.takeId = res.body.take_id || null;
        u.state = res.body.status || 'queued';
        u.pct = 0;
        if (!u.jobId) {
          u.error = 'Server accepted the file but returned no job id.';
          return;
        }
        this.startUploadPolling();
      } catch (err) {
        u.state = '';
        u.error = 'Network error during upload: ' + this._errMessage(err);
      } finally {
        u.uploading = false;
      }
    },

    // XHR rather than fetch: fetch exposes no upload-progress event, and a
    // 100 MB local upload with no byte counter is indistinguishable from a
    // hang. Timeout is generous (2 min) but finite, so a dead socket cannot
    // wedge the panel forever.
    _xhrUpload(url, form, onProgress) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', url, true);
        xhr.setRequestHeader('X-CSRFToken', this._csrfToken());
        xhr.setRequestHeader('Accept', 'application/json');
        xhr.timeout = 120000;
        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
        };
        xhr.onload = () => {
          let body = {};
          try { body = JSON.parse(xhr.responseText); } catch (e) { /* non-JSON */ }
          resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, body });
        };
        xhr.onerror = () => reject(new Error('network error'));
        xhr.ontimeout = () => reject(new Error('upload timed out after 120s'));
        xhr.send(form);
      });
    },

    startUploadPolling() {
      const u = this.upload;
      if (!u.jobId) return;
      this.stopUploadPolling();
      u.polling = true;
      // 20-minute ceiling: a long take on a slow CPU can legitimately take
      // minutes of Whisper, but never 20 hours. On expiry we stop the timer
      // and say so, rather than spinning silently forever.
      u._deadline = Date.now() + 20 * 60 * 1000;

      const tick = async () => {
        if (!this.upload.polling) return;
        try {
          const resp = await fetch('/studio/jobs/' + u.jobId + '/', {
            headers: { 'Accept': 'application/json' },
          });
          if (!resp.ok) throw new Error('HTTP ' + resp.status);
          const d = await resp.json();

          u.state = d.state || u.state;
          u.pct = d.progress_pct || 0;

          if (d.state === 'done') {
            this.stopUploadPolling();
            await this.fetchUploadTranscript();
            return;
          }
          if (d.state === 'failed') {
            this.stopUploadPolling();
            u.error = d.last_error || 'Transcription failed. See /admin/studio/job/.';
            return;
          }
          if (d.state === 'cancelled') {
            this.stopUploadPolling();
            u.error = 'Job was cancelled.';
            return;
          }
          if (Date.now() > u._deadline) {
            this.stopUploadPolling();
            u.error = 'Still ' + (d.state || 'processing') + ' after 20 minutes. '
              + 'Is the worker running? (cd backend && python manage.py tsworker)';
            return;
          }
          u._timer = setTimeout(tick, 1500);
        } catch (err) {
          // Transient poll failure (server restart, blip): back off and retry
          // rather than killing the loop. Only give up past the deadline.
          if (Date.now() > u._deadline) {
            this.stopUploadPolling();
            u.error = 'Polling failed: ' + this._errMessage(err);
            return;
          }
          u._timer = setTimeout(tick, 3000);
        }
      };

      u._timer = setTimeout(tick, 800);
    },

    stopUploadPolling() {
      const u = this.upload;
      if (u._timer) { clearTimeout(u._timer); u._timer = null; }
      u.polling = false;
    },

    async fetchUploadTranscript() {
      const u = this.upload;
      try {
        const resp = await fetch('/studio/jobs/' + u.jobId + '/transcript/', {
          headers: { 'Accept': 'application/json' },
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        const d = await resp.json();
        u.segments = Array.isArray(d.segments) ? d.segments : [];
        u.language = d.language ? String(d.language).toUpperCase() : '';
        u.languageProbability = d.language_probability || 0;
        u.durationMs = d.duration_ms || 0;
      } catch (err) {
        u.error = 'Transcript fetch failed: ' + this._errMessage(err);
      }
    },

    uploadStatusLabel() {
      const u = this.upload;
      if (u.uploading) return 'Uploading\u2026 ' + u.progress + '%';
      if (u.polling) {
        const names = {
          queued: 'Queued (waiting for worker)',
          normalizing: 'Normalizing (FFmpeg)\u2026',
          segmenting: 'Detecting speech (Silero VAD)\u2026',
          transcribing: 'Transcribing (Faster-Whisper)\u2026',
        };
        const base = names[u.state] || (u.state || 'Processing');
        return u.pct ? base + ' ' + u.pct + '%' : base;
      }
      if (u.state === 'done') return 'Complete';
      if (u.state === 'failed') return 'Failed';
      if (u.state === 'cancelled') return 'Cancelled';
      if (u.file) return 'Ready to upload';
      return 'No file selected';
    },

    _csrfToken() {
      const m = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
      return m ? decodeURIComponent(m[1]) : '';
    },

    // ── helpers ──

    _newSessionId() {
      if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
      return 'sess-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10);
    },

    _pickMime() {
      const candidates = [
        'audio/webm;codecs=opus',
        'audio/webm',
        'audio/ogg;codecs=opus',
        'audio/mp4',
      ];
      if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return '';
      for (const candidate of candidates) {
        if (MediaRecorder.isTypeSupported(candidate)) return candidate;
      }
      return '';
    },

    _errMessage(err) { return err && err.message ? err.message : String(err); },

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
          try { track.stop(); } catch (e) { /* ignore */ }
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

    // Full pipeline still running?
    // NOTE: deliberately excludes upload state. Uploading is an HTTP POST and
    // the worker's queue is independent of the mic, so a user may legitimately
    // record while an upload transcribes. pipelineBusy() gates the MIC controls
    // only.
    pipelineBusy() {
      return this.connecting || this.recording || this.normalizing || this.vadRunning || this.transcribing;
    },

    // Status label for the header pill.
    pipelineLabel() {
      if (this.connecting) return 'Connecting\u2026';
      if (this.recording) {
        // Surface preview liveness right in the status line.
        return this.liveTranscriptText ? 'Recording\u2026 (live)' : 'Recording\u2026';
      }
      if (this.normalizing) return 'Normalizing\u2026';
      if (this.vadRunning) return 'Detecting speech\u2026';
      if (this.transcribing) return 'Transcribing\u2026';
      if (this.transcriptSegments.length > 0) return 'Ready';
      if (this.saved) return 'Saved';
      return 'Idle';
    },
  }));
});
