// Shared helpers: CSRF-aware fetch, local-time rendering, evidence lightbox.
(function () {
  const meta = document.querySelector('meta[name="csrf-token"]');
  window.CSRF = meta ? meta.content : '';

  window.api = async function (url, body, method) {
    const res = await fetch(url, {
      method: method || 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': window.CSRF },
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    return { ok: res.ok, status: res.status, data };
  };

  // <time class="lt" datetime="...Z"> -> viewer's local timezone
  document.querySelectorAll('time.lt').forEach(function (t) {
    const d = new Date(t.getAttribute('datetime'));
    if (!isNaN(d)) t.textContent = d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
  });

  // Evidence lightbox
  const lb = document.getElementById('lightbox');
  if (lb) {
    document.addEventListener('click', function (e) {
      const img = e.target.closest('img[data-zoom]');
      if (img) { lb.querySelector('img').src = img.dataset.zoom; lb.classList.add('on'); }
      else if (e.target.closest('#lightbox')) { lb.classList.remove('on'); }
    });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') lb.classList.remove('on'); });
  }
})();
