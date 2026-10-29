// Browser-side AI proctoring engine.
//  - MediaPipe FaceLandmarker : face count, head pose, gaze (blendshapes)
//  - MediaPipe ObjectDetector : phones, books, extra laptops, extra people
//  - WebAudio analyser        : talking / voice activity
// All inference runs locally in the student's browser; only screenshots of
// flagged moments are uploaded (by exam.js).
import {
  Sustained, MovementAnalyzer, VoiceActivity, CalibrationBuffer, CALIB, headPose, lookingAway, gazeAway, classifyObjects,
} from './detectors.js';

const VISION_VERSION = '0.10.14';
// two CDNs: if the first is blocked (college / office networks) the second is tried
const CDNS = [
  `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@${VISION_VERSION}`,
  `https://unpkg.com/@mediapipe/tasks-vision@${VISION_VERSION}`,
];
const FACE_MODEL = 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task';
const OBJECT_MODEL = 'https://storage.googleapis.com/mediapipe-tasks/object_detector/efficientdet_lite0_uint8.tflite';

const FACE_TICK_MS = 250;      // face analysis ~4 fps
const OBJECT_EVERY = 4;        // object detection every 4th tick (~1 fps)
const AUDIO_TICK_MS = 100;
const withTimeout = (p, ms, msg) => Promise.race([
  p, new Promise((_, rej) => setTimeout(() => rej(new Error(msg)), ms)),
]);

export class Proctor {
  /**
   * @param {object} o
   * @param {(type:string, detail:string, boxes:Array)=>void} o.onViolation
   * @param {(s:object)=>void} [o.onStatus]
   * @param {()=>void} [o.onMediaLost]
   */
  constructor(o) {
    this.onViolation = o.onViolation;
    this.onStatus = o.onStatus || (() => {});
    this.onMediaLost = o.onMediaLost || (() => {});
    this.videos = [];
    this.stream = null;
    this.audio = null;
    this.face = null;
    this.objects = null;
    this.phase = 'idle';          // idle | loading | calibrating | ready | monitoring | stopped
    this.base = null;
    this.calib = new CalibrationBuffer();
    this.calibStart = 0;
    this.calibSeen = false;
    this._faceErrRun = 0;
    this._fps = { n: 0, t: 0, v: 0 };
    this._frame = { t: -1, since: 0 };
    this.diag = { errors: 0, lastError: '' };
    this.micCalib = [];
    this.tickN = 0;
    this.monitoring = false;
    this.lastObj = { phone: null, other: null, persons: 0 };
    this.lastBoxes = [];
    this.lastFaceTs = 0; this.lastObjTs = 0;
    this.voice = new VoiceActivity();
    this.yawMove = new MovementAnalyzer();
    this.pitchMove = new MovementAnalyzer();
    this.rules = {
      no_face:        new Sustained(4000, 10000),
      multiple_faces: new Sustained(2000, 10000),
      looking_away:   new Sustained(3500, 12000),
      head_movement:  new Sustained(0, 15000),
      talking:        new Sustained(500, 15000),
      phone:          new Sustained(800, 10000),
      object:         new Sustained(1800, 15000),
    };
    this.canvas = document.createElement('canvas');
    this.micLevel = 0;
  }

  // ----------------------------------------------------------- lifecycle
  async start(onPhase) {
    const say = onPhase || (() => {});
    this._clearTimers();
    this._lost = false;
    this.calib.clear(); this.micCalib = []; this.base = null; this.calibSeen = false; this._faceErrRun = 0;
    this.phase = 'loading';
    say('camera:run');
    await this._acquireMedia();
    say('camera:ok');

    say('models:run');
    let vision = null;
    for (const cdn of CDNS) {
      try { vision = await withTimeout(import(`${cdn}/vision_bundle.mjs`), 30000, 'timeout'); this.cdn = cdn; break; }
      catch (e) { /* try next CDN */ }
    }
    if (!vision) throw Object.assign(new Error('Could not download the AI library. Check your internet connection (or try another network) and retry.'), { code: 'LIB' });
    try {
      this.vision = vision;
      this.fileset = await withTimeout(vision.FilesetResolver.forVisionTasks(`${this.cdn}/wasm`), 30000, 'wasm timeout');
      this.face = await withTimeout(this._createFace(false), 60000, 'face model timeout');
    } catch (e) {
      throw Object.assign(new Error('Could not load the face model. Check your connection and retry.'), { code: 'MODEL', cause: e });
    }
    // object detection (phones / books) is a bonus: if it fails, the exam still works without it
    this.objects = null;
    try {
      this.objects = await withTimeout(this._create(vision.ObjectDetector, this.fileset, OBJECT_MODEL,
        { scoreThreshold: 0.4, maxResults: 10 }, true), 60000, 'object model timeout');
    } catch (e) { this.objects = null; this.objectsOff = true; }
    say('models:ok');

    this.phase = 'calibrating';
    this.calibStart = performance.now();
    say('face:run');
    this._faceTimer = setInterval(() => this._tick(say), FACE_TICK_MS);
    this._audioTimer = setInterval(() => this._audioTick(), AUDIO_TICK_MS);
  }

