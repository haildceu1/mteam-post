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
 if not proxy and args.get('proxy_url'):
  proxy={'http':args['proxy_url'],'https':args['proxy_url']}
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
 network_errors={'SSLError','ProxyError','ConnectTimeout','ReadTimeout','ConnectionError','Timeout'}
 reason=str(exc) if str(exc) in allowed else ('network_error' if type(exc).__name__ in network_errors else None)
 print(json.dumps({'ok':False,'error_type':type(exc).__name__,'reason':reason}));sys.exit(2)
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
        # The MoviePilot container shares the host network and the host's
        # Clash listener is reachable on 127.0.0.1:7890. Requests is
        # intentionally trust_env=False in SEARCH_CODE so choose this proxy
        # explicitly rather than silently bypassing it.
        proxy_url=os.environ.get('MTEAM_SEARCH_PROXY_URL','http://127.0.0.1:7890').strip()
        process=subprocess.run(['docker','exec','-i',container,'/opt/venv/bin/python','-',json.dumps({'keyword':keyword,'site_id':site_id,'proxy_url':proxy_url})],
                               input=SEARCH_CODE,text=True,capture_output=True,timeout=680)
    payload=None
    for line in process.stdout.splitlines():
        try: item=json.loads(line)
        except ValueError: continue
        if isinstance(item,dict) and 'ok' in item:payload=item
    if not payload or not payload.get('ok') or not payload.get('complete') or process.returncode:
        code=str((payload or {}).get('api_code',''))
        reason=(payload or {}).get('reason')
        if code=='4': raise ValueError('mteam_rate_limited')
        if reason=='network_error': raise ValueError('mteam_search_network_error')
        if reason in {'site_configuration','search_incomplete','search_changed'}:
            raise ValueError('mteam_search_'+reason)
        raise ValueError('mteam_search_unavailable')
    return payload

def search_identity(package):
    """Use a corroborated title when TMDB is unavailable; never search blank."""
    tmdb = package.get('tmdb') or {}
    # Prefer the title in its original language for site searches.  The
    # localized TMDB ``name`` can be a translated regional title (for example
    # ``Presencia`` for the English film ``Presence``) and miss existing
    # releases indexed under the original title.
    tmdb_name = str(tmdb.get('original_name') or tmdb.get('name') or '').strip()
    if tmdb_name:
        return tmdb_name
    identity = (package.get('identification_evidence') or {}).get('identity') or {}
    douban = package.get('douban_match') or {}
    douban_url = str(package.get('douban_url') or '')
    imdb_url = str(package.get('imdb_url') or '')
    verified_douban = (douban_url.startswith('https://movie.douban.com/subject/')
                       and douban.get('url') == douban_url)
    verified_imdb = bool(re.fullmatch(r'https://www\.imdb\.com/title/tt\d+/?', imdb_url))
    # A fully matched, authenticated Douban subject can itself corroborate the
    # search title when TMDB is absent. Require the canonical URL, exact year,
    # non-empty original title, and a high candidate score; never derive this
    # fallback from a bare URL or a model-invented value.
    douban_title = str(douban.get('original_title') or '').strip()
    douban_year = str(douban.get('year') or '').strip()
    try:
        douban_score = float(douban.get('score') or 0)
    except (TypeError, ValueError):
        douban_score = 0
    try:
        douban_confidence = float(douban.get('selection_confidence') or 0)
    except (TypeError, ValueError):
        douban_confidence = 0
    if (verified_douban and douban_title and package.get('year')
            and douban_year == str(package['year'])
            and (douban_score >= 90 or douban_confidence >= .90)):
        return douban_title
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

