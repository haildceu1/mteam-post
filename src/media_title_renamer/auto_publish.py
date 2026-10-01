"""Locked publication, official recall and qB seeding with evidence checks."""
from __future__ import annotations
import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
import tempfile
from contextlib import contextmanager
from pathlib import Path

STATE_ROOT = Path(os.environ.get('MTEAM_AUTO_STATE_DIR', '/data/Zhyw/media-stack/downloads/.mteam-transfer/auto-state'))

SEARCH_CODE = r'''
import json, sqlite3, sys, requests, time
from app.runtime.settings import get_runtime_setting
args=json.loads(sys.argv[1])
try:
 db=sqlite3.connect('file:/config/user.db?mode=ro',uri=True);db.row_factory=sqlite3.Row
 row=db.execute('SELECT domain,apikey,ua,proxy FROM site WHERE id=? AND is_active=1',(args['site_id'],)).fetchone();db.close()
 if not row or str(row['domain']).strip('/')!='m-team.cc' or not row['apikey']:raise ValueError('site_configuration')
 session=requests.Session();session.trust_env=False
 headers={'x-api-key':row['apikey'],'User-Agent':row['ua'] or get_runtime_setting('USER_AGENT')}
 proxy=get_runtime_setting('PROXY') if row['proxy'] else None
 found={};total=None
 for page in range(1,11):
  response=session.post('https://api.m-team.cc/api/torrent/search',json={'keyword':args['keyword'],'mode':'normal','pageNumber':page,'pageSize':100,'visible':1},headers=headers,proxies=proxy,timeout=(10,35))
  if response.status_code!=200:
   print(json.dumps({'ok':False,'http_status':response.status_code}));sys.exit(2)
  payload=response.json()
  if str(payload.get('code'))!='0':
   print(json.dumps({'ok':False,'api_code':payload.get('code')}));sys.exit(2)
  data=payload.get('data');records=data.get('data') if isinstance(data,dict) else None
  if not isinstance(records,list) or data.get('total') is None:raise ValueError('search_incomplete')
  count=int(data['total'])
  if total is not None and count!=total:raise ValueError('search_changed')
  total=count
  for item in records:
   if not isinstance(item,dict) or not str(item.get('id','')).isdigit() or not isinstance(item.get('name'),str) or item.get('size') is None:raise ValueError('search_incomplete')
   found[str(item['id'])]={'id':str(item['id']),'title':item['name'],'size_bytes':int(item['size'])}
  if len(found)==total:break
  if not records or len(found)>total:raise ValueError('search_incomplete')
  time.sleep(60)
 if len(found)!=total:raise ValueError('search_incomplete')
 print(json.dumps({'ok':True,'complete':True,'total':total,'candidates':list(found.values())},ensure_ascii=False))
except Exception as exc:
 allowed={'site_configuration','search_incomplete','search_changed'}
 print(json.dumps({'ok':False,'error_type':type(exc).__name__,'reason':str(exc) if str(exc) in allowed else None}));sys.exit(2)
'''

def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary=path.with_suffix('.writing')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)

def resource_key(source):
    return hashlib.sha256(str(Path(source).resolve()).encode()).hexdigest()