  _clearTimers() { clearInterval(this._faceTimer); clearInterval(this._audioTimer); }

  /** Restart calibration (e.g. user moved, or it took too long). */
  recalibrate() {
    this.calib.clear(); this.base = null; this.calibSeen = false;
    this.micCalib = [];
    this.calibStart = performance.now();
    this.phase = 'calibrating';
  }

  _createFace(forceCpu) {
    return this._create(this.vision.FaceLandmarker, this.fileset, FACE_MODEL,
      { numFaces: 3, outputFaceBlendshapes: true, minFaceDetectionConfidence: 0.5, minTrackingConfidence: 0.5 }, forceCpu);
  }

  /** GPU delegate crashed at runtime on this machine: rebuild the face model on CPU. */
  async _fallbackFaceCpu() {
    if (this._recreating) return;
    this._recreating = true;
    const old = this.face; this.face = null;
    try { old && old.close(); } catch (e) { /* ignore */ }
    try { this.face = await this._createFace(true); this._faceErrRun = 0; this.lastFaceTs = 0; }
    catch (e) { this.diag.lastError = 'CPU fallback failed: ' + (e.message || e); }
    this._recreating = false;
  }

  async _create(Cls, fileset, modelPath, extra, forceCpu) {
    const make = (delegate) => Cls.createFromOptions(fileset, {
      baseOptions: { modelAssetPath: modelPath, delegate },
      runningMode: 'VIDEO', ...extra,
    });
    if (forceCpu) return make('CPU');
    try { return await make('GPU'); } catch (e) { return await make('CPU'); }
  }

  attachVideo(el) {
    if (!el) return;
    el.srcObject = this.stream;
    el.muted = true;
    el.play().catch(() => {});
    if (!this.videos.includes(el)) this.videos.push(el);
  }