def search_keyword(package):
    """Narrow M-Team searches with the verified movie year or TV season.

    Very short titles (for example ``Go``) otherwise return oversized,
    paginated result sets that may never complete. Keep identity validation
    in ``search_identity`` and add only metadata already present in the
    reviewed package.
    """
    title=search_identity(package)
    terms=[title]
    if package.get('kind')=='movie':
        year=str(package.get('year') or '').strip()
        if re.fullmatch(r'\d{4}',year) and not re.search(r'(?<!\d)'+re.escape(year)+r'(?!\d)',title):
            terms.append(year)
    elif package.get('kind')=='tv':
        season=str(package.get('episode') or '').strip()
        if re.fullmatch(r'S\d{2}(?:-S\d{2})?',season,re.I) and not re.search(r'(?<![A-Z0-9])'+re.escape(season)+r'(?!\d)',title,re.I):
            terms.append(season.upper())
    return ' '.join(terms)

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
        size_delta_percent=abs(int(item['size_bytes'])-total_size)/max(total_size,1)*100
        if same_group and size_delta_percent < 1.0:
            duplicates.append({**item,'rule':'same_release_group_size_within_1_percent',
                               'same_release_group':True,
                               'size_delta_percent':size_delta_percent,
                               'detail_url':'https://kp.m-team.cc/detail/'+item['id']})
    return duplicates

def existing_rejected_torrent_id(receipt):
    """Return an ID only for M-Team's explicit existing-torrent rejection."""
    if receipt.get('status') != 'publish_rejected' or str(receipt.get('api_code')) != '1':
        return None
    message = str(receipt.get('api_message') or '')
    match = re.search(r'(?:种子已存在|種子已存在)\s*[（(]\s*([\d,]+)\s*[）)]', message)
    if not match:
        return None
    torrent_id = match[1].replace(',', '')
    return torrent_id if torrent_id.isdigit() and int(torrent_id) > 0 else None

def same_content_info(uploaded_path, official_path):
    """Require identical path/size/piece hashes; M-Team may change only source."""
    from .prepare import _bdecode
    uploaded = _bdecode(Path(uploaded_path).read_bytes())[b'info']
    official = _bdecode(Path(official_path).read_bytes())[b'info']
    keys = set(uploaded) | set(official)
    return all(uploaded.get(key) == official.get(key) for key in keys if key != b'source')

def resume_existing_torrent(package, receipt, receipt_path, options):
    """Recall and seed an explicitly rejected duplicate, gated by exact infohash."""
    torrent_id = existing_rejected_torrent_id(receipt)
    if not torrent_id:
        return None
    from .qb_seed import seed
    from .recall_official import recall
    from .recall_official import validate_manifest

    source = Path(str(package.get('prepared_path') or package.get('input_path') or '')).resolve()
    if source != Path(str(package.get('input_path') or '')).resolve():
        raise ValueError('remain_source_mismatch')
    upload = Path(str((package.get('torrent') or {}).get('path') or ''))
    if not upload.is_file():
        raise ValueError('qb_export_artifact_missing')
    expected = validate_manifest(upload.read_bytes(), source)
    output = receipt_path.parent / f'official-existing-{torrent_id}.torrent'
    official = recall(torrent_id, source=source, output=output,
                      container=options.moviepilot_container, site_id=options.site_id)
    if not same_content_info(upload, output):
        raise ValueError('existing_torrent_content_mismatch')
    seeded = seed(output, source, container=options.qb_container,
                  url=options.qb_url, config=options.qb_config)
    result = {
        **receipt,
        'status': 'seeded',
        'publication_status': 'already_exists',
        'existing_mteam_torrent': True,
        'uploaded_info_hash': expected.get('info_hash'),
        'official_info_hash': official.get('info_hash'),
        'info_hash_differs_only_by_source': official.get('info_hash') != expected.get('info_hash'),
        'mteam_torrent_id': torrent_id,
        'mteam_detail_url': f'https://kp.m-team.cc/detail/{torrent_id}',
        'official_torrent': official,
        'qb_torrent_hash': seeded['qb_torrent_hash'],
        'seed_status': seeded['status'],
    }
    atomic_json(receipt_path, result)
    print(f"[已存在] M-Team {torrent_id} 的官方种子与上传 infohash、源文件清单一致。", flush=True)
    print(f"[完成] 官方 announce 已写入 qB 并移除 TL tracker；qB {seeded['qb_torrent_hash']}。", flush=True)
    return result

