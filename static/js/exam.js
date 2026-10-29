// Exam-taking page controller: gate -> start -> answer -> submit, with proctoring.
import { Proctor } from './proctor.js';

const $ = (id) => document.getElementById(id);
const EXAM = window.EXAM;
const state = {
  attemptId: null, questions: [], answers: {}, idx: 0, deadlineMs: 0, offsetMs: 0,
  warnings: 0, submitting: false, started: false, maxViolations: 0,
};
const lastReport = {};
const queue = [];
let flushing = false;
const isFsSupported = !!document.documentElement.requestFullscreen;

// ------------------------------------------------------------- UI helpers
function toast(msg, ms = 4500) {
  const t = $('toast'); t.textContent = msg; t.classList.add('on');
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove('on'), ms);
}
function setStep(id, status) { // run | ok | err
  const li = $(id); li.className = status;
}
function gateError(msg) {
  const b = $('gateMsg'); b.textContent = msg; b.style.display = 'block';
}
function setChip(id, text, cls) { const c = $(id); c.textContent = text; c.className = 'chip ' + (cls || ''); }
function meter(el, level) { el.style.width = Math.min(100, Math.round(level * 900)) + '%'; }

// ----------------------------------------------------------- proctor setup
const proctor = new Proctor({
  onViolation: (type, detail, boxes) => report(type, detail),
  onStatus: onStatus,
  onMediaLost: (where) => {
    if (where === 'gate') {   // camera unplugged while still on the setup screen
      setStep('st-cam', 'err');
      gateError('The camera or microphone was disconnected. Reconnect it and press Retry.');
      $('btnEnable').disabled = false; $('btnEnable').textContent = 'Retry';
      proctor.stop(); proctor.phase = 'idle';
      return;
    }
    if (!state.started || state.submitting) return;
    showBlocker('Camera or microphone lost',
      'Your camera or microphone stopped working. This was recorded. Reconnect it and press the button to continue.',
      'Reconnect', async () => {
        try { await proctor.restartMedia(); proctor.attachVideo($('liveVideo')); hideBlocker(); }
        catch (e) { toast('Still cannot access the camera/microphone.'); }
      });
  },
});

function onStatus(s) {
  if (s.phase === 'calibrating' || s.phase === 'ready') {
    meter($('gateMic'), s.mic || 0);
    $('gateCalib').style.width = Math.round((s.calibProgress || 0) * 100) + '%';
    if (s.faces > 1) setStep('st-face', 'err'); else if (s.faces === 1) setStep('st-face', 'ok'); else setStep('st-face', 'run');
    const hint = $('gateHint');
    if (s.phase === 'ready') { hint.textContent = '✓ Calibration complete - you can start the exam.'; hint.className = 'small center ok'; }
    else { hint.textContent = s.hint || ''; hint.className = 'small center'; }
    $('gateDiag').textContent = s.diag || '';
    $('btnRecalib').style.display = (s.phase === 'ready' || s.stuckMs > 12000) ? '' : 'none';
    if (s.error) gateError(s.error); else $('gateMsg').style.display = 'none';
    return;
  }
  meter($('liveMic'), s.mic || 0);
  setChip('chipFace', s.faces === 1 ? 'Face: OK' : (s.faces === 0 ? 'Face: not visible' : 'Faces: ' + s.faces), s.faces === 1 ? 'ok' : 'bad');
  setChip('chipGaze', s.away ? 'Looking away' : 'Gaze: OK', s.away ? 'warn' : 'ok');
  setChip('chipMic', s.voice ? 'Voice detected' : 'Mic: quiet', s.voice ? 'warn' : 'ok');
}

// ------------------------------------------------------------------ gate
function refreshStartButton() {
  $('btnStart').disabled = !(proctor.phase === 'ready' && $('consent').checked);
}
$('consent').addEventListener('change', refreshStartButton);

