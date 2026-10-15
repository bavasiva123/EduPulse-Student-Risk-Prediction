import os, io, csv, sqlite3, secrets
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
import pandas as pd
import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from flask import Flask, render_template, redirect, url_for, request, flash, send_file, abort
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import precision_score, recall_score, f1_score, accuracy_score

BASE = Path(__file__).resolve().parent
app = Flask(__name__)
app.config.update(SECRET_KEY=os.environ.get('SECRET_KEY', secrets.token_hex(32)), MAX_CONTENT_LENGTH=5*1024*1024, WTF_CSRF_ENABLED=True, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE')=='1')
CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
DB = Path(os.environ.get('DB_PATH', str(BASE/'student_risk.db')))
FEATURES = ['attendance','internal_marks','assignment_rate','arrears','study_hours']
BUCKET = os.environ.get('S3_BUCKET','')

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with conn() as c:
        c.executescript('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role TEXT NOT NULL); CREATE TABLE IF NOT EXISTS students (id INTEGER PRIMARY KEY, student_id TEXT UNIQUE NOT NULL, name TEXT NOT NULL, attendance REAL NOT NULL, internal_marks REAL NOT NULL, assignment_rate REAL NOT NULL, arrears INTEGER NOT NULL, study_hours REAL NOT NULL, actual_risk INTEGER, created_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, username TEXT NOT NULL, action TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL);''')
        if not c.execute('SELECT id FROM users LIMIT 1').fetchone():
            admin_password = os.environ.get('ADMIN_PASSWORD')
            teacher_password = os.environ.get('TEACHER_PASSWORD')
            if not admin_password or not teacher_password:
                raise RuntimeError('Set ADMIN_PASSWORD and TEACHER_PASSWORD environment variables before first run.')
            c.execute('INSERT INTO users(username,password_hash,role) VALUES (?,?,?)', ('admin',generate_password_hash(admin_password),'admin'))
            c.execute('INSERT INTO users(username,password_hash,role) VALUES (?,?,?)', ('teacher',generate_password_hash(teacher_password),'teacher'))

def audit(action, detail=''):
    with conn() as c:
        c.execute('INSERT INTO audit_log(username,action,detail,created_at) VALUES (?,?,?,?)', (current_user.username if current_user.is_authenticated else 'anonymous',action,str(detail)[:250],datetime.now(timezone.utc).isoformat()))

class User(UserMixin):
    def __init__(self,row): self.id=str(row['id']); self.username=row['username']; self.role=row['role']
@login_manager.user_loader
def load_user(uid):
    with conn() as c: row=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    return User(row) if row else None

def admin_only(fn):
    @wraps(fn)
    @login_required
    def wrapper(*args,**kwargs):
        if current_user.role!='admin': abort(403)
        return fn(*args,**kwargs)
    return wrapper

def risk_rule(row):
    return int(float(row['attendance'])<75 or float(row['internal_marks'])<40 or int(row['arrears'])>0 or float(row['assignment_rate'])<55)

def get_model():
    with conn() as c: rows=c.execute('SELECT * FROM students WHERE actual_risk IS NOT NULL').fetchall()
    if len(rows)<20: return None, None
    df=pd.DataFrame([dict(r) for r in rows]); y=df['actual_risk'].astype(int)
    if y.nunique()<2 or y.value_counts().min()<4: return None,None
    X=df[FEATURES]
    xtr,xte,ytr,yte=train_test_split(X,y,test_size=.25,random_state=42,stratify=y)
    clf=RandomForestClassifier(n_estimators=100,max_depth=6,random_state=42,class_weight='balanced')
    clf.fit(xtr,ytr)
    pred=clf.predict(xte)
    baseline=( (xte['attendance']<75) | (xte['internal_marks']<40) ).astype(int)
    metrics={'model':{'precision':precision_score(yte,pred,zero_division=0),'recall':recall_score(yte,pred,zero_division=0),'f1':f1_score(yte,pred,zero_division=0),'accuracy':accuracy_score(yte,pred)},'baseline':{'precision':precision_score(yte,baseline,zero_division=0),'recall':recall_score(yte,baseline,zero_division=0),'f1':f1_score(yte,baseline,zero_division=0),'accuracy':accuracy_score(yte,baseline)}}
    final=RandomForestClassifier(n_estimators=100,max_depth=6,random_state=42,class_weight='balanced').fit(X,y)
    
    import joblib
    os.makedirs("models", exist_ok=True)
    joblib.dump(final, "models/model.pkl")

    return final,metrics

def prediction(row,model):
    d=dict(row)
    score=float(model.predict_proba(pd.DataFrame([{k:d[k] for k in FEATURES}]))[0][list(model.classes_).index(1)]) if model is not None else None
    flagged=score>=.5 if score is not None else bool(risk_rule(d))
    reasons=[]
    if d['attendance']<75: reasons.append('Attendance below 75%')
    if d['internal_marks']<40: reasons.append('Internal marks below 40/100')
    if d['assignment_rate']<55: reasons.append('Assignment submission below 55%')
    if d['arrears']>0: reasons.append('Previous arrears recorded')
    if d['study_hours']<2: reasons.append('Low reported study hours')
    return {'risk':'At Risk' if flagged else 'On Track','score':round(score*100,1) if score is not None else None,'reasons':reasons or ['No obvious rule-based warning factors'],'recommendation':'Teacher review recommended; discuss attendance, coursework and support needs.' if flagged else 'Continue regular monitoring and academic support.'}

def upload_s3(data,key):
    if not BUCKET: return 'Local only (S3_BUCKET not configured)'
    try:
        boto3.client('s3',region_name=os.environ.get('AWS_DEFAULT_REGION','us-east-1')).put_object(Bucket=BUCKET,Key=key,Body=data,ServerSideEncryption='AES256')
        return 'Uploaded to S3'
    except (BotoCoreError,ClientError,NoCredentialsError) as exc:
        app.logger.warning('S3 upload failed: %s',exc)
        return 'S3 upload failed; local database saved'

def validate_df(df):
    required=['student_id','name']+FEATURES
    missing=set(required)-set(df.columns)
    if missing: raise ValueError('Missing columns: '+', '.join(sorted(missing)))
    df=df.drop_duplicates(subset=['student_id'],keep='last').copy()
    df['student_id']=df['student_id'].astype(str).str.strip()
    df['name']=df['name'].astype(str).str.strip()
    if (df['student_id']=='').any() or (df['name']=='').any(): raise ValueError('Student ID and name cannot be blank')
    for k in FEATURES: df[k]=pd.to_numeric(df[k],errors='coerce'); df[k]=df[k].fillna(df[k].median())
    if df[FEATURES].isna().any().any(): raise ValueError('Missing numeric column cannot be imputed')
    for k in ['attendance','internal_marks','assignment_rate']:
        if not df[k].between(0,100).all(): raise ValueError(k+' must be between 0 and 100')
    if not df['arrears'].between(0,50).all() or not (df['arrears']%1==0).all(): raise ValueError('Arrears must be whole numbers between 0 and 50')
    if not df['study_hours'].between(0,24).all(): raise ValueError('Study hours must be between 0 and 24')
    if 'actual_risk' in df.columns:
        df['actual_risk']=pd.to_numeric(df['actual_risk'],errors='coerce')
        if not df['actual_risk'].dropna().isin([0,1]).all(): raise ValueError('actual_risk must be 0, 1 or blank')
    else: df['actual_risk']=None
    return df

@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        with conn() as c: row=c.execute('SELECT * FROM users WHERE username=?',(request.form.get('username',''),)).fetchone()
        if row and check_password_hash(row['password_hash'],request.form.get('password','')):
            login_user(User(row)); audit('login'); return redirect(url_for('dashboard'))
        flash('Invalid credentials','error')
    return render_template('login.html')
@app.route('/logout',methods=['POST'])
@login_required
def logout(): audit('logout'); logout_user(); return redirect(url_for('login'))
@app.route('/')
@login_required
def dashboard():
    with conn() as c: rows=c.execute('SELECT * FROM students ORDER BY id DESC').fetchall()
    model,metrics=get_model()
    records=[{'student':dict(r),'prediction':prediction(r,model)} for r in rows]
    return render_template('dashboard.html',records=records,at_risk=sum(x['prediction']['risk']=='At Risk' for x in records),metrics=metrics,importance=sorted(zip(FEATURES,model.feature_importances_),key=lambda x:-x[1]) if model is not None else None,model_active=model is not None)
@app.route('/upload',methods=['GET','POST'])
@login_required
def upload():
    if request.method=='POST':
        file=request.files.get('file')
        if not file or not file.filename.lower().endswith(('.csv','.xlsx')): flash('Upload a CSV or XLSX file','error'); return redirect(url_for('upload'))
        try:
            raw=file.read()
            df=pd.read_csv(io.BytesIO(raw)) if file.filename.lower().endswith('.csv') else pd.read_excel(io.BytesIO(raw))
            df=validate_df(df)
            with conn() as c:
                for _,r in df.iterrows():
                    c.execute('''INSERT INTO students(student_id,name,attendance,internal_marks,assignment_rate,arrears,study_hours,actual_risk,created_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(student_id) DO UPDATE SET name=excluded.name,attendance=excluded.attendance,internal_marks=excluded.internal_marks,assignment_rate=excluded.assignment_rate,arrears=excluded.arrears,study_hours=excluded.study_hours,actual_risk=excluded.actual_risk''',(str(r['student_id']),str(r['name']),float(r['attendance']),float(r['internal_marks']),float(r['assignment_rate']),int(r['arrears']),float(r['study_hours']),None if pd.isna(r['actual_risk']) else int(r['actual_risk']),datetime.now(timezone.utc).isoformat()))
            clean=df.to_csv(index=False).encode()
            status=upload_s3(clean,'datasets/'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)+'.csv')
            audit('upload',f'{len(df)} records; {status}')
            flash(f'{len(df)} records imported. {status}','success'); return redirect(url_for('dashboard'))
        except (ValueError,UnicodeError,pd.errors.ParserError,KeyError) as exc: flash(str(exc),'error')
        except Exception as exc: app.logger.exception('Upload failed'); flash('Upload failed. Check the file format.','error')
    return render_template('upload.html')