def recent_duplicate_check(path, package, total_size, *, max_age=900):
    """Reuse only a fresh, complete, internally consistent positive search result."""
    path=Path(path)
    try:
        age=time.time()-path.stat().st_mtime
        if age < -60 or age > max_age:
            return None
        data=json.loads(path.read_text())
        candidates=data.get('candidates')
        if (data.get('ok') is not True or data.get('complete') is not True
                or data.get('keyword') != search_keyword(package)
                or not isinstance(candidates,list) or int(data.get('total',-1)) != len(candidates)):
            return None
        recomputed=duplicate_evidence(package,candidates,total_size)
        saved=data.get('matches')
        if not isinstance(saved,list) or len(saved)!=len(recomputed):
            return None
        saved_by_id={str(item.get('id')):item for item in saved if isinstance(item,dict)}
        for item in recomputed:
            prior=saved_by_id.get(item['id'])
            if not prior or any(prior.get(key)!=item.get(key) for key in
                                ('title','size_bytes','rule','same_release_group')):
                return None
        return {'search':data,'matches':recomputed}
    except (OSError,ValueError,TypeError,KeyError):
        return None

def resume_search_duplicate(package, package_path, output, duplicate, receipt_path, options, before):
    """Recall a search-confirmed release and validate its metadata before seeding."""
    from .qb_seed import seed
    from .recall_official import recall,validate_manifest,verify_piece_hashes
    from .autonomous import files_snapshot

    torrent_id=str(duplicate.get('id') or '')
    if not torrent_id.isdigit() or int(torrent_id)<=0:
        raise ValueError('mteam_duplicate_id_invalid')
    source=Path(str(package.get('input_path') or '')).resolve()
    if source!=Path(str(package.get('prepared_path') or '')).resolve():
        raise ValueError('remain_source_mismatch')
    upload=Path(str((package.get('torrent') or {}).get('path') or ''))
    if not upload.is_file() or upload.is_symlink():
        raise ValueError('qb_export_artifact_missing')
    upload_content=upload.read_bytes()
    expected=validate_manifest(upload_content,source)
    expected_hash=str((package.get('torrent') or {}).get('info_hash') or '').lower()
    if expected_hash and expected.get('info_hash')!=expected_hash:
        raise ValueError('qb_export_infohash_mismatch')
    if int(duplicate.get('size_bytes') or -1)!=expected['size_bytes']:
        raise ValueError('mteam_duplicate_size_changed')

    live=source_tasks(source,qb_url=options.qb_url,qb_config=options.qb_config,
                      qb_container=options.qb_container)
    original=next((task for task in live if str(task.get('hash','')).lower()==expected['info_hash']),None)
    if (not original or original.get('mteam') or float(original.get('progress') or 0)<1
            or int(original.get('size') or -1)!=expected['size_bytes']):
        raise ValueError('original_tl_task_not_verified')

    official_path=Path(output)/f'official-existing-{torrent_id}.torrent'
    print(f"[已存在] M-Team {torrent_id}；通过 MoviePilot 召回官方种子并核对内容…",flush=True)
    official=recall(torrent_id,source=source,output=official_path,
                    container=options.moviepilot_container,site_id=options.site_id,
                    proxy_url=os.environ.get('MTEAM_RECALL_PROXY_URL'))
    skip_piece_verification=bool(getattr(options,'skip_piece_verification',False))
    official_proof=None
    if skip_piece_verification:
        if not same_content_info(upload,official_path):
            official_proof={'status':'skipped_by_user','official_info_matches_upload':False,
                            'size_bytes':expected['size_bytes']}
        else:
            official_proof={'status':'skipped_by_user','official_info_matches_upload':True,
                            'size_bytes':expected['size_bytes'],
                            'reason':'user_requested_skip; torrent info dictionaries match except source'}
    else:
        try:
            def show_hash_progress(done,total):
                print(f'[校验] M-Team 官方种子分片 SHA-1：{done/1024**3:.1f}/{total/1024**3:.1f} GiB',flush=True)
            official_proof=verify_piece_hashes(official_path.read_bytes(),source,progress=show_hash_progress)
        except ValueError:
            official_proof=None
    if official_proof is None or (skip_piece_verification and not official_proof.get('official_info_matches_upload')):
        result={'status':'blocked','reason':'existing_torrent_content_mismatch',
                'mteam_torrent_id':torrent_id,'detail_url':f'https://kp.m-team.cc/detail/{torrent_id}',
                'uploaded_info_hash':expected['info_hash'],'official_info_hash':official.get('info_hash'),
                'source_unchanged':files_snapshot(source)==before}
        atomic_json(Path(output)/'duplicate-recall-result.json',result)
        raise ValueError('existing_torrent_content_mismatch')
    if expected['size_bytes']!=official_proof['size_bytes']:
        raise ValueError('existing_torrent_content_mismatch')
    if files_snapshot(source)!=before:
        raise ValueError('source_changed')

    live=source_tasks(source,qb_url=options.qb_url,qb_config=options.qb_config,
                      qb_container=options.qb_container)
    still_original=next((task for task in live if str(task.get('hash','')).lower()==expected['info_hash']),None)
    if (not still_original or still_original.get('mteam') or
            float(still_original.get('progress') or 0)<1 or
            int(still_original.get('size') or -1)!=expected['size_bytes']):
        raise ValueError('original_tl_task_changed')
    conflicts=[task for task in live if task.get('mteam') and
               str(task.get('hash','')).lower()!=str(official.get('info_hash','')).lower()]
    if conflicts:
        atomic_json(Path(output)/'duplicate-recall-result.json',{
            'status':'blocked','reason':'different_mteam_task_at_source_path',
            'mteam_torrent_id':torrent_id,'source_unchanged':True})
        raise ValueError('different_mteam_task_at_source_path')

    atomic_json(STATE_ROOT/(resource_key(source)+'.json'),{
        'source':str(source),'package':str(Path(package_path).resolve()),
        'receipt':str(Path(receipt_path).resolve())})
    if skip_piece_verification:
        print('[核对] 上传与官网 torrent 的 info 内容相同；按用户要求跳过本地分片重读，准备加入 qB。',flush=True)
    else:
        print('[核对] 上传种子与官网种子的所有分片均通过源文件 SHA-1 校验；准备以官方种子加入 qB。',flush=True)
    try:
        seeded=seed(official_path,source,container=options.qb_container,
                    url=options.qb_url,config=options.qb_config)
    except Exception as exc:
        result={'status':'seed_pending','reason':type(exc).__name__,
                'mteam_torrent_id':torrent_id,'detail_url':f'https://kp.m-team.cc/detail/{torrent_id}',
                'official_info_hash':official.get('info_hash'),'source_unchanged':files_snapshot(source)==before}
        atomic_json(Path(output)/'duplicate-recall-result.json',result)
        raise
    if files_snapshot(source)!=before:
        raise ValueError('source_changed')
    result={
        'status':'seeded','publication_status':'already_exists','existing_mteam_torrent':True,
        'mteam_torrent_id':torrent_id,'mteam_detail_url':f'https://kp.m-team.cc/detail/{torrent_id}',
        'matched_candidate':{key:duplicate.get(key) for key in ('id','title','size_bytes','rule','same_release_group')},
        'uploaded_info_hash':expected['info_hash'],'official_info_hash':official.get('info_hash'),
        'piece_validation':{'official':official_proof,'original_tl_qb_hash_verified':True,
                            'original_tl_qb_state':original.get('state')},
        'official_torrent':official,'qb_torrent_hash':seeded['qb_torrent_hash'],
        'seed_status':seeded['status'],'source_unchanged':True,
    }
    atomic_json(receipt_path,result)
    atomic_json(Path(output)/'duplicate-recall-result.json',result)
    review_path=Path(output)/'review.json'
    if review_path.is_file():
        try:
            report=json.loads(review_path.read_text())
            report.update(status='seeded',mode='submit',blockers=[],
                          publication_status='already_exists',source_unchanged=True,
                          mteam_torrent_id=torrent_id,qb_torrent_hash=seeded['qb_torrent_hash'])
            atomic_json(review_path,report)
        except (OSError,ValueError,TypeError):
            pass
    print(f"[完成] 复用 M-Team {torrent_id}；qB {seeded['qb_torrent_hash']} 已带 Mteam 标签做种。",flush=True)
    return result