  async _acquireMedia() {
    this._releaseMedia();
    this.stream = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 640 }, height: { ideal: 480 }, facingMode: 'user' },
      audio: { echoCancellation: true, noiseSuppression: true },
    });
    this.videos.forEach((v) => this.attachVideo(v));
    this.stream.getTracks().forEach((t) => {
      t.addEventListener('ended', () => this._mediaLost('ended'));
      let timer = null;
      // a short 'mute' is normal (browser hiccup); only a track that stays muted counts as lost
      t.addEventListener('mute', () => { clearTimeout(timer); timer = setTimeout(() => { if (t.muted) this._mediaLost('muted'); }, 4000); });
      t.addEventListener('unmute', () => clearTimeout(timer));
    });
    const AC = window.AudioContext || window.webkitAudioContext;
    this.audio = new AC();
    if (this.audio.state === 'suspended') await this.audio.resume();
    const src = this.audio.createMediaStreamSource(this.stream);
    this.analyser = this.audio.createAnalyser();
    this.analyser.fftSize = 1024;
    src.connect(this.analyser);
    this.buf = new Float32Array(this.analyser.fftSize);
  }

  async restartMedia() {
    await this._acquireMedia();
    this._lost = false;
  }

  _releaseMedia() {
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    if (this.audio) { try { this.audio.close(); } catch (e) { /* ignore */ } }
    this.stream = null; this.audio = null;
  }

  _mediaLost(why) {
    if (this._lost || this.phase === 'stopped') return;
    if (!this.monitoring) {
      // setup screen: only a real disconnect matters, and it is not an exam violation
      if (why === 'ended') { this._lost = true; this.onMediaLost('gate', why); }
      return;
    }
    this._lost = true;
    this.onViolation('camera_off', 'Camera or microphone ' + why, []);
    this.onMediaLost('exam', why);
  }

  beginMonitoring() {
    Object.values(this.rules).forEach((r) => { r.reset(); r.lastFire = -Infinity; });
    this.yawMove.clear(); this.pitchMove.clear();
    this.monitoring = true;
    this.phase = 'monitoring';
  }

  stop() {
    this.phase = 'stopped';
    this.monitoring = false;
    this._clearTimers();
    this._releaseMedia();
    try { this.face && this.face.close(); } catch (e) { /* ignore */ }
    try { this.objects && this.objects.close(); } catch (e) { /* ignore */ }
  }

  // --------------------------------------------------------------- audio
  _audioTick() {
    if (!this.analyser || this._lost) return;
    this.analyser.getFloatTimeDomainData(this.buf);
    let sum = 0;
    for (let i = 0; i < this.buf.length; i++) sum += this.buf[i] * this.buf[i];
    const rms = Math.sqrt(sum / this.buf.length);
    this.micLevel = rms;
    const now = performance.now();
    if (this.phase === 'calibrating' || this.phase === 'ready') {
      if (this.micCalib.length < 30) this.micCalib.push(rms);
    }
    this.voice.push(now, rms);
  }

  // ---------------------------------------------------------------- tick
  _videoReady(v) { return v && v.readyState >= 2 && v.videoWidth > 0; }

  _fpsTick(now) {
    const f = this._fps; f.n++;
    if (now - f.t >= 2000) { f.v = Math.round((f.n * 1000) / (now - f.t) * 10) / 10; f.n = 0; f.t = now; }
  }

  /** Is the camera really delivering new frames? (a frozen <video> never calibrates) */
  _frameFresh(video, now) {
    if (video.currentTime !== this._frame.t) { this._frame = { t: video.currentTime, since: now }; return true; }
    if (now - this._frame.since > 3000 && video.paused) video.play().catch(() => {});
    return now - this._frame.since < 3000;
  }

  _calibStatus(extra) {
    const now = performance.now();
    this.onStatus({
      phase: this.phase, away: null, voice: false, mic: this.micLevel,
      calibProgress: this.phase === 'ready' ? 1 : this.calib.progress(),
      stuckMs: now - this.calibStart, fps: this._fps.v, objectsOff: !!this.objectsOff,
      error: this.diag.lastError && this._faceErrRun >= 2 ? 'Detector error: ' + this.diag.lastError : '',
      ...extra,
    });
  }

  _tick(say) {
    const now = performance.now();
    const calibrating = this.phase === 'calibrating' || this.phase === 'ready';
    const video = this.videos.find((v) => this._videoReady(v));
    if (document.hidden) { if (calibrating) this._calibStatus({ faces: 0, hint: 'Keep this tab open and visible - calibration is paused.' }); return; }
    if (!this.face || this._lost) { if (calibrating) this._calibStatus({ faces: 0, hint: 'Starting the face detector…' }); return; }
    if (!video) { if (calibrating) this._calibStatus({ faces: 0, hint: 'Waiting for the camera picture…' }); return; }
    if (calibrating && !this._frameFresh(video, now)) {
      this._calibStatus({ faces: 0, hint: 'Camera frames are not updating. Close other apps using the camera, then press Restart.' });
      return;
    }
    if (now <= this.lastFaceTs) return;
    this.lastFaceTs = now;

    let res;
    try { res = this.face.detectForVideo(video, now); this._faceErrRun = 0; }
    catch (e) {
      this.diag.errors++; this.diag.lastError = (e && e.message) || String(e);
      if (++this._faceErrRun >= 5) this._fallbackFaceCpu();
      if (calibrating) this._calibStatus({ faces: 0, hint: 'The face detector had a problem - retrying…' });
      return;
    }
    this._fpsTick(now);
    const faces = (res.faceLandmarks || []).length;
    const W = video.videoWidth, H = video.videoHeight;
    const faceBoxes = (res.faceLandmarks || []).map((lm) => {
      let x0 = 1, y0 = 1, x1 = 0, y1 = 0;
      for (const p of lm) { if (p.x < x0) x0 = p.x; if (p.x > x1) x1 = p.x; if (p.y < y0) y0 = p.y; if (p.y > y1) y1 = p.y; }
      return { x: x0 * W, y: y0 * H, w: (x1 - x0) * W, h: (y1 - y0) * H, label: 'face' };
    });

    // object detection at a lower rate
    this.tickN++;
    let objRan = false;
    if (this.objects && this.tickN % OBJECT_EVERY === 0 && now > this.lastObjTs) {
      this.lastObjTs = now;
      try {
        this.lastObj = classifyObjects(this.objects.detectForVideo(video, now).detections);
        objRan = true;
      } catch (e) { /* skip this round */ }
    }
    const objBoxes = [];
    if (this.lastObj.phone && this.lastObj.phone.box) objBoxes.push({ ...this.lastObj.phone.box, label: 'phone' });
    if (this.lastObj.other && this.lastObj.other.box) objBoxes.push({ ...this.lastObj.other.box, label: this.lastObj.other.label });
    this.lastBoxes = faceBoxes.concat(objBoxes);

    const pose = faces === 1 ? headPose(res.faceLandmarks[0]) : null;

    // ---------------- calibration ----------------
    if (calibrating) {
      let hint = '';
      if (faces === 0) { this.calib.clear(); hint = 'No face detected - move into the camera view and check the lighting.'; }
      else if (faces > 1) { this.calib.clear(); hint = 'More than one face detected - you must be alone.'; }
      else if (!pose) { this.calib.clear(); hint = 'Face found but not clear - face the camera.'; }
      else if (pose.size < CALIB.minFaceSize) { this.calib.clear(); hint = 'Move a little closer to the camera.'; }
      else if (this.phase === 'calibrating') {
        if (!this.calibSeen) { this.calibSeen = true; say && say('face:ok'); }
        const before = this.calib.count;
        this.calib.push(pose);
        hint = this.calib.count <= before && before > 2 ? 'Hold still and look straight at the screen.' : 'Calibrating - hold still and look at the screen…';
        const waited = now - this.calibStart;
        const done = this.calib.complete() || (waited > CALIB.fallbackMs && this.calib.count >= CALIB.fallbackMin);
        if (done) {
          this.base = this.calib.baseline();
          const mc = this.micCalib.slice().sort((x, y) => x - y);
          this.voice.setNoiseFloor(Math.min(0.05, mc.length ? mc[Math.floor(mc.length * 0.8)] : 0));
          this.phase = 'ready';
          say && say('calib:ok');
        }
      }
      this._calibStatus({ faces, hint, size: pose ? Math.round(pose.size * 100) / 100 : 0,
        diag: `detector ${this._fps.v} fps · face ${pose ? Math.round(pose.size * 100) + '%' : '-'} · samples ${this.calib.count}/${CALIB.samples}` });
      return;
    }
    if (!this.monitoring) return;

    // ---------------- monitoring ----------------
    let away = null;
    if (pose && this.base) {
      away = lookingAway(pose, this.base);
      if (!away && res.faceBlendshapes && res.faceBlendshapes[0]) away = gazeAway(res.faceBlendshapes[0].categories);
      this.yawMove.push(now, pose.yaw);
      this.pitchMove.push(now, pose.pitch);
    } else {
      this.yawMove.clear(); this.pitchMove.clear();
    }

    const fire = (type, detail) => this.onViolation(type, detail, this.lastBoxes);

    if (this.rules.no_face.update(faces === 0, now)) fire('no_face', 'No face visible in the camera');
    const multi = faces > 1 || this.lastObj.persons > 1;
    if (this.rules.multiple_faces.update(multi, now)) {
      fire('multiple_faces', faces > 1 ? `${faces} faces detected` : `${this.lastObj.persons} people detected in frame`);
    }
    if (this.rules.looking_away.update(!!away && faces === 1, now)) fire('looking_away', 'Looking away: ' + away);
    const moving = this.yawMove.suspicious() || this.pitchMove.suspicious();
    if (this.rules.head_movement.update(moving, now)) {
      fire('head_movement', 'Repeated rapid head movements');
      this.yawMove.clear(); this.pitchMove.clear();
    }
    const talking = this.voice.active();
    if (this.rules.talking.update(talking, now)) fire('talking', 'Sustained voice / speech detected on microphone');

    if (objRan) {
      if (this.rules.phone.update(!!this.lastObj.phone, now)) {
        fire('phone', `Mobile phone detected (${Math.round(this.lastObj.phone.score * 100)}% confidence)`);
      }
      if (this.rules.object.update(!!this.lastObj.other, now)) {
        fire('object', `${this.lastObj.other.label} detected (${Math.round(this.lastObj.other.score * 100)}% confidence)`);
      }
    }
    this.onStatus({ phase: 'monitoring', faces, away, voice: talking, mic: this.micLevel, phone: !!this.lastObj.phone });
  }

  // ------------------------------------------------------------ evidence
  /** JPEG data-URL of the current camera frame with detection boxes drawn on it. */
  snapshot(extraLabel) {
    const video = this.videos.find((v) => this._videoReady(v));
    if (!video) return null;
    const w = 480, h = Math.round((video.videoHeight / video.videoWidth) * w);
    const c = this.canvas; c.width = w; c.height = h;
    const g = c.getContext('2d');
    try { g.drawImage(video, 0, 0, w, h); } catch (e) { return null; }
    const sx = w / video.videoWidth, sy = h / video.videoHeight;
    g.lineWidth = 2; g.font = '12px sans-serif';
    for (const b of this.lastBoxes) {
      const isFace = b.label === 'face';
      g.strokeStyle = isFace ? '#22c55e' : '#ef4444';
      g.strokeRect(b.x * sx, b.y * sy, b.w * sx, b.h * sy);
      if (!isFace) { g.fillStyle = '#ef4444'; g.fillText(b.label, b.x * sx + 3, Math.max(12, b.y * sy - 4)); }
    }
    g.fillStyle = 'rgba(0,0,0,.55)'; g.fillRect(0, h - 20, w, 20);
    g.fillStyle = '#fff';
    g.fillText(new Date().toLocaleString() + (extraLabel ? '  |  ' + extraLabel : ''), 6, h - 6);
    return c.toDataURL('image/jpeg', 0.6);
  }
}
