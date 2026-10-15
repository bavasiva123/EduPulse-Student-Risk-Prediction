# EduPulse — Student Academic Early Risk Prediction

A Flask-based college project demo covering data engineering, machine learning, security, and optional AWS S3 storage. **The bundled dataset is synthetic and its risk labels are simulated. This is NOT a validated real-world dropout prediction system.**

## Quick start (Windows PowerShell)

```powershell
cd student-risk-system
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:ADMIN_PASSWORD='ChooseYourOwnStrongAdminPassword!'
$env:TEACHER_PASSWORD='ChooseAnotherStrongTeacherPassword!'
$env:SECRET_KEY='ReplaceWithLongRandomSessionSecret'
python app.py
```

Visit http://127.0.0.1:5000 and sign in as `admin` with your chosen admin password or `teacher` with your chosen teacher password. Import `data/sample_students.csv`. The first run creates `student_risk.db` and initial users. Changing the environment passwords later does not reset existing database passwords; delete only your *local demo* database to recreate users.

## Optional S3 integration

Set `S3_BUCKET=student-risk-project-2026-unique` and `AWS_DEFAULT_REGION=us-east-1` in the environment. The application uses the AWS default credential provider chain; **never put access keys in source code**. On EC2, use an attached IAM role with only the required `s3:PutObject` permissions on the bucket's `datasets/*` and `reports/*` paths. Academy permissions may restrict IAM role attachment. With no S3 configuration, the app still works locally. S3 failures are surfaced as a status message and logged; do not claim uploads succeeded unless confirmed in S3.

## Dataset schema

`student_id,name,attendance,internal_marks,assignment_rate,arrears,study_hours,actual_risk`

Attendance, marks, assignment rate: 0–100. Arrears: integer 0–50. Study hours: 0–24. `actual_risk` is optional and must be a justified 0/1 label if supplied. Without 20+ labeled records with both classes, the system uses a rule-based fallback. Never label fabricated synthetic data as UCI data or claim its test scores are real-world performance.

## Model and limitations

Random Forest is trained only on uploaded rows with `actual_risk` labels, and evaluated using a stratified 25% holdout split. A simple attendance/marks baseline is compared on the same holdout. The model then refits on all labeled records for demo predictions. The five features do not include final grades. Global feature importance describes model-level patterns, not individual causes. Warning factors are transparent rules, not per-student model explanations. Do not use these predictions for punitive or automated academic decisions.

## Security and deployment notes

Passwords are hashed, Flask-Login manages sessions, Flask-WTF checks CSRF tokens on POST forms, teacher/admin roles control access to audit logs, and uploaded records are validated. Production deployment needs persistent `SECRET_KEY`, TLS/HTTPS, secure cookies (`COOKIE_SECURE=1`), backup strategy, additional authorization and privacy review, secure database hosting, and rate limiting. Never expose the Flask development server to the public internet. For EC2, use Gunicorn behind Nginx and HTTPS after verifying lab permissions. SQLite is intended for single-instance demonstrations.

## Files

- `app.py`: Flask application, SQLite, Random Forest, S3 upload service and audit logging
- `templates/`: dashboard, login, upload, student and audit pages
- `static/style.css`: responsive UI
- `data/sample_students.csv`: **synthetic demo dataset**
- `data/generate_sample.py`: deterministic synthetic sample generator
