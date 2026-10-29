// Pure decision logic for the proctoring engine (no DOM / camera access),
// so it can be unit-tested in Node. Tune the thresholds in THRESHOLDS below.

export const THRESHOLDS = {
  // head pose: deviation from the student's calibrated baseline
  yawDelta: 0.13,        // left/right turn (ratio units, ~0.5 when facing the screen)
  pitchDelta: 0.14,      // up/down tilt
  gazeHorizontal: 0.6,   // blendshape score for eyes looking sideways
  gazeDown: 0.7,         // eyes looking far down (phone / notes in lap)
  // rapid head movement: zig-zags within a sliding window
  moveWindowMs: 6000,
  moveSwing: 0.07,
  moveReversals: 3,
  // voice
  voiceMinRms: 0.035,
  voiceNoiseFactor: 3,
  voiceWindowMs: 3000,
  voiceRatio: 0.55,
  voiceMinSamples: 8,
};

// calibration: the baseline is the longest recent run of frames in which the head was still
export const CALIB = {
  samples: 12,        // stable frames needed (~3 s at 4 fps)
  maxRange: 0.07,     // max spread of yaw / pitch inside the stable window
  minFaceSize: 0.08,  // face width as a fraction of the frame; smaller = too far away
  fallbackMs: 25000,  // after this long, accept a shorter stable run instead of waiting forever
  fallbackMin: 6,
};

/** Keeps the most recent *stable* run of head-pose samples; the median becomes the baseline. */
export class CalibrationBuffer {
  constructor(t = CALIB) { this.t = t; this.buf = []; }
  clear() { this.buf = []; }
  _range(k) {
    let lo = Infinity, hi = -Infinity;
    for (const p of this.buf) { if (p[k] < lo) lo = p[k]; if (p[k] > hi) hi = p[k]; }
    return hi - lo;
  }
  push(pose) {
    this.buf.push({ yaw: pose.yaw, pitch: pose.pitch });
    // head moved too much: forget the oldest samples until the window is still again
    while (this.buf.length > 1 && (this._range('yaw') > this.t.maxRange || this._range('pitch') > this.t.maxRange)) this.buf.shift();
    if (this.buf.length > this.t.samples) this.buf.shift();
  }
  get count() { return this.buf.length; }
  progress() { return Math.min(1, this.buf.length / this.t.samples); }
  complete() { return this.buf.length >= this.t.samples; }
  baseline() {
    if (!this.buf.length) return null;
    const med = (k) => { const a = this.buf.map((p) => p[k]).sort((x, y) => x - y); return a[Math.floor(a.length / 2)]; };
    return { yaw: med('yaw'), pitch: med('pitch') };
  }
}

/** Fires once `holdMs` of continuous activity has passed, then waits `cooldownMs`. */
export class Sustained {
  constructor(holdMs, cooldownMs) {
    this.holdMs = holdMs;
    this.cooldownMs = cooldownMs;
    this.since = null;
    this.lastFire = -Infinity;
  }
  update(active, now) {
    if (!active) { this.since = null; return false; }
    if (this.since === null) this.since = now;
    if (now - this.since >= this.holdMs && now - this.lastFire >= this.cooldownMs) {
      this.lastFire = now;
      return true;
    }
    return false;
  }
  reset() { this.since = null; }
}

/**
 * Head pose from MediaPipe face-mesh landmarks (normalised x/y).
 * yaw  = where the nose sits between the two cheek edges (0.5 = frontal)
 * pitch = where the nose sits between forehead and chin
 * Ratios are mirror-invariant and independent of the matrix conventions.
 */
export function headPose(lm) {
  if (!lm || lm.length < 455) return null;
  const nose = lm[1], left = lm[234], right = lm[454], top = lm[10], chin = lm[152];
  const w = right.x - left.x;
  const h = chin.y - top.y;
  if (Math.abs(w) < 1e-4 || Math.abs(h) < 1e-4) return null;
  return {
    yaw: (nose.x - left.x) / w,
    pitch: (nose.y - top.y) / h,
    size: Math.abs(w),
  };
}