$('btnEnable').addEventListener('click', async () => {
  $('btnEnable').disabled = true;
  $('gateMsg').style.display = 'none';
  proctor.attachVideo($('gateVideo'));
  try {
    await proctor.start((p) => {
      const map = { 'camera:run': ['st-cam', 'run'], 'camera:ok': ['st-cam', 'ok'], 'models:run': ['st-model', 'run'],
        'models:ok': ['st-model', 'ok'], 'face:run': ['st-face', 'run'], 'face:ok': ['st-face', 'ok'], 'calib:ok': ['st-calib', 'ok'] };
      if (map[p]) setStep(...map[p]);
      if (p === 'face:run') setStep('st-calib', 'run');
      if (p === 'calib:ok') refreshStartButton();
    });
  } catch (e) {
    setStep('st-cam', e.code ? 'ok' : 'err');
    let msg = e.message || 'Something went wrong.';
    if (e.name === 'NotAllowedError') msg = 'Permission denied. Allow camera and microphone access in your browser (lock icon in the address bar) and try again.';
    else if (e.name === 'NotFoundError') msg = 'No camera or microphone was found on this device.';
    else if (e.name === 'NotReadableError') msg = 'Your camera is being used by another application. Close it and try again.';
    else if (!window.isSecureContext) msg = 'Camera access requires a secure (HTTPS) connection.';
    gateError(msg);
    if (e.code) setStep('st-model', 'err');
    proctor.stop();
    $('btnEnable').disabled = false;
    $('btnEnable').textContent = 'Retry';
    // allow a fresh Proctor run after failure
    proctor.phase = 'idle';
  }
});

$('btnRecalib').addEventListener('click', () => {
  proctor.recalibrate();
  setStep('st-calib', 'run');
  $('btnStart').disabled = true;
  $('btnRecalib').style.display = 'none';
});

$('btnStart').addEventListener('click', async () => {
  $('btnStart').disabled = true;
  // fullscreen must be requested synchronously inside the click gesture
  let fsPromise = null;
  if (isFsSupported) fsPromise = document.documentElement.requestFullscreen().catch(() => {});
  const res = await window.api(`/api/exams/${EXAM.id}/start`, {});
  if (fsPromise) await fsPromise;
  if (!res.ok) {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    if (res.data.submitted) { window.location.href = '/student'; return; }
    gateError(res.data.error || 'Could not start the exam.');
    $('btnStart').disabled = false;
    return;
  }
  const d = res.data;
  state.attemptId = d.attempt_id;
  state.questions = d.questions;
  state.answers = d.answers || {};
  state.maxViolations = d.max_violations;
  state.warnings = d.violations || 0;
  state.deadlineMs = new Date(d.deadline).getTime();
  state.offsetMs = new Date(d.server_now).getTime() - Date.now();
  state.started = true;

  $('gate').classList.remove('on');
  $('examWrap').style.visibility = 'visible';
  proctor.attachVideo($('liveVideo'));
  proctor.beginMonitoring();
  $('btnSubmit').disabled = false;
  updateWarn();
  buildPalette(); showQuestion(0);
  startTimer();
  installGuards();
});

// ---------------------------------------------------------------- questions
function buildPalette() {
  const p = $('palette'); p.innerHTML = '';
  state.questions.forEach((q, i) => {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = i + 1; b.dataset.i = i;
    b.addEventListener('click', () => showQuestion(i));
    p.appendChild(b);
  });
  refreshPalette();
}
function refreshPalette() {
  Array.from($('palette').children).forEach((b, i) => {
    const q = state.questions[i];
    const a = state.answers[q.id];
    b.className = (a !== undefined && String(a).trim() !== '' ? 'done ' : '') + (i === state.idx ? 'cur' : '');
  });
}
const TYPE_LABEL = { mcq: 'Multiple choice', numeric: 'Numerical', text: 'Descriptive' };

function showQuestion(i) {
  state.idx = i;
  const q = state.questions[i];
  $('qMeta').textContent = `Question ${i + 1} of ${state.questions.length} · ${TYPE_LABEL[q.qtype]}`;
  $('qMarks').textContent = `${q.marks} mark${q.marks === 1 ? '' : 's'}`;
  $('qText').textContent = q.text;
  const host = $('qInput'); host.innerHTML = '';
  const cur = state.answers[q.id] !== undefined ? String(state.answers[q.id]) : '';

  if (q.qtype === 'mcq') {
    q.options.forEach((opt, oi) => {
      const lab = document.createElement('label'); lab.className = 'q-opt';
      const r = document.createElement('input'); r.type = 'radio'; r.name = 'opt'; r.value = oi;
      r.checked = cur === String(oi);
      r.addEventListener('change', () => setAnswer(q, String(oi), true));
      const span = document.createElement('span'); span.textContent = opt;
      lab.append(r, span); host.appendChild(lab);
    });
  } else if (q.qtype === 'numeric') {
    const inp = document.createElement('input');
    inp.type = 'text'; inp.inputMode = 'decimal'; inp.placeholder = 'Enter a number'; inp.autocomplete = 'off';
    inp.style.maxWidth = '280px'; inp.value = cur;
    inp.addEventListener('input', () => setAnswer(q, inp.value));
    host.appendChild(inp);
  } else {
    const ta = document.createElement('textarea');
    ta.placeholder = 'Type your answer here…'; ta.maxLength = 10000; ta.style.minHeight = '260px'; ta.value = cur;
    ta.spellcheck = true;
    ta.addEventListener('input', () => setAnswer(q, ta.value));
    host.appendChild(ta);
  }
  $('btnPrev').disabled = i === 0;
  $('btnNext').disabled = i === state.questions.length - 1;
  refreshPalette();
}
$('btnPrev').addEventListener('click', () => showQuestion(state.idx - 1));
$('btnNext').addEventListener('click', () => showQuestion(state.idx + 1));

