"""Generate synthetic demo records. Labels are simulated, NOT real student outcomes."""
import csv,random
from pathlib import Path
r=random.Random(42)
path=Path(__file__).with_name('sample_students.csv')
with path.open('w',newline='',encoding='utf-8') as f:
    w=csv.writer(f)
    w.writerow(['student_id','name','attendance','internal_marks','assignment_rate','arrears','study_hours','actual_risk'])
    for i in range(1,121):
        attendance=round(r.uniform(45,100),1); marks=round(r.uniform(20,95),1)
        assignments=round(r.uniform(35,100),1); arrears=r.choices([0,1,2,3],[.65,.2,.1,.05])[0]
        hours=round(r.uniform(.5,6),1)
        # Synthetic target with noise; strictly for software demonstration.
        risk=int((attendance<72)+(marks<42)+(assignments<52)+(arrears>0)>=2 or (marks<35))
        if r.random()<.07: risk=1-risk
        w.writerow([f'STU{i:03d}',f'Demo Student {i:03d}',attendance,marks,assignments,arrears,hours,risk])
print(path)