export function lookingAway(pose, base, t = THRESHOLDS) {
  if (!pose || !base) return null;
  const dy = pose.yaw - base.yaw, dp = pose.pitch - base.pitch;
  if (Math.abs(dy) > t.yawDelta) return dy > 0 ? 'head turned to one side' : 'head turned to the other side';
  if (Math.abs(dp) > t.pitchDelta) return dp > 0 ? 'head tilted down' : 'head tilted up';
  return null;
}

/** blendshapes: array of {categoryName, score}. Returns a reason string or null. */
export function gazeAway(categories, t = THRESHOLDS) {
  if (!categories) return null;
  const m = {};
  for (const c of categories) m[c.categoryName] = c.score;
  const g = (k) => m[k] || 0;
  const lookRight = (g('eyeLookOutRight') + g('eyeLookInLeft')) / 2;
  const lookLeft = (g('eyeLookOutLeft') + g('eyeLookInRight')) / 2;
  if (Math.max(lookRight, lookLeft) > t.gazeHorizontal) return 'eyes looking sideways';
  if ((g('eyeLookDownLeft') + g('eyeLookDownRight')) / 2 > t.gazeDown) return 'eyes looking down';
  return null;
}

/** Detects repeated left-right (or up-down) swings of a scalar signal. */
export class MovementAnalyzer {
  constructor(t = THRESHOLDS) {
    this.t = t;
    this.samples = [];
  }
  push(now, value) {
    this.samples.push([now, value]);
    const cutoff = now - this.t.moveWindowMs;
    while (this.samples.length && this.samples[0][0] < cutoff) this.samples.shift();
  }
  reversals() {
    const T = this.t.moveSwing;
    if (this.samples.length < 4) return 0;
    let ext = this.samples[0][1], dir = 0, rev = 0;
    for (const [, v] of this.samples) {
      if (dir === 0) {
        if (v - ext > T) { dir = 1; ext = v; } else if (ext - v > T) { dir = -1; ext = v; }
      } else if (dir === 1) {
        if (v > ext) ext = v; else if (ext - v > T) { dir = -1; ext = v; rev++; }
      } else {
        if (v < ext) ext = v; else if (v - ext > T) { dir = 1; ext = v; rev++; }
      }
    }
    return rev;
  }
  suspicious() { return this.reversals() >= this.t.moveReversals; }
  clear() { this.samples = []; }
}

/** Sliding-window voice-activity estimate from microphone RMS samples. */
export class VoiceActivity {
  constructor(t = THRESHOLDS) {
    this.t = t;
    this.samples = [];
    this.noiseFloor = 0;
  }
  setNoiseFloor(v) { this.noiseFloor = v; }
  threshold() { return Math.max(this.t.voiceMinRms, this.noiseFloor * this.t.voiceNoiseFactor); }
  push(now, rms) {
    this.samples.push([now, rms > this.threshold()]);
    const cutoff = now - this.t.voiceWindowMs;
    while (this.samples.length && this.samples[0][0] < cutoff) this.samples.shift();
  }
  active() {
    if (this.samples.length < this.t.voiceMinSamples) return false;
    const on = this.samples.reduce((n, s) => n + (s[1] ? 1 : 0), 0);
    return on / this.samples.length >= this.t.voiceRatio;
  }
}

export const UNAUTHORIZED_OBJECTS = new Set(['book', 'laptop', 'remote', 'scissors']);
export const PHONE_LABEL = 'cell phone';

/** Classify object-detector output (MediaPipe `detections`). */
export function classifyObjects(detections, minScore = 0.45) {
  let phone = null, other = null, persons = 0;
  for (const d of detections || []) {
    const c = d.categories && d.categories[0];
    if (!c) continue;
    const box = d.boundingBox
      ? { x: d.boundingBox.originX, y: d.boundingBox.originY, w: d.boundingBox.width, h: d.boundingBox.height }
      : null;
    if (c.categoryName === PHONE_LABEL && c.score >= minScore) {
      if (!phone || c.score > phone.score) phone = { label: 'mobile phone', score: c.score, box };
    } else if (UNAUTHORIZED_OBJECTS.has(c.categoryName) && c.score >= Math.max(minScore, 0.5)) {
      if (!other || c.score > other.score) other = { label: c.categoryName, score: c.score, box };
    } else if (c.categoryName === 'person' && c.score >= 0.55) {
      persons++;
    }
  }
  return { phone, other, persons };
}