const saveTimers = {};
function setAnswer(q, value, immediate) {
  state.answers[q.id] = value;
  refreshPalette();
  $('saveState').textContent = 'Saving…';
  clearTimeout(saveTimers[q.id]);
  saveTimers[q.id] = setTimeout(() => saveAnswer(q.id), immediate ? 0 : 700);
}
async function saveAnswer(qid, tries = 0) {
  if (state.submitting) return;
  try {
    const res = await window.api(`/api/attempts/${state.attemptId}/answer`, { question_id: qid, answer: state.answers[qid] });
    if (res.status === 409 && res.data.submitted) return finish(true);
    $('saveState').textContent = res.ok ? 'Saved ✓' : 'Not saved - retrying…';
    if (!res.ok && tries < 5) setTimeout(() => saveAnswer(qid, tries + 1), 2000);
  } catch (e) {
    $('saveState').textContent = 'Offline - retrying…';
    if (tries < 30) setTimeout(() => saveAnswer(qid, tries + 1), 3000);
  }
}

// -------------------------------------------------------------------- timer
function startTimer() {
  const el = $('timer');
  const tick = () => {
    const left = Math.max(0, Math.round((state.deadlineMs - (Date.now() + state.offsetMs)) / 1000));
    const h = Math.floor(left / 3600), m = Math.floor((left % 3600) / 60), s = left % 60;
    const p = (n) => String(n).padStart(2, '0');
    el.textContent = (h ? h + ':' : '') + p(m) + ':' + p(s);
    el.classList.toggle('low', left <= 300);
    if (left <= 0 && !state.submitting) { toast('Time is up - submitting your exam…', 8000); submitExam(); }
  };
  tick(); state.timer = setInterval(tick, 500);
}

// ------------------------------------------------------------------- submit
$('btnSubmit').addEventListener('click', () => {
  const answered = state.questions.filter((q) => String(state.answers[q.id] ?? '').trim() !== '').length;
  const left = state.questions.length - answered;
  $('confirmMsg').textContent = left
    ? `You have ${left} unanswered question(s). Once submitted you cannot change your answers.`
    : 'You have answered all questions. Once submitted you cannot change your answers.';
  $('confirmBox').classList.add('on');
});
$('confirmNo').addEventListener('click', () => $('confirmBox').classList.remove('on'));
$('confirmYes').addEventListener('click', () => { $('confirmBox').classList.remove('on'); submitExam(); });

async function submitExam() {
  if (state.submitting) return;
  state.submitting = true;
  clearInterval(state.timer);
  $('btnSubmit').disabled = true;
  $('saveState').textContent = 'Submitting…';
  for (let i = 0; i < 40; i++) {
    try {
      const res = await window.api(`/api/attempts/${state.attemptId}/submit`, { answers: state.answers });
      if (res.ok || (res.status === 409 && res.data.submitted)) return finish(false, res.data.redirect);
    } catch (e) { /* network down - retry */ }
    $('saveState').textContent = 'Connection problem - retrying…';
    await new Promise((r) => setTimeout(r, 3000));
  }
  state.submitting = false; $('btnSubmit').disabled = false;
  toast('Could not submit. Check your connection and press Submit again.', 8000);
}

function finish(already, redirect) {
  state.submitting = true;
  window.onbeforeunload = null;
  proctor.stop();
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  window.location.href = redirect || `/student/attempts/${state.attemptId}`;
}