def source_tasks(source, *, qb_url=None, qb_config=None, qb_container='qbittorrent'):
    from .qb_seed import mapped_source
    from .seed_official import _qb_api,_qb_config_key,DEFAULT_QB_URL,DEFAULT_QB_CONFIG
    from urllib.parse import urlsplit
    base=(qb_url or DEFAULT_QB_URL).rstrip('/')
    source=Path(source).absolute()
    mapped_path=mapped_source(source,qb_container)
    mapped=mapped_path.as_posix()
    accepted_content_paths={mapped}
    if source.is_dir():
        files=[path for path in source.rglob('*') if path.is_file() and not path.is_symlink()]
        if len(files)==1:
            accepted_content_paths.add((mapped_path/files[0].relative_to(source)).as_posix())
    session=_qb_api(base,_qb_config_key(qb_config or DEFAULT_QB_CONFIG))
    try:
        response=session.get(base+'/api/v2/torrents/info',timeout=20);response.raise_for_status()
        records=[]
        for task in response.json():
            if str(task.get('content_path','')).rstrip('/') not in accepted_content_paths:continue
            response=session.get(base+'/api/v2/torrents/trackers',params={'hash':task['hash']},timeout=10);response.raise_for_status()
            hosts=[urlsplit(row.get('url','')).hostname or '' for row in response.json()]
            record={key:task.get(key) for key in ('hash','name','size','progress','state','save_path','content_path','tags')}
            record['mteam']=any(host=='m-team.cc' or host.endswith('.m-team.cc')
                                or host=='m-team.io' or host.endswith('.m-team.io')
                                for host in hosts) or 'mteam' in str(task.get('tags','')).lower()
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
    if receipt:
        recovered=resume_existing_torrent(package,receipt,receipt_path,options)
        if recovered:
            if receipt_path!=output/'publish-result.json':atomic_json(output/'resumed-result.json',recovered)
            return recovered
        if receipt.get('status')=='seeded' and receipt.get('mteam_torrent_id') and receipt.get('qb_torrent_hash'):
            live=source_tasks(source,qb_url=options.qb_url,qb_config=options.qb_config,
                              qb_container=options.qb_container)
            task=next((item for item in live if str(item.get('hash','')).lower()==
                       str(receipt['qb_torrent_hash']).lower()),None)
            if task and task.get('mteam') and float(task.get('progress') or 0)>=1:
                return receipt
            raise ValueError('seed_receipt_out_of_sync')
    decision=operation_decision(receipt)
    if decision not in ('resume_recall_only','no_prior_submission','retry_pre_dispatch'):raise ValueError(decision)
    before=files_snapshot(source)
    if not receipt.get('mteam_torrent_id'):
        tasks=source_tasks(source,qb_url=options.qb_url,qb_config=options.qb_config,qb_container=options.qb_container)
        atomic_json(output/'qb-before.json',tasks)
        if any(t['mteam'] for t in tasks):raise ValueError('already_in_qb')
        total_size=sum(r['bytes'] for r in before)
        cached=recent_duplicate_check(output/'duplicate-check.json',package,total_size)
        if cached:
            result=cached['search'];duplicates=cached['matches']
            print('[查重] 复用 15 分钟内已完成且完整的唯一性证据。',flush=True)
        else:
            keyword=search_keyword(package)
            print('[阶段] M-Team 发布前查重：'+keyword,flush=True)
            result=search_site(keyword,container=options.moviepilot_container,site_id=options.site_id)
            duplicates=duplicate_evidence(package,result['candidates'],total_size)
            atomic_json(output/'duplicate-check.json',{**result,'keyword':keyword,'matches':duplicates})
        if duplicates:
            # A near-size same-group match blocks a new upload, but cannot be
            # recalled as the source torrent unless its size is exact. When
            # there is one exact match, validate and reuse its official torrent.
            if len(duplicates)!=1:raise ValueError('mteam_duplicate')
            if int(duplicates[0].get('size_bytes') or -1)!=total_size:
                raise ValueError('mteam_duplicate')
            return resume_search_duplicate(package,package_path,output,duplicates[0],receipt_path,options,before)
    atomic_json(STATE_ROOT/(resource_key(source)+'.json'),{'source':str(source.resolve()),'package':str(package_path.resolve()),'receipt':str(receipt_path.resolve())})
    if files_snapshot(source)!=before:raise ValueError('source_changed')
    command=[str(package_path),'--yes','--submit','--recall-official','--result-json',str(receipt_path),
             '--moviepilot-container',options.moviepilot_container,'--site-id',str(options.site_id),
             '--qb-container',options.qb_container,'--qb-url',options.qb_url,'--qb-config',str(options.qb_config),
             '--login-timeout',str(options.login_timeout)]
    if options.profile_dir:command.extend(['--profile-dir',str(options.profile_dir)])
    if getattr(options,'credentials_file',None):command.extend(['--credentials-file',str(options.credentials_file)])
    print('[阶段] 自动发布 → 官方种子召回 → 暂停加种与路径验证 → Mteam 标签做种',flush=True)
    # Chrome's Unix-domain socket paths must fit sockaddr_un (108 bytes).
    # A deeply nested task TMPDIR can make Chrome exit before creating a session.
    chrome_tmp=Path(os.environ.get('MTEAM_CHROME_TMPDIR','/data/Zhyw/media-stack/.chrome-tmp'))/('auto-'+resource_key(source)[:8])
    if len(os.fsencode(chrome_tmp))>65:raise ValueError('chrome_temp_path_too_long')
    chrome_tmp.mkdir(parents=True,exist_ok=True,mode=0o700)
    previous_tmp=os.environ.get('TMPDIR');previous_tempdir=tempfile.tempdir
    os.environ['TMPDIR']=str(chrome_tmp);tempfile.tempdir=str(chrome_tmp)
    publish_failure=None
    try:publish_main(command)
    except SystemExit as exc:publish_failure=exc
    finally:
        if previous_tmp is None:os.environ.pop('TMPDIR',None)
        else:os.environ['TMPDIR']=previous_tmp
        tempfile.tempdir=previous_tempdir
        unchanged=files_snapshot(source)==before
        atomic_json(output/'publication-source-check.json',{'source_unchanged':unchanged,'source':str(source),'receipt':str(receipt_path)})
        if not unchanged:raise ValueError('source_changed')
    result=json.loads(receipt_path.read_text())
    recovered=resume_existing_torrent(package,result,receipt_path,options)
    if recovered:
        if receipt_path!=output/'publish-result.json':atomic_json(output/'resumed-result.json',recovered)
        return recovered
    if publish_failure:raise RuntimeError('browser_or_recall_failed') from publish_failure
    if result.get('status')!='seeded':raise ValueError('publication_or_seed_incomplete')
    if receipt_path!=output/'publish-result.json':atomic_json(output/'resumed-result.json',result)
    print(f"[完成] M-Team {result['mteam_torrent_id']}；qB {result['qb_torrent_hash']}；标签 Mteam",flush=True)
    return result
