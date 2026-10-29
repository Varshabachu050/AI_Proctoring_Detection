# AI Online Proctoring System

Flask + PostgreSQL app where **examiners** create and publish exams (MCQ, numerical, descriptive) and
**students** take them while being monitored through webcam and microphone. AI runs **in the student's browser**
(MediaPipe), flags malpractice, uploads a screenshot of each flagged moment, and the examiner gets a live
dashboard plus a PDF report with evidence, analysis and performance.

## What it detects

| Violation | How |
|---|---|
| Multiple faces / extra person | MediaPipe FaceLandmarker (up to 3 faces) + person detection |
| Face not visible | no face for > 4 s |
| Looking away | head-pose vs. calibrated baseline + eye-gaze blendshapes, sustained > 3.5 s |
| Suspicious head movement | repeated rapid left-right / up-down swings within 6 s |
| Talking | microphone voice-activity (calibrated to the room's noise floor) |
| Mobile phone / other objects | MediaPipe ObjectDetector (EfficientDet-Lite0): phone, book, laptop, remote |
| Browser integrity | tab/window switch, fullscreen exit, copy/paste/cut, blocked shortcuts, camera/mic lost |

Each violation → screenshot (with detection boxes) + server timestamp + stored in the DB.

**Cheating / misuse percentage** (see `scoring.py`):
`points = Σ weight × min(count, cap)` and `misuse% = 100 × (1 − e^(−points/60))`.
Low < 25 %, Medium 25–50 %, High ≥ 50 %. Weights/caps are in `VIOLATION_TYPES` - edit to taste.
Detector sensitivity is in `static/js/detectors.js` → `THRESHOLDS`.

## Run locally

```bash
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py                                        # http://localhost:5000  (uses SQLite: proctor.db)
```
Camera access works on `http://localhost`; any other host needs HTTPS (Render provides it).

Tests: `python -m unittest discover -s tests -v` and `node tests/test_detectors.mjs`.

## Calibration (exam setup screen)

The setup screen now shows a **calibration progress bar**, a live hint ("move closer", "hold still", "no face detected"…)
and a diagnostics line (detector fps · face size · samples). Calibration finishes after ~3 s of a steady head;
moving resets the bar. If it takes > 12 s a **Restart calibration** button appears.
Tunables: `CALIB` in `static/js/detectors.js`. Phone/book detection is optional - if its model cannot load, the exam still works.

Troubleshooting: if the bar never moves, check (1) the diagnostics line shows fps > 0, (2) no other app (Zoom, Meet, another tab) is using the camera,
(3) the page can reach `cdn.jsdelivr.net` / `unpkg.com` and `storage.googleapis.com`, (4) good front lighting.

## Deploy to Render

1. **Push to GitHub** (only this project folder - NOT your old `venv/`, screenshots or `yolov8n.pt`; `.gitignore` handles it):
   ```bash
   cd ai-proctor
   git init && git add . && git commit -m "AI proctoring system"
   git branch -M main
   git remote add origin https://github.com/<you>/ai-proctor.git
   git push -u origin main
   ```
2. In Render: **New → Blueprint** → select the repo. Render reads `render.yaml` and creates the web service **and** the Postgres database, wiring `DATABASE_URL` and a generated `SECRET_KEY`.
3. When prompted, set **`EXAMINER_INVITE_CODE`** to a secret only your examiners know (otherwise anyone can register as an examiner). Leave empty to allow open examiner registration.
4. Wait for the deploy, open the `https://<name>.onrender.com` URL, register an examiner, create an exam.

Manual alternative (no Blueprint): create a Postgres DB, then a Web Service with build `pip install -r requirements.txt`,
start `gunicorn app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT`, health check `/healthz`, and env vars
`DATABASE_URL` (internal URL), `SECRET_KEY`, `PYTHON_VERSION=3.12.8`.

### Free-tier caveats (Render)
* Free web service **sleeps after 15 min idle** (≈1 min cold start) - wake it before an exam, or use the $7 Starter plan.
* Free Postgres **expires after 30 days** (14-day grace), 1 GB, no backups. Fine for a demo; for real exams use a paid DB or an external Postgres (just set `DATABASE_URL`).
* Screenshots are stored in Postgres (≈30 KB each, max 120 per attempt) because Render's disk is ephemeral.

## Project layout
```
app.py            app factory / entry point (gunicorn app:app)
config.py db.py   settings; SQLite+Postgres layer
scoring.py        violation weights, misuse %, analysis text
services.py       grading, attempt finalisation, exam validation
report.py         PDF reports (ReportLab)
blueprints/       auth, student, examiner, api
templates/ static/   UI; static/js: proctor.js (AI), detectors.js (logic), exam.js, exam_form.js
render.yaml       Render Blueprint
```

## Honest limitations
* AI detections are **indicators, not proof** - false positives happen (poor lighting, glasses, noisy rooms). The report says so; examiners should review screenshots.
* Detection runs client-side, so a technically skilled student could tamper with their browser. Server-side checks (cooldowns, time limits, auth) limit abuse, but this is not a lockdown browser.
* Phone detection uses a general COCO model; small/angled phones can be missed.
* Best on desktop/laptop Chrome, Edge or Firefox. Mobile browsers don't support fullscreen enforcement.