// -------------------------------------------------------------- violations
const LABELS = {
  no_face: 'Face not visible', multiple_faces: 'Multiple people detected', looking_away: 'Looking away from the screen',
  head_movement: 'Suspicious head movement', talking: 'Talking detected', phone: 'Mobile phone detected',
  object: 'Unauthorized object detected', tab_switch: 'Tab / window switch', fullscreen_exit: 'Fullscreen exited',
  copy_paste: 'Copy / paste / shortcut attempt', camera_off: 'Camera or microphone lost',
};
const BROWSER_EVENTS = new Set(['tab_switch', 'fullscreen_exit', 'copy_paste']);

function updateWarn() {
  $('warnCount').textContent = '⚠ ' + state.warnings + ' warning' + (state.warnings === 1 ? '' : 's') +
    (state.maxViolations ? ' (limit ' + state.maxViolations + ')' : '');
}

function report(type, detail) {
  if (!state.started || state.submitting) return;
  const now = Date.now();
  if (BROWSER_EVENTS.has(type) && lastReport[type] && now - lastReport[type] < 4000) return;
  lastReport[type] = now;
  const shot = proctor.snapshot(LABELS[type]);
  queue.push({ type, detail, screenshot: shot });
  state.warnings += 1; updateWarn();
  toast('⚠ ' + (LABELS[type] || type) + ' - this has been recorded.');
  flush();
}

async function flush() {
  if (flushing) return;
  flushing = true;
  while (queue.length && !state.submitting) {
    const item = queue[0];
    try {
      const res = await window.api(`/api/attempts/${state.attemptId}/violation`, item);
      if (res.status === 409 && res.data.submitted) { queue.length = 0; return finish(true); }
      if (res.ok || (res.status >= 400 && res.status < 500)) queue.shift(); // 4xx = won't succeed on retry
      else throw new Error('server');
      if (res.ok && typeof res.data.count === 'number') { state.warnings = Math.max(state.warnings, res.data.count); updateWarn(); }
      if (res.ok && res.data.terminated) {
        toast('Violation limit reached - your exam has been ended.', 8000);
        queue.length = 0;
        setTimeout(() => finish(true), 1500);
        return;
      }
    } catch (e) {
      await new Promise((r) => setTimeout(r, 4000)); // keep item, retry
    }
  }
  flushing = false;
}

// --------------------------------------------------------------- guard rails
function showBlocker(title, msg, btnText, onClick) {
  $('blockTitle').textContent = title; $('blockMsg').textContent = msg;
  const b = $('blockBtn'); b.textContent = btnText; b.onclick = onClick;
  $('blocker').classList.add('on');
}
function hideBlocker() { $('blocker').classList.remove('on'); }

function installGuards() {
  window.onbeforeunload = (e) => { if (!state.submitting) { e.preventDefault(); e.returnValue = ''; } };

  document.addEventListener('visibilitychange', () => { if (document.hidden) report('tab_switch', 'Exam tab was hidden or switched'); });
  window.addEventListener('blur', () => { if (!document.hidden) report('tab_switch', 'Exam window lost focus'); });

  if (isFsSupported) {
    document.addEventListener('fullscreenchange', () => {
      if (!document.fullscreenElement && state.started && !state.submitting) {
        report('fullscreen_exit', 'Left fullscreen mode');
        showBlocker('Return to fullscreen', 'This exam must be taken in fullscreen. The exit was recorded.', 'Continue exam',
          () => document.documentElement.requestFullscreen().then(hideBlocker).catch(() => toast('Press F11 or allow fullscreen.')));
      }
    });
  }

  ['copy', 'cut', 'paste'].forEach((ev) => document.addEventListener(ev, (e) => {
    e.preventDefault(); report('copy_paste', ev.charAt(0).toUpperCase() + ev.slice(1) + ' attempt blocked');
  }));
  document.addEventListener('contextmenu', (e) => e.preventDefault());
  document.addEventListener('keydown', (e) => {
    const k = e.key.toLowerCase(), mod = e.ctrlKey || e.metaKey;
    const blocked = (mod && ['c', 'v', 'x', 'p', 's', 'u', 'f'].includes(k)) ||
      (mod && e.shiftKey && ['i', 'j', 'c', 'k'].includes(k)) || e.key === 'F12';
    if (blocked) {
      e.preventDefault();
      report('copy_paste', 'Blocked shortcut: ' + (mod ? 'Ctrl+' : '') + (e.shiftKey ? 'Shift+' : '') + e.key.toUpperCase());
    }
  });
}