@contextmanager
def task_lock(source):
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with (STATE_ROOT/(resource_key(source)+'.lock')).open('a') as handle:
        try: fcntl.flock(handle, fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise ValueError('resource_busy') from None
        try: yield
        finally: fcntl.flock(handle, fcntl.LOCK_UN)

def previous_task(source):
    registry=STATE_ROOT/(resource_key(source)+'.json')
    if not registry.is_file(): return None
    item=json.loads(registry.read_text())
    if str(Path(item['source']).resolve())!=str(Path(source).resolve()):raise ValueError('registry_source_mismatch')
    return item

def search_site(keyword, *, container='moviepilot', site_id=1):
    """Credentials stay in MP. Successful empty results are valid, failures are not."""
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with (STATE_ROOT/'search.lock').open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        rate=STATE_ROOT/'search-rate.json'
        previous=json.loads(rate.read_text()).get('started',0) if rate.is_file() else 0
        delay=max(0,60-(time.time()-previous))
        while delay>0:
            print(f'[查重] 请求间隔保护，等待 {delay:.0f} 秒',flush=True)
            time.sleep(min(delay,30));delay=max(0,60-(time.time()-previous))
        atomic_json(rate,{'started':time.time()})
        process=subprocess.run(['docker','exec','-i',container,'/opt/venv/bin/python','-',json.dumps({'keyword':keyword,'site_id':site_id})],
                               input=SEARCH_CODE,text=True,capture_output=True,timeout=680)
    payload=None
    for line in process.stdout.splitlines():
        try: item=json.loads(line)
        except ValueError: continue
        if isinstance(item,dict) and 'ok' in item:payload=item
    if not payload or not payload.get('ok') or not payload.get('complete') or process.returncode:
        code=str((payload or {}).get('api_code',''))
        raise ValueError('mteam_rate_limited' if code=='4' else 'mteam_search_unavailable')
    return payload

def search_identity(package):
    """Use a corroborated title when TMDB is unavailable; never search blank."""
    tmdb = package.get('tmdb') or {}
    tmdb_name = str(tmdb.get('name') or tmdb.get('original_name') or '').strip()
    if tmdb_name:
        return tmdb_name
    identity = (package.get('identification_evidence') or {}).get('identity') or {}
    douban = package.get('douban_match') or {}
    douban_url = str(package.get('douban_url') or '')
    imdb_url = str(package.get('imdb_url') or '')
    verified_douban = (douban_url.startswith('https://movie.douban.com/subject/')
                       and douban.get('url') == douban_url)
    verified_imdb = bool(re.fullmatch(r'https://www\.imdb\.com/title/tt\d+/?', imdb_url))
    if not (verified_douban or verified_imdb):
        raise ValueError('search_identity_missing')
    if (identity.get('kind') != package.get('kind')
            or float(identity.get('confidence') or 0) < .90
            or (package.get('year') and str(identity.get('year') or '') != str(package['year']))):
        raise ValueError('search_identity_missing')
    name = str(identity.get('search_title') or identity.get('title') or '').strip()
    if not name:
        raise ValueError('search_identity_missing')
    return name

def duplicate_evidence(package, candidates, total_size):
    name=search_identity(package)
    norm=lambda s: ' '.join(re.findall(r'\w+',str(s).casefold()))
    identity=norm(name)
    if not identity:raise ValueError('search_identity_missing')
    season=str(package.get('episode') or '')
    duplicates=[]
    for item in candidates:
        text=norm(item['title'])
        if identity not in text:continue
        if package.get('kind')=='movie' and str(package.get('year') or '') not in text:continue
        if season and not re.search(re.escape(season)+r'(?!\d)',item['title'],re.I):continue
        group=str(package.get('group') or '')
        same_group=group not in ('','NOGRP') and bool(re.search(r'(?:-|\[)'+re.escape(group)+r'(?:\]|\s|$)',item['title'],re.I))
        if int(item['size_bytes'])==total_size or same_group:
            duplicates.append({**item,'rule':'same_size' if int(item['size_bytes'])==total_size else 'same_release_group',
                               'detail_url':'https://kp.m-team.cc/detail/'+item['id']})
    return duplicates

def source_tasks(source, *, qb_url=None, qb_config=None, qb_container='qbittorrent'):
    from .qb_seed import mapped_source
    from .seed_official import _qb_api,_qb_config_key,DEFAULT_QB_URL,DEFAULT_QB_CONFIG
    from urllib.parse import urlsplit
    base=(qb_url or DEFAULT_QB_URL).rstrip('/')
    mapped=mapped_source(Path(source).absolute(),qb_container).as_posix()
    session=_qb_api(base,_qb_config_key(qb_config or DEFAULT_QB_CONFIG))
    try:
        response=session.get(base+'/api/v2/torrents/info',timeout=20);response.raise_for_status()
        records=[]
        for task in response.json():
            if str(task.get('content_path','')).rstrip('/')!=mapped:continue
            response=session.get(base+'/api/v2/torrents/trackers',params={'hash':task['hash']},timeout=10);response.raise_for_status()
            hosts=[urlsplit(row.get('url','')).hostname or '' for row in response.json()]
            record={key:task.get(key) for key in ('hash','name','size','progress','state','save_path','content_path','tags')}
            record['mteam']=any(host=='m-team.cc' or host.endswith('.m-team.cc') for host in hosts) or 'mteam' in str(task.get('tags','')).lower()
            records.append(record)
        return records
    finally:session.close()

def publish_and_seed(package_path, output, options):
    from .publish import main as publish_main
    from .autonomous import files_snapshot,operation_decision
    package=json.loads(package_path.read_text());source=Path(package['input_path'])
    if Path(package['prepared_path']).resolve()!=source.resolve():raise ValueError('remain_source_mismatch')
    receipt_path=output/'publish-result.json'
    prior=previous_task(source)
    if prior:
        package_path=Path(prior['package']);receipt_path=Path(prior['receipt'])
        package=json.loads(package_path.read_text())
        if Path(package['input_path']).resolve()!=source.resolve():raise ValueError('registry_source_mismatch')
    receipt=json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
    decision=operation_decision(receipt)
    if decision not in ('resume_recall_only','no_prior_submission'):raise ValueError(decision)
    before=files_snapshot(source)
    if not receipt.get('mteam_torrent_id'):
        tasks=source_tasks(source,qb_url=options.qb_url,qb_config=options.qb_config,qb_container=options.qb_container)
        atomic_json(output/'qb-before.json',tasks)
        if any(t['mteam'] for t in tasks):raise ValueError('already_in_qb')
        keyword=search_identity(package)
        print('[阶段] M-Team 发布前查重：'+keyword,flush=True)
        result=search_site(keyword,container=options.moviepilot_container,site_id=options.site_id)
        duplicates=duplicate_evidence(package,result['candidates'],sum(r['bytes'] for r in before))
        atomic_json(output/'duplicate-check.json',{**result,'keyword':keyword,'matches':duplicates})
        if duplicates:raise ValueError('mteam_duplicate')
    atomic_json(STATE_ROOT/(resource_key(source)+'.json'),{'source':str(source.resolve()),'package':str(package_path.resolve()),'receipt':str(receipt_path.resolve())})
    if files_snapshot(source)!=before:raise ValueError('source_changed')
    command=[str(package_path),'--yes','--submit','--recall-official','--result-json',str(receipt_path),
             '--moviepilot-container',options.moviepilot_container,'--site-id',str(options.site_id),
             '--qb-container',options.qb_container,'--qb-url',options.qb_url,'--qb-config',str(options.qb_config),
             '--login-timeout',str(options.login_timeout)]
    if options.profile_dir:command.extend(['--profile-dir',str(options.profile_dir)])
    print('[阶段] 自动发布 → 官方种子召回 → 暂停加种与路径验证 → Mteam 标签做种',flush=True)
    # Chrome's Unix-domain socket paths must fit sockaddr_un (108 bytes).
    # A deeply nested task TMPDIR can make Chrome exit before creating a session.
    chrome_tmp=Path(os.environ.get('MTEAM_CHROME_TMPDIR','/data/Zhyw/media-stack/.chrome-tmp'))/('auto-'+resource_key(source)[:8])
    if len(os.fsencode(chrome_tmp))>65:raise ValueError('chrome_temp_path_too_long')
    chrome_tmp.mkdir(parents=True,exist_ok=True,mode=0o700)
    previous_tmp=os.environ.get('TMPDIR');previous_tempdir=tempfile.tempdir
    os.environ['TMPDIR']=str(chrome_tmp);tempfile.tempdir=str(chrome_tmp)
    try:publish_main(command)
    except SystemExit as exc:raise RuntimeError('browser_or_recall_failed') from exc
    finally:
        if previous_tmp is None:os.environ.pop('TMPDIR',None)
        else:os.environ['TMPDIR']=previous_tmp
        tempfile.tempdir=previous_tempdir
        unchanged=files_snapshot(source)==before
        atomic_json(output/'publication-source-check.json',{'source_unchanged':unchanged,'source':str(source),'receipt':str(receipt_path)})
        if not unchanged:raise ValueError('source_changed')
    result=json.loads(receipt_path.read_text())
    if result.get('status')!='seeded':raise ValueError('publication_or_seed_incomplete')
    if receipt_path!=output/'publish-result.json':atomic_json(output/'resumed-result.json',result)
    print(f"[完成] M-Team {result['mteam_torrent_id']}；qB {result['qb_torrent_hash']}；标签 Mteam",flush=True)
    return result
