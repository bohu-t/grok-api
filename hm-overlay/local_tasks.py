"""Local browser-registration tasks embedded in the HM admin service."""
from __future__ import annotations
import json, os, re, shutil, signal, sqlite3, subprocess, threading, time
from datetime import datetime
from pathlib import Path
from typing import Any
import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from grok2api.admin.admin_routes import require_admin
from grok2api.admin.settings_store import create_session_token

router = APIRouter(prefix="/admin/api/local-tasks", tags=["local-tasks"])
DATA = Path("/app/data/local-tasks")
DB = DATA / "local-tasks.db"
SRC = Path("/opt/local-register-source")
LOCK = threading.RLock()
RUNNING: dict[int, subprocess.Popen] = {}
STARTED = False
STATUSES = {"creating","queued","running","stopping","completed","partial","failed","stopped"}
ROUND = re.compile(r"开始第\s*(\d+)\s*轮注册")
SUCCESS = re.compile(r"注册成功\s*\|\s*email=([^|\s]+)")
ERROR = re.compile(r"\[Error\]\s*第\s*(\d+)\s*轮失败:\s*(.+)")
MAIL = re.compile(r"临时邮箱创建成功:\s*([^\s]+)")

class TaskCreate(BaseModel):
    name: str = Field(default="注册任务", max_length=120)
    count: int = Field(default=1, ge=1, le=5000)
    proxy: str = ""
    browser_proxy: str = ""
    temp_mail_api_base: str = ""
    temp_mail_admin_password: str = ""
    temp_mail_domain: str = ""
    temp_mail_domains: list[str] = Field(default_factory=list)
    temp_mail_site_password: str = ""
    notes: str = Field(default="", max_length=500)

def split_mail_domains(value: Any) -> list[str]:
  raw=[]
  if isinstance(value, str): raw=re.split(r"[,;\s]+", value)
  elif isinstance(value, (list, tuple, set)):
    for item in value: raw.extend(re.split(r"[,;\s]+", str(item or "")))
  elif value is not None: raw=re.split(r"[,;\s]+", str(value))
  out=[]; seen=set()
  for item in raw:
    domain=str(item or "").strip().lstrip("@")
    key=domain.lower()
    if key and key not in seen:
      out.append(domain); seen.add(key)
  return out

