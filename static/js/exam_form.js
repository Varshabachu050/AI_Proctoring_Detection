// Exam builder: dynamic MCQ / numerical / descriptive question editor.
(function () {
  const init = JSON.parse(document.getElementById('init-data').textContent);
  const $ = (id) => document.getElementById(id);
  const locked = !!init.locked;
  let questions = (init.questions || []).map((q) => Object.assign({}, q, { options: (q.options || []).slice() }));

  // ---- helpers
  function toLocalInput(isoZ) {
    if (!isoZ) return '';
    const d = new Date(isoZ);
    if (isNaN(d)) return '';
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function toUtcIso(localValue) {
    if (!localValue) return '';
    const d = new Date(localValue);
    return isNaN(d) ? '' : d.toISOString().slice(0, 19);
  }
  function el(tag, attrs, children) {
    const e = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (k === 'class') e.className = v;
      else if (k === 'text') e.textContent = v;
      else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
      else if (v !== false && v !== null && v !== undefined) e.setAttribute(k, v === true ? '' : v);
    });
    (children || []).forEach((c) => e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c));
    return e;
  }
  function newQuestion(type) {
    if (type === 'mcq') return { qtype: 'mcq', text: '', marks: 1, options: ['', '', '', ''], correct: 0 };
    if (type === 'numeric') return { qtype: 'numeric', text: '', marks: 1, correct: '', tolerance: 0, options: [] };
    return { qtype: 'text', text: '', marks: 5, correct: '', options: [] };
  }
  const LABEL = { mcq: 'Multiple choice', numeric: 'Numerical', text: 'Descriptive' };

  // ---- render
  function render() {
    const host = $('questions');
    host.innerHTML = '';
    if (!questions.length) {
      host.appendChild(el('p', { class: 'muted small', style: 'margin-top:12px', text: 'No questions yet - use the buttons above to add one.' }));
    }
    questions.forEach((q, i) => host.appendChild(renderQuestion(q, i)));
    const total = questions.reduce((s, q) => s + (parseFloat(q.marks) || 0), 0);
    $('qSummary').textContent = '· ' + questions.length + ' question(s), ' + total + ' marks';
  }

  function renderQuestion(q, i) {
    const card = el('div', { class: 'qcard' });
    const head = el('div', { class: 'qhead' }, [
      el('b', { text: 'Q' + (i + 1) + ' · ' + LABEL[q.qtype] }),
      el('div', { class: 'row' }, [
        el('button', { type: 'button', class: 'btn ghost sm', text: '↑', disabled: locked || i === 0, onclick: () => move(i, -1) }),
        el('button', { type: 'button', class: 'btn ghost sm', text: '↓', disabled: locked || i === questions.length - 1, onclick: () => move(i, 1) }),
        el('button', { type: 'button', class: 'btn danger sm', text: 'Delete', disabled: locked, onclick: () => { questions.splice(i, 1); render(); } }),
      ]),
    ]);
    card.appendChild(head);

    const text = el('textarea', { placeholder: 'Question text', disabled: locked, style: 'min-height:70px;margin-top:10px' });
    text.value = q.text;
    text.addEventListener('input', () => (q.text = text.value));
    card.appendChild(text);

    const marks = el('input', { type: 'number', min: '0.5', step: '0.5', disabled: locked, style: 'width:120px' });
    marks.value = q.marks;
    marks.addEventListener('input', () => { q.marks = marks.value; updateSummary(); });
    card.appendChild(el('label', { text: 'Marks' }));
    card.appendChild(marks);

    if (q.qtype === 'mcq') {
      card.appendChild(el('label', { text: 'Options (select the correct one)' }));
      q.options.forEach((opt, oi) => {
        const radio = el('input', { type: 'radio', name: 'correct_' + i, disabled: locked });
        radio.checked = Number(q.correct) === oi;
        radio.addEventListener('change', () => (q.correct = oi));
        const input = el('input', { type: 'text', placeholder: 'Option ' + String.fromCharCode(65 + oi), disabled: locked, maxlength: '300' });
        input.value = opt;
        input.addEventListener('input', () => (q.options[oi] = input.value));
        const row = el('div', { class: 'opt' }, [radio, input]);
        if (q.options.length > 2 && !locked) {
          row.appendChild(el('button', { type: 'button', class: 'btn ghost sm', text: '✕', onclick: () => {
            q.options.splice(oi, 1);
            if (Number(q.correct) >= q.options.length) q.correct = 0;
            else if (Number(q.correct) > oi) q.correct = Number(q.correct) - 1;
            render();
          } }));
        }
        card.appendChild(row);
      });
      if (q.options.length < 6 && !locked) {
        card.appendChild(el('button', { type: 'button', class: 'btn ghost sm', style: 'margin-top:10px', text: '+ Add option', onclick: () => { q.options.push(''); render(); } }));
      }
    } else if (q.qtype === 'numeric') {
      const ans = el('input', { type: 'text', inputmode: 'decimal', placeholder: 'e.g. 9.81', disabled: locked, style: 'width:200px' });
      ans.value = q.correct;
      ans.addEventListener('input', () => (q.correct = ans.value));
      const tol = el('input', { type: 'number', step: 'any', min: '0', disabled: locked, style: 'width:200px' });
      tol.value = q.tolerance || 0;
      tol.addEventListener('input', () => (q.tolerance = tol.value));
      card.appendChild(el('div', { class: 'row', style: 'align-items:flex-start;gap:20px' }, [
        el('div', {}, [el('label', { text: 'Correct answer' }), ans]),
        el('div', {}, [el('label', { text: 'Accepted tolerance (±)' }), tol, el('div', { class: 'hint', text: '0 = exact match required' })]),
      ]));
    } else {
      const ref = el('textarea', { placeholder: 'Optional model answer / marking notes (only you will see this)', disabled: locked, style: 'min-height:70px' });
      ref.value = q.correct || '';
      ref.addEventListener('input', () => (q.correct = ref.value));
      card.appendChild(el('label', { text: 'Model answer (optional)' }));
      card.appendChild(ref);
      card.appendChild(el('div', { class: 'hint', text: 'Descriptive answers are graded manually by you after submission.' }));
    }
    return card;
  }

  function updateSummary() {
    const total = questions.reduce((s, q) => s + (parseFloat(q.marks) || 0), 0);
    $('qSummary').textContent = '· ' + questions.length + ' question(s), ' + total + ' marks';
  }
  function move(i, d) {
    const j = i + d;
    if (j < 0 || j >= questions.length) return;
    [questions[i], questions[j]] = [questions[j], questions[i]];
    render();
  }

  // ---- students
  const assigned = new Set((init.student_ids || []).map(Number));
  document.querySelectorAll('.stu').forEach((cb) => {
    cb.checked = assigned.has(Number(cb.value));
    cb.addEventListener('change', countStudents);
  });
  function visibleStudents() { return Array.from(document.querySelectorAll('#stuList label')).filter((l) => l.style.display !== 'none'); }
  function countStudents() {
    $('stuCount').textContent = '· ' + document.querySelectorAll('.stu:checked').length + ' selected';
  }
  $('stuFilter').addEventListener('input', (e) => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll('#stuList label').forEach((l) => { l.style.display = l.dataset.s.includes(q) ? '' : 'none'; });
  });
  $('stuAll').addEventListener('click', () => {
    const vis = visibleStudents();
    const allOn = vis.every((l) => l.querySelector('input').checked);
    vis.forEach((l) => (l.querySelector('input').checked = !allOn));
    countStudents();
  });

  // ---- load initial values
  $('title').value = init.title || '';
  $('desc').value = init.description || '';
  $('duration').value = init.duration_min || 60;
  $('maxv').value = init.max_violations || 0;
  if (init.start_at) { $('start').value = toLocalInput(init.start_at); $('end').value = toLocalInput(init.end_at); }
  else {
    const now = new Date(); now.setMinutes(now.getMinutes() + 10, 0, 0);
    const end = new Date(now.getTime() + 24 * 3600 * 1000);
    const p = (n) => String(n).padStart(2, '0');
    const f = (d) => d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
    $('start').value = f(now); $('end').value = f(end);
  }
  document.querySelectorAll('[data-add]').forEach((b) => {
    if (locked) { b.disabled = true; return; }
    b.addEventListener('click', () => { questions.push(newQuestion(b.dataset.add)); render(); });
  });
  if (!questions.length && !locked) questions.push(newQuestion('mcq'));
  render(); countStudents();

  // ---- save
  async function save(publish) {
    const box = $('errors');
    box.style.display = 'none';
    const payload = {
      title: $('title').value, description: $('desc').value,
      duration_min: $('duration').value, start_at: toUtcIso($('start').value), end_at: toUtcIso($('end').value),
      max_violations: $('maxv').value, published: publish,
      student_ids: Array.from(document.querySelectorAll('.stu:checked')).map((c) => Number(c.value)),
      questions: questions.map((q) => ({
        qtype: q.qtype, text: q.text, marks: q.marks, options: q.options, correct: q.correct, tolerance: q.tolerance,
      })),
    };
    const btns = [$('btnPublish'), $('btnDraft')];
    btns.forEach((b) => (b.disabled = true));
    const res = await window.api(window.SAVE_URL, payload);
    if (res.ok && res.data.redirect) { window.location.href = res.data.redirect; return; }
    btns.forEach((b) => (b.disabled = false));
    const errs = res.data.errors || [res.data.error || 'Could not save the exam.'];
    box.innerHTML = '';
    const ul = document.createElement('ul');
    errs.forEach((e) => { const li = document.createElement('li'); li.textContent = e; ul.appendChild(li); });
    box.appendChild(ul);
    box.style.display = 'block';
    box.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  $('btnPublish').addEventListener('click', () => save(true));
  $('btnDraft').addEventListener('click', () => save(false));
})();
