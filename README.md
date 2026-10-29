# AI Online Proctoring System

An AI-powered online examination platform built with Flask and PostgreSQL. Examiners can create and manage exams, while students take tests with browser-based AI monitoring to flag potentially suspicious activity.

## Live Demo

**Website:** https://ai-proctoring-detection.onrender.com

**GitHub Repository:** https://github.com/Varshabachu050/AI_Proctoring_Detection

## Features

- **Exam Management:** Create, edit, schedule, and publish MCQ, numerical, and descriptive exams.
- **Student Portal:** Register, log in, take exams, and view results.
- **AI Proctoring:** Uses MediaPipe and browser-based detection to flag multiple faces, missing faces, unusual head movements, and suspicious objects.
- **Audio Monitoring:** Detects voice activity through the microphone.
- **Browser Monitoring:** Flags tab switching, fullscreen exits, copy/paste actions, and camera or microphone interruptions.
- **Evidence Capture:** Stores screenshots of flagged events for examiner review.
- **Examiner Dashboard:** Review exam attempts, monitoring events, and student performance.
- **PDF Reports:** Generate reports containing evidence, violation analysis, and performance details.
- **Database Support:** Supports SQLite for local development and PostgreSQL for deployment.

## Technology Stack

- **Backend:** Python, Flask
- **Frontend:** HTML, CSS, JavaScript
- **AI and Computer Vision:** MediaPipe
- **Database:** PostgreSQL, SQLite
- **PDF Generation:** ReportLab
- **Deployment:** Render

## Run Locally

1. Clone or download this repository.
2. Open a terminal in the project directory.
3. Create and activate a virtual environment:

   ```bash
   python -m venv venv
   source venv/bin/activate
   ```

   On Windows:

   ```bash
   venv\Scripts\activate
   ```

4. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

5. Start the application:

   ```bash
   python app.py
   ```

6. Open **http://localhost:5000** in your browser.

## Deployment

The application is deployed on Render.

- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT`
- Configure the required environment variables, including `SECRET_KEY` and `DATABASE_URL`, according to your deployment setup.

## Project Structure

```text
AI_Proctoring_Detection/
├── app.py
├── config.py
├── db.py
├── scoring.py
├── services.py
├── report.py
├── blueprints/
├── templates/
├── static/
├── tests/
├── requirements.txt
├── render.yaml
└── README.md
```

## Author

**Bachu Varsha**