def now(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def conn():
    c=sqlite3.connect(DB, check_same_thread=False); c.row_factory=sqlite3.Row; return c
def init():
    DATA.mkdir(parents=True,exist_ok=True)
    with LOCK, conn() as c:
      c.execute('''CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,status TEXT NOT NULL,target_count INTEGER NOT NULL,completed_count INTEGER NOT NULL DEFAULT 0,failed_count INTEGER NOT NULL DEFAULT 0,current_round INTEGER NOT NULL DEFAULT 0,current_phase TEXT,last_email TEXT,last_error TEXT,notes TEXT,config_json TEXT NOT NULL,task_dir TEXT NOT NULL,console_path TEXT NOT NULL,pid INTEGER,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,exit_code INTEGER,hm_imported_count INTEGER NOT NULL DEFAULT 0,hm_processed_count INTEGER NOT NULL DEFAULT 0,hm_import_status TEXT)''')
      c.execute('CREATE TABLE IF NOT EXISTS task_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
      c.commit()
def rows(sql,*args):
  with LOCK,conn() as c:return c.execute(sql,args).fetchall()
def one(i):
  x=rows('SELECT * FROM tasks WHERE id=?',i)
  if not x: raise HTTPException(404,'Task not found')
  return x[0]
def update(sql,*args):
  with LOCK,conn() as c:c.execute(sql,args);c.commit()
def ser(r):
  d=dict(r); d['config']=json.loads(d.pop('config_json') or '{}'); return d
def parse(r):
  out={'completed_count':0,'failed_count':0,'current_round':0,'current_phase':'','last_email':'','last_error':''}
  p=Path(r['console_path'])
  if not p.exists(): return out
  for ln in p.read_text(errors='replace').splitlines():
    if m:=ROUND.search(ln): out['current_round']=int(m.group(1));out['current_phase']='starting_round'
    if m:=SUCCESS.search(ln): out['completed_count']+=1;out['last_email']=m.group(1);out['current_phase']='success'
    if m:=ERROR.search(ln): out['failed_count']+=1;out['last_error']=m.group(2)[:1000];out['current_phase']='error'
    if m:=MAIL.search(ln): out['last_email']=m.group(1);out['current_phase']='mailbox_created'
  return out
def source_copy(dst,cfg):
  dst.mkdir(parents=True,exist_ok=True)
  for f in ('DrissionPage_example.py','email_register.py') : shutil.copy2(SRC/f,dst/f)
  shutil.copytree(SRC/'turnstilePatch',dst/'turnstilePatch',dirs_exist_ok=True)
  (dst/'logs').mkdir(exist_ok=True);(dst/'sso').mkdir(exist_ok=True)
  (dst/'config.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2))
def import_sso(r):
  p=Path(r['task_dir'])/'sso'/f"task_{r['id']}.txt"
  if not p.exists(): return
  lines=[x.strip().split('----')[-1] for x in p.read_text(errors='replace').splitlines() if x.strip()]
  done=int(r['hm_processed_count'] or 0); new=lines[done:]
  if not new:return
  try:
    base='http://127.0.0.1:'+os.getenv('GROK2API_PORT','3000')
    token=create_session_token(); h={'X-Admin-Token':token}
    resp=requests.post(base+'/admin/api/accounts/import-sso',headers=h,json={'sso_cookies':new,'merge':True,'delay':0,'max_workers':2},timeout=30)
    if resp.status_code>=400:
      raise RuntimeError(f"import-sso HTTP {resp.status_code}: {resp.text[:500]}")
    j=resp.json(); job_id=j.get('job_id')
    if not job_id:
      imported=int(j.get('success') or j.get('imported') or 0)
      update('UPDATE tasks SET hm_processed_count=?,hm_imported_count=hm_imported_count+?,hm_import_status=? WHERE id=?',done+len(new),imported,'done' if imported else 'submitted_no_job',r['id'])
      return
    final=None
    for _ in range(90):
      time.sleep(2)
      pr=requests.get(base+f'/admin/api/accounts/import-sso/jobs/{job_id}',headers=h,timeout=15)
      if pr.status_code>=400:
        raise RuntimeError(f"import-sso job HTTP {pr.status_code}: {pr.text[:500]}")
      final=pr.json()
      status=str(final.get('status') or '').lower()
      if status in {'done','error','failed','completed'} or final.get('finished_at'):
        break
    imported=int((final or {}).get('success') or (final or {}).get('imported_count') or (final or {}).get('imported') or 0)
    failed=int((final or {}).get('fail') or (final or {}).get('failed') or 0)
    status=str((final or {}).get('status') or 'unknown')
    msg=(final or {}).get('message') or (final or {}).get('error') or status
    processed=len(new) if status in {'done','completed'} or imported or failed else 0
    update('UPDATE tasks SET hm_processed_count=hm_processed_count+?,hm_imported_count=hm_imported_count+?,hm_import_status=? WHERE id=?',processed,imported,(f"{status}: imported={imported} failed={failed} {msg}")[:1000],r['id'])
  except Exception as e:update('UPDATE tasks SET hm_import_status=? WHERE id=?',('error: '+str(e))[:1000],r['id'])
def start(r):
  dst=Path(r['task_dir']);cfg=json.loads(r['config_json']);source_copy(dst,cfg)
  log=Path(r['console_path']); out=dst/'sso'/f"task_{r['id']}.txt"
  fh=log.open('a'); p=subprocess.Popen(['python',str(dst/'DrissionPage_example.py'),'--count',str(r['target_count']),'--output',str(out)],cwd=dst,stdout=fh,stderr=subprocess.STDOUT,start_new_session=True)
  RUNNING[int(r['id'])]=p;update('UPDATE tasks SET status=?,pid=?,started_at=?,current_phase=? WHERE id=?','running',p.pid,now(),'process_started',r['id'])
def loop():
  while True:
    try:
      for tid,p in list(RUNNING.items()):
        r=one(tid);x=parse(r);update('UPDATE tasks SET completed_count=?,failed_count=?,current_round=?,current_phase=?,last_email=?,last_error=? WHERE id=?',x['completed_count'],x['failed_count'],x['current_round'],x['current_phase'],x['last_email'],x['last_error'],tid);import_sso(one(tid))
        code=p.poll()
        if code is not None:
          status='stopped' if r['status']=='stopping' else ('completed' if code==0 and x['completed_count']>=r['target_count'] else ('partial' if x['completed_count'] else 'failed'))
          update('UPDATE tasks SET status=?,finished_at=?,exit_code=?,current_phase=? WHERE id=?',status,now(),code,status,tid);import_sso(one(tid));RUNNING.pop(tid,None)
      slots=max(1,int(os.getenv('GROK2API_LOCAL_TASK_WORKERS','1')))-len(RUNNING)
      if slots>0:
        for r in rows('SELECT * FROM tasks WHERE status=? ORDER BY id LIMIT ?', 'queued',slots): start(r)
    except Exception: pass
    time.sleep(2)
def ensure_started():
  global STARTED
  init()
  if not STARTED: STARTED=True;threading.Thread(target=loop,daemon=True).start()
def defaults():
  try:d=json.loads((SRC/'config.example.json').read_text())
  except Exception:d={}
  with LOCK, conn() as c:
    row=c.execute("SELECT value FROM task_settings WHERE key='defaults'").fetchone()
  if row:
    try:d.update(json.loads(row['value']))
    except Exception:pass
  d['proxy']=d.get('proxy') or os.getenv('GROK_REGISTER_DEFAULT_PROXY','socks5://127.0.0.1:1081')
  d['browser_proxy']=d.get('browser_proxy') or os.getenv('GROK_REGISTER_DEFAULT_BROWSER_PROXY','socks5://127.0.0.1:1081')
  domains=split_mail_domains(d.get('temp_mail_domains') or d.get('temp_mail_domain'))
  d['temp_mail_domains']=domains
  d['temp_mail_domain']=', '.join(domains)
  return d
@router.get('/meta')
def meta(_:str=Depends(require_admin)): ensure_started();return {'defaults':defaults()}
@router.get('')
def list_tasks(_:str=Depends(require_admin)): ensure_started();return {'tasks':[ser(r) for r in rows('SELECT * FROM tasks ORDER BY id DESC')]}
@router.post('')
def create(p:TaskCreate,_:str=Depends(require_admin)):
  ensure_started();cfg=defaults();payload=p.model_dump();cfg.update({k:v for k,v in payload.items() if k in cfg and v});domains=split_mail_domains(payload.get('temp_mail_domains') or payload.get('temp_mail_domain') or cfg.get('temp_mail_domains') or cfg.get('temp_mail_domain'));cfg['temp_mail_domains']=domains;cfg['temp_mail_domain']=', '.join(domains);cfg['api']={'endpoint':'','token':'','append':True};cfg.setdefault('run',{})['count']=p.count
  with LOCK,conn() as c:
    cur=c.execute('INSERT INTO tasks(name,status,target_count,notes,config_json,task_dir,console_path,created_at) VALUES(?,?,?,?,?,?,?,?)',(p.name,'creating',p.count,p.notes,json.dumps(cfg,ensure_ascii=False),'pending','pending',now()));tid=cur.lastrowid;d=DATA/f'task_{tid}';c.execute('UPDATE tasks SET status=?,task_dir=?,console_path=? WHERE id=?',('queued',str(d),str(d/'console.log'),tid));c.commit()
  return {'task':ser(one(tid))}
@router.get('/task/{tid}')
def get(tid:int,_:str=Depends(require_admin)):ensure_started();return {'task':ser(one(tid))}
@router.get('/task/{tid}/logs')
def logs(tid:int,limit:int=Query(350,ge=20,le=1000),_:str=Depends(require_admin)):
  r=one(tid);p=Path(r['console_path']);return {'lines':p.read_text(errors='replace').splitlines()[-limit:] if p.exists() else []}
@router.post('/task/{tid}/stop')
def stop(tid:int,_:str=Depends(require_admin)):
  r=one(tid);p=RUNNING.get(tid)
  if p: update('UPDATE tasks SET status=?,current_phase=? WHERE id=?','stopping','stopping',tid);os.killpg(p.pid,signal.SIGTERM)
  elif r['status']=='queued':update('UPDATE tasks SET status=?,finished_at=? WHERE id=?','stopped',now(),tid)
  else:raise HTTPException(409,'Task is not running')
  return {'ok':True}
@router.delete('/task/{tid}')
def delete(tid:int,_:str=Depends(require_admin)):
  if tid in RUNNING:raise HTTPException(409,'Task is running')
  r=one(tid);shutil.rmtree(r['task_dir'],ignore_errors=True);update('DELETE FROM tasks WHERE id=?',tid);return {'ok':True}

@router.get('/tasks')
def list_tasks_compat(_:str=Depends(require_admin)):
  return list_tasks(_)

@router.post('/tasks')
def create_task_compat(p:TaskCreate,_:str=Depends(require_admin)):
  return create(p,_)

@router.get('/settings')
def get_settings(_:str=Depends(require_admin)):
  ensure_started(); return {'settings':defaults()}

@router.post('/settings')
def save_settings(payload:dict[str,Any],_:str=Depends(require_admin)):
  ensure_started()
  allowed={'proxy','browser_proxy','temp_mail_api_base','temp_mail_admin_password','temp_mail_domain','temp_mail_domains','temp_mail_site_password'}
  current=defaults()
  saved={k:v for k,v in payload.items() if k in allowed}
  # Password inputs are intentionally not echoed to the browser.  If the user
  # saves the form with those fields blank, preserve the existing secrets
  # instead of overwriting them with empty strings.
  for secret_key in ('temp_mail_admin_password','temp_mail_site_password'):
    if str(saved.get(secret_key) or '') == '' and current.get(secret_key):
      saved[secret_key]=current.get(secret_key)
  domains=split_mail_domains(saved.get('temp_mail_domains') or saved.get('temp_mail_domain'))
  saved['temp_mail_domains']=domains
  saved['temp_mail_domain']=', '.join(domains)
  merged={k:current.get(k,'') for k in allowed if k in current}
  merged.update(saved)
  merged={k:(v if isinstance(v,list) else str(v or '')) for k,v in merged.items()}
  with LOCK, conn() as c:
    c.execute("INSERT INTO task_settings(key,value) VALUES('defaults',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(merged,ensure_ascii=False),))
    c.commit()
  return {'settings':defaults()}

@router.get('/tasks/{tid}')
def get_task_compat(tid:int,_:str=Depends(require_admin)):
  return get(tid,_)

@router.get('/tasks/{tid}/logs')
def logs_compat(tid:int,limit:int=Query(350,ge=20,le=1000),_:str=Depends(require_admin)):
  return logs(tid,limit,_)

@router.post('/tasks/{tid}/stop')
def stop_compat(tid:int,_:str=Depends(require_admin)):
  return stop(tid,_)

@router.delete('/tasks/{tid}')
def delete_compat(tid:int,_:str=Depends(require_admin)):
  return delete(tid,_)
