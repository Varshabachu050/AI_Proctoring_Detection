import assert from 'node:assert/strict';
import { Sustained, headPose, lookingAway, gazeAway, MovementAnalyzer, VoiceActivity, classifyObjects, THRESHOLDS, CalibrationBuffer, CALIB } from '../static/js/detectors.js';

// --- Sustained
{
  const s = new Sustained(1000, 5000);
  assert.equal(s.update(true, 0), false);
  assert.equal(s.update(true, 900), false);
  assert.equal(s.update(true, 1000), true, 'fires after hold');
  assert.equal(s.update(true, 2000), false, 'cooldown blocks');
  assert.equal(s.update(false, 2500), false);
  assert.equal(s.update(true, 3000), false, 'since reset, not held yet');
  assert.equal(s.update(true, 6100), true, 'fires again after cooldown+hold');
}
// --- headPose / lookingAway
function mkLm(noseX, noseY) {
  const lm = Array.from({ length: 478 }, () => ({ x: 0.5, y: 0.5, z: 0 }));
  lm[234] = { x: 0.3, y: 0.5 }; lm[454] = { x: 0.7, y: 0.5 };
  lm[10] = { x: 0.5, y: 0.2 }; lm[152] = { x: 0.5, y: 0.8 };
  lm[1] = { x: noseX, y: noseY };
  return lm;
}
{
  const base = headPose(mkLm(0.5, 0.52));
  assert.ok(Math.abs(base.yaw - 0.5) < 1e-9);
  assert.equal(lookingAway(headPose(mkLm(0.51, 0.53)), base), null, 'small jitter ok');
  assert.ok(lookingAway(headPose(mkLm(0.40, 0.52)), base), 'turned one way');
  assert.ok(lookingAway(headPose(mkLm(0.60, 0.52)), base), 'turned other way (mirror)');
  assert.ok(lookingAway(headPose(mkLm(0.5, 0.65)), base), 'looking down');
  assert.equal(headPose(null), null);
}
// --- gaze
{
  assert.equal(gazeAway([{ categoryName: 'eyeLookOutLeft', score: 0.1 }]), null);
  assert.ok(gazeAway([{ categoryName: 'eyeLookOutLeft', score: 0.9 }, { categoryName: 'eyeLookInRight', score: 0.8 }]));
  assert.ok(gazeAway([{ categoryName: 'eyeLookDownLeft', score: 0.9 }, { categoryName: 'eyeLookDownRight', score: 0.9 }]));
}
// --- movement
{
  const m = new MovementAnalyzer();
  // steady head: no reversals
  for (let t = 0; t < 5000; t += 200) m.push(t, 0.5 + (Math.random() - 0.5) * 0.01);
  assert.equal(m.suspicious(), false, 'steady head not suspicious');
  // slow single turn and back: only 1 reversal
  m.clear();
  [0.5, 0.5, 0.6, 0.6, 0.5, 0.5].forEach((v, i) => m.push(i * 400, v));
  assert.equal(m.suspicious(), false, 'one turn-and-back not suspicious');
  // rapid shaking left-right
  m.clear();
  const seq = [0.5, 0.6, 0.4, 0.6, 0.4, 0.6, 0.4];
  seq.forEach((v, i) => m.push(i * 500, v));
  assert.equal(m.suspicious(), true, 'repeated swings flagged');
  // old samples fall out of window
  m.push(20000, 0.5);
  assert.equal(m.samples.length, 1);
}
// --- voice
{
  const v = new VoiceActivity();
  v.setNoiseFloor(0.01);
  for (let t = 0; t < 3000; t += 100) v.push(t, 0.012);
  assert.equal(v.active(), false, 'silence');
  const v2 = new VoiceActivity(); v2.setNoiseFloor(0.01);
  for (let t = 0; t < 3000; t += 100) v2.push(t, 0.08);
  assert.equal(v2.active(), true, 'sustained speech');
  const v3 = new VoiceActivity(); v3.setNoiseFloor(0.01);
  for (let t = 0; t < 3000; t += 100) v3.push(t, t === 1000 ? 0.2 : 0.01);
  assert.equal(v3.active(), false, 'single cough/click not talking');
  const v4 = new VoiceActivity(); v4.setNoiseFloor(0.05); // noisy room raises bar
  assert.ok(Math.abs(v4.threshold() - 0.15) < 1e-9);
  const v5 = new VoiceActivity();
  for (let t = 0; t < 300; t += 100) v5.push(t, 0.5);
  assert.equal(v5.active(), false, 'too few samples');
}
// --- objects
{
  const det = (name, score) => ({ categories: [{ categoryName: name, score }], boundingBox: { originX: 1, originY: 2, width: 3, height: 4 } });
  let r = classifyObjects([det('cell phone', 0.7), det('book', 0.8), det('person', 0.9), det('person', 0.8)]);
  assert.equal(r.phone.label, 'mobile phone'); assert.equal(r.other.label, 'book'); assert.equal(r.persons, 2);
  r = classifyObjects([det('cell phone', 0.3), det('keyboard', 0.9), det('cup', 0.9)]);
  assert.equal(r.phone, null); assert.equal(r.other, null); assert.equal(r.persons, 0);
  assert.deepEqual(classifyObjects(null), { phone: null, other: null, persons: 0 });
}
// --- calibration buffer
{
  const cb = new CalibrationBuffer();
  for (let i = 0; i < CALIB.samples - 1; i++) cb.push({ yaw: 0.5 + (i % 2) * 0.01, pitch: 0.5 });
  assert.equal(cb.complete(), false, 'not enough samples yet');
  cb.push({ yaw: 0.5, pitch: 0.5 });
  assert.equal(cb.complete(), true, 'complete after N stable samples');
  assert.ok(Math.abs(cb.baseline().yaw - 0.5) < 0.02);
  cb.push({ yaw: 0.8, pitch: 0.5 });          // head jerks away: old samples are dropped
  assert.equal(cb.count, 1);
  assert.equal(cb.complete(), false, 'progress restarts after movement');
  cb.clear(); assert.equal(cb.baseline(), null);
}
console.log('detectors: all assertions passed');