@app.route('/student/<int:sid>')
@login_required
def student(sid):
    with conn() as c: row=c.execute('SELECT * FROM students WHERE id=?',(sid,)).fetchone()
    if row is None: abort(404)
    model,_=get_model(); result=prediction(row,model); audit('prediction',row['student_id'])
    return render_template('student.html',student=row,result=result,model_active=model is not None)
@app.route('/reports')
@login_required
def reports():
    with conn() as c: rows=c.execute('SELECT * FROM students ORDER BY student_id').fetchall()
    model,_=get_model()
    output=io.StringIO(); writer=csv.writer(output)
    writer.writerow(['student_id','name']+FEATURES+['risk','risk_probability_percent','warning_factors','recommendation'])
    for r in rows:
        p=prediction(r,model)
        writer.writerow([r['student_id'],r['name']]+[r[k] for k in FEATURES]+[p['risk'],p['score'] if p['score'] is not None else 'Rule-based','; '.join(p['reasons']),p['recommendation']])
    data=output.getvalue().encode('utf-8-sig')
    status=upload_s3(data,'reports/report-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'.csv')
    audit('export_report',status)
    return send_file(io.BytesIO(data),mimetype='text/csv',as_attachment=True,download_name='student_risk_report.csv')
@app.route('/audit')
@admin_only
def logs():
    with conn() as c: rows=c.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 100').fetchall()
    return render_template('audit.html',logs=rows)

init_db()
if __name__=='__main__': app.run(debug=False,host='127.0.0.1',port=5000)
