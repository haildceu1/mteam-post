import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from media_title_renamer import auto_publish as ap

@pytest.fixture
def isolated(tmp_path,monkeypatch):
    monkeypatch.setattr(ap,'STATE_ROOT',tmp_path/'state')
    return tmp_path

def package():
    return {'tmdb':{'name':'Westworld'},'kind':'tv','episode':'S03','group':'GROUP'}

def test_search_keyword_adds_verified_movie_year_for_short_titles():
    assert ap.search_keyword({'tmdb':{'name':'Go'},'kind':'movie','year':'1999'})=='Go 1999'

def test_search_keyword_does_not_duplicate_year_or_tv_season():
    assert ap.search_keyword({'tmdb':{'name':'The Last Boy Scout 1991'},
                              'kind':'movie','year':'1991'})=='The Last Boy Scout 1991'
    assert ap.search_keyword({'tmdb':{'name':'Westworld'},'kind':'tv',
                              'episode':'S03'})=='Westworld S03'
    assert ap.search_keyword({'tmdb':{'name':'Friends'},'kind':'tv',
                              'episode':'S01-S10'})=='Friends S01-S10'

def test_verified_douban_match_supplies_search_identity_without_tmdb():
    package={'tmdb':None,'kind':'movie','year':'2024',
             'douban_url':'https://movie.douban.com/subject/36903421/',
             'douban_match':{'url':'https://movie.douban.com/subject/36903421/',
                             'original_title':'Lick','year':'2024','score':100}}
    assert ap.search_identity(package)=='Lick'
    assert ap.search_keyword(package)=='Lick 2024'
    package['douban_match'].pop('score')
    package['douban_match']['selection_confidence']=.96
    assert ap.search_identity(package)=='Lick'
    package['douban_match']['year']='2023'
    with pytest.raises(ValueError,match='search_identity_missing'):
        ap.search_identity(package)

def test_duplicate_requires_media_and_season_identity():
    rows=[{'id':'1','title':'Westworld S03 1080p BluRay-GROUP','size_bytes':50},
          {'id':'2','title':'Westworld S02 1080p BluRay-GROUP','size_bytes':10},
          {'id':'3','title':'Otherworld S03 1080p BluRay-GROUP','size_bytes':10},
          {'id':'4','title':'Westworld S03 1080p WEB-DL-OTHER','size_bytes':10}]
    assert [r['id'] for r in ap.duplicate_evidence(package(),rows,10)]==['4']


def test_same_release_group_with_different_size_is_not_duplicate():
    row={'id':'5','title':'Westworld S03 2160p BluRay-GROUP','size_bytes':50}
    assert ap.duplicate_evidence(package(),[row],10)==[]

def test_same_size_candidate_is_recorded_without_signed_urls():
    row={'id':'4','title':'Westworld S03 1080p WEB-DL-OTHER','size_bytes':10}
    evidence=ap.duplicate_evidence(package(),[row],10)
    assert evidence[0]['rule']=='same_size'
    assert evidence[0]['detail_url']=='https://kp.m-team.cc/detail/4'

@pytest.mark.parametrize(('status','code','message','expected'),[
    ('publish_rejected','1','種子已存在(1,257,901)','1257901'),
    ('publish_rejected','4','rate limited',None),
    ('publish_rejected','1','欄位錯誤',None),
    ('publish_ambiguous','1','種子已存在(123)',None),
])
def test_only_explicit_existing_torrent_rejection_yields_id(status,code,message,expected):
    assert ap.existing_rejected_torrent_id({'status':status,'api_code':code,'api_message':message})==expected

def test_existing_torrent_allows_only_tracker_source_metadata_difference(tmp_path):
    from media_title_renamer.prepare import _bencode
    upload=tmp_path/'upload.torrent';official=tmp_path/'official.torrent'
    common={b'length':1,b'name':b'a.mkv',b'piece length':1,b'pieces':b'x'*20,b'private':1}
    upload.write_bytes(_bencode({b'info':{**common,b'source':b'TL'}}))
    official.write_bytes(_bencode({b'info':{**common,b'source':b'MTeam'}}))
    assert ap.same_content_info(upload,official)
    official.write_bytes(_bencode({b'info':{**common,b'pieces':b'y'*20,b'source':b'MTeam'}}))
    assert not ap.same_content_info(upload,official)

def test_existing_torrent_resume_requires_official_infohash_match(isolated,monkeypatch):
    from media_title_renamer import qb_seed, recall_official
    source=isolated/'source.mkv';source.write_bytes(b'media')
    upload=isolated/'qb-upload.torrent';upload.write_bytes(b'fixture')
    receipt_path=isolated/'publish-result.json'
    receipt={'status':'publish_rejected','api_code':'1','api_message':'種子已存在(123)'}
    package={'input_path':str(source),'prepared_path':str(source),
             'torrent':{'path':str(upload),'info_hash':'a'*40}}
    calls=[]
    monkeypatch.setattr(recall_official,'validate_manifest',lambda content,path:{'info_hash':'a'*40})
    monkeypatch.setattr(recall_official,'recall',lambda *a,**kw:{'info_hash':'b'*40})
    monkeypatch.setattr(ap,'same_content_info',lambda *a:False)
    monkeypatch.setattr(qb_seed,'seed',lambda *a,**kw:calls.append('seed'))
    with pytest.raises(ValueError,match='existing_torrent_content_mismatch'):
        ap.resume_existing_torrent(package,receipt,receipt_path,
            SimpleNamespace(moviepilot_container='mp',site_id=1,qb_container='qb',
                             qb_url='http://local',qb_config=Path('cfg')))
    assert calls==[]

def test_existing_torrent_resume_seeds_only_after_exact_infohash_match(isolated,monkeypatch):
    from media_title_renamer import qb_seed, recall_official
    source=isolated/'source.mkv';source.write_bytes(b'media')
    upload=isolated/'qb-upload.torrent';upload.write_bytes(b'fixture')
    receipt_path=isolated/'publish-result.json'
    receipt={'status':'publish_rejected','api_code':'1','api_message':'種子已存在(123)'}
    package={'input_path':str(source),'prepared_path':str(source),
             'torrent':{'path':str(upload),'info_hash':'a'*40}}
    calls=[]
    monkeypatch.setattr(recall_official,'validate_manifest',lambda content,path:{'info_hash':'a'*40})
    monkeypatch.setattr(recall_official,'recall',lambda *a,**kw:{'info_hash':'a'*40,'mteam_torrent_id':'123'})
    monkeypatch.setattr(ap,'same_content_info',lambda *a:True)
    monkeypatch.setattr(qb_seed,'seed',lambda *a,**kw:calls.append('seed') or
                        {'qb_torrent_hash':'b'*40,'status':'seeded'})
    result=ap.resume_existing_torrent(package,receipt,receipt_path,
        SimpleNamespace(moviepilot_container='mp',site_id=1,qb_container='qb',
                         qb_url='http://local',qb_config=Path('cfg')))
    assert calls==['seed']
    assert result['status']=='seeded' and result['existing_mteam_torrent']
    assert result['mteam_torrent_id']=='123' and result['qb_torrent_hash']=='b'*40
    assert json.loads(receipt_path.read_text())['publication_status']=='already_exists'

def test_recent_complete_duplicate_check_is_recomputed_before_reuse(tmp_path):
    import time
    package={'tmdb':{'name':'The Last Boy Scout'},'kind':'movie','year':'1991','group':'MTeam'}
    candidates=[{'id':'1263946','title':'The Last Boy Scout 1991 UHD BluRay-MTeam','size_bytes':100}]
    matches=ap.duplicate_evidence(package,candidates,100)
    path=tmp_path/'duplicate-check.json'
    path.write_text(json.dumps({'ok':True,'complete':True,'keyword':'The Last Boy Scout 1991',
        'total':1,'candidates':candidates,'matches':matches}))
    os.utime(path,(time.time(),time.time()))
    cached=ap.recent_duplicate_check(path,package,100)
    assert cached and [row['id'] for row in cached['matches']]==['1263946']
    path.write_text(json.dumps({'ok':True,'complete':False,'keyword':'The Last Boy Scout 1991',
        'total':1,'candidates':candidates,'matches':matches}))
    assert ap.recent_duplicate_check(path,package,100) is None

def test_search_duplicate_recall_seeds_only_exact_official_content(isolated,monkeypatch):
    from media_title_renamer import qb_seed,recall_official,autonomous
    source=isolated/'source';source.mkdir();(source/'disc.mkv').write_bytes(b'media')
    upload=isolated/'upload.torrent';upload.write_bytes(b'fixture')
    package={'input_path':str(source),'prepared_path':str(source),
        'torrent':{'path':str(upload),'info_hash':'a'*40}}
    package_path=isolated/'mteam-prepare.json';package_path.write_text('{}')
    receipt_path=isolated/'publish-result.json'
    options=SimpleNamespace(moviepilot_container='mp',site_id=1,qb_container='qb',
        qb_url='http://local',qb_config=Path('cfg'))
    calls=[]
    monkeypatch.setattr(recall_official,'validate_manifest',lambda content,path:
        {'info_hash':'a'*40,'size_bytes':100})
    monkeypatch.setattr(recall_official,'verify_piece_hashes',lambda content,path,**kwargs:
        {'info_hash':'a'*40,'size_bytes':100,'pieces_verified':1})
    def fake_recall(value,*,source,output,**kwargs):
        Path(output).write_bytes(b'official')
        return {'info_hash':'b'*40,'mteam_torrent_id':'123'}
    monkeypatch.setattr(recall_official,'recall',fake_recall)
    monkeypatch.setattr(autonomous,'files_snapshot',lambda path:['unchanged'])
    monkeypatch.setattr(ap,'source_tasks',lambda *a,**kw:[{'hash':'a'*40,'size':100,
        'progress':1,'state':'stalledUP','mteam':False}])
    monkeypatch.setattr(qb_seed,'seed',lambda *a,**kw:calls.append('seed') or
        {'qb_torrent_hash':'c'*40,'status':'seeded'})
    result=ap.resume_search_duplicate(package,package_path,isolated,{'id':'123','title':'Film',
        'size_bytes':100,'rule':'same_size','same_release_group':True},receipt_path,options,['unchanged'])
    assert calls==['seed']
    assert result['status']=='seeded' and result['publication_status']=='already_exists'
    assert result['mteam_torrent_id']=='123' and result['source_unchanged']

def test_search_duplicate_recall_never_seeds_content_mismatch(isolated,monkeypatch):
    from media_title_renamer import qb_seed,recall_official,autonomous
    source=isolated/'source';source.mkdir();(source/'disc.mkv').write_bytes(b'media')
    upload=isolated/'upload.torrent';upload.write_bytes(b'fixture')
    package={'input_path':str(source),'prepared_path':str(source),
        'torrent':{'path':str(upload),'info_hash':'a'*40}}
    options=SimpleNamespace(moviepilot_container='mp',site_id=1,qb_container='qb',
        qb_url='http://local',qb_config=Path('cfg'))
    calls=[]
    monkeypatch.setattr(recall_official,'validate_manifest',lambda content,path:
        {'info_hash':'a'*40,'size_bytes':100})
    monkeypatch.setattr(recall_official,'verify_piece_hashes',lambda *a,**kw:
        (_ for _ in ()).throw(ValueError('torrent_piece_hash_mismatch')))
    def fake_recall(value,*,source,output,**kwargs):
        Path(output).write_bytes(b'official')
        return {'info_hash':'b'*40}
    monkeypatch.setattr(recall_official,'recall',fake_recall)
    monkeypatch.setattr(autonomous,'files_snapshot',lambda path:['unchanged'])
    monkeypatch.setattr(ap,'source_tasks',lambda *a,**kw:[{'hash':'a'*40,'size':100,
        'progress':1,'state':'stalledUP','mteam':False}])
    monkeypatch.setattr(qb_seed,'seed',lambda *a,**kw:calls.append('seed'))
    with pytest.raises(ValueError,match='existing_torrent_content_mismatch'):
        ap.resume_search_duplicate(package,isolated/'package.json',isolated,
            {'id':'123','size_bytes':100},isolated/'result.json',options,['unchanged'])
    assert calls==[]
    assert json.loads((isolated/'duplicate-recall-result.json').read_text())['status']=='blocked'

def test_explicit_existing_torrent_rejection_recovers_without_second_submit(isolated,monkeypatch):
    from media_title_renamer import publish, qb_seed, recall_official
    source=isolated/'source.mkv';source.write_bytes(b'media')
    upload=isolated/'qb-upload.torrent';upload.write_bytes(b'fixture')
    output=isolated/'task';output.mkdir()
    package_path=output/'mteam-prepare.json'
    package_path.write_text(json.dumps({'input_path':str(source),'prepared_path':str(source),
        'torrent':{'path':str(upload),'info_hash':'a'*40}}))
    monkeypatch.setattr(ap,'source_tasks',lambda *a,**kw:[])
    monkeypatch.setattr(ap,'search_identity',lambda pkg:'Test Film')
    monkeypatch.setattr(ap,'search_site',lambda *a,**kw:{'ok':True,'complete':True,'total':0,'candidates':[]})
    monkeypatch.setattr(ap,'duplicate_evidence',lambda *a:[])
    monkeypatch.setattr(recall_official,'validate_manifest',lambda content,path:{'info_hash':'a'*40})
    monkeypatch.setattr(recall_official,'recall',lambda *a,**kw:{'info_hash':'b'*40,'mteam_torrent_id':'123'})
    monkeypatch.setattr(ap,'same_content_info',lambda *a:True)
    monkeypatch.setattr(qb_seed,'seed',lambda *a,**kw:{'qb_torrent_hash':'b'*40,'status':'seeded'})
    submits=[]
    def rejected(args):
        submits.append(1)
        receipt=Path(args[args.index('--result-json')+1])
        receipt.write_text(json.dumps({'status':'publish_rejected','api_code':'1',
            'api_message':'種子已存在(123)','submit_attempted_at':123}))
        raise SystemExit(2)
    monkeypatch.setattr(publish,'main',rejected)
    options=SimpleNamespace(qb_url='http://local',qb_config=Path('cfg'),qb_container='qb',
        moviepilot_container='mp',site_id=1,login_timeout=30,profile_dir=None)
    result=ap.publish_and_seed(package_path,output,options)
    assert len(submits)==1
    assert result['status']=='seeded' and result['mteam_torrent_id']=='123'
    assert result['publication_status']=='already_exists'


def test_tmdb_free_search_requires_corroborated_identity():
    pkg = {'tmdb': None, 'kind': 'movie', 'year': '2026', 'group': 'JustWatch',
           'douban_url': 'https://movie.douban.com/subject/37447627/',
           'douban_match': {'url': 'https://movie.douban.com/subject/37447627/'},
           'identification_evidence': {'identity': {
               'kind': 'movie', 'year': '2026', 'confidence': .98,
               'search_title': 'Billie Eilish Hit Me Hard and Soft The Tour'}}}
    assert ap.search_identity(pkg) == 'Billie Eilish Hit Me Hard and Soft The Tour'
    rows = [{'id': '123', 'title': 'Billie Eilish Hit Me Hard and Soft The Tour 2026 1080p-JustWatch', 'size_bytes': 10}]
    assert ap.duplicate_evidence(pkg, rows, 10)[0]['rule'] == 'same_size'
    pkg['douban_match']['url'] = 'https://movie.douban.com/subject/other/'
    with pytest.raises(ValueError, match='search_identity_missing'):
        ap.search_identity(pkg)
    pkg['douban_match']['url'] = pkg['douban_url']
    pkg['identification_evidence']['identity']['confidence'] = .4
    with pytest.raises(ValueError, match='search_identity_missing'):
        ap.search_identity(pkg)

def test_lock_blocks_duplicate_click_or_process(isolated):
    with ap.task_lock(isolated/'video'):
        with pytest.raises(ValueError,match='resource_busy'):
            with ap.task_lock(isolated/'video'): pass

@pytest.mark.parametrize('payload,code',[
    ({'ok':True,'complete':True,'total':0,'candidates':[]},None),
    ({'ok':False,'api_code':'4'},'mteam_rate_limited'),
    ({'ok':True,'complete':False},'mteam_search_unavailable')])
def test_search_empty_success_distinct_from_error(isolated,monkeypatch,payload,code):
    process=SimpleNamespace(stdout=json.dumps(payload),returncode=0 if payload.get('ok') else 2)
    monkeypatch.setattr(ap.subprocess,'run',lambda *a,**kw:process)
    if code:
        with pytest.raises(ValueError,match=code):ap.search_site('Westworld')
    else:assert ap.search_site('Westworld')['total']==0

def test_search_uses_explicit_clash_proxy_without_logging_credentials(isolated,monkeypatch):
    monkeypatch.setenv('MTEAM_SEARCH_PROXY_URL','http://127.0.0.1:7890')
    process=SimpleNamespace(stdout=json.dumps({'ok':True,'complete':True,'total':0,'candidates':[]}),returncode=0)
    captured={}
    def fake_run(command,**kwargs):
        captured['args']=json.loads(command[-1])
        captured['code']=kwargs['input']
        return process
    monkeypatch.setattr(ap.subprocess,'run',fake_run)

    assert ap.search_site('The Last Boy Scout')['total']==0
    assert captured['args']['proxy_url']=='http://127.0.0.1:7890'
    assert 'apikey' not in captured['args']
    assert "proxies=proxy" in captured['code']

def test_known_published_resource_resumes_without_search_or_new_qb_detection(isolated,monkeypatch):
    from media_title_renamer import publish
    source=isolated/'video.mkv';source.write_bytes(b'media')
    output=isolated/'old';output.mkdir()
    pkg=output/'mteam-prepare.json';pkg.write_text(json.dumps({'input_path':str(source),'prepared_path':str(source)}))
    receipt=output/'publish-result.json';receipt.write_text(json.dumps({'mteam_torrent_id':'123','status':'recall_failed'}))
    ap.atomic_json(ap.STATE_ROOT/(ap.resource_key(source)+'.json'),{'source':str(source),'package':str(pkg),'receipt':str(receipt)})
    def forbidden(*a,**kw):pytest.fail('resume must not search/publish new torrent')
    monkeypatch.setattr(ap,'search_site',forbidden);monkeypatch.setattr(ap,'source_tasks',forbidden)
    def fake_publish(args):
        assert str(receipt) in args
        receipt.write_text(json.dumps({'status':'seeded','mteam_torrent_id':'123','qb_torrent_hash':'a'*40}))
    monkeypatch.setattr(publish,'main',fake_publish)
    options=SimpleNamespace(qb_url='http://local',qb_config=Path('cfg'),qb_container='qb',moviepilot_container='mp',site_id=1,login_timeout=30,profile_dir=None)
    resumed=isolated/'new';resumed.mkdir()
    assert ap.publish_and_seed(pkg,resumed,options)['status']=='seeded'
    assert (resumed/'resumed-result.json').is_file()


def test_seeded_receipt_returns_only_when_live_mteam_qb_task_is_verified(isolated,monkeypatch):
    from media_title_renamer import publish
    source=isolated/'source.mkv';source.write_bytes(b'media')
    output=isolated/'task';output.mkdir()
    package_path=output/'mteam-prepare.json'
    package_path.write_text(json.dumps({'input_path':str(source),'prepared_path':str(source)}))
    receipt={'status':'seeded','mteam_torrent_id':'123','qb_torrent_hash':'a'*40}
    (output/'publish-result.json').write_text(json.dumps(receipt))
    options=SimpleNamespace(qb_url='http://local',qb_config=Path('cfg'),qb_container='qb',
        moviepilot_container='mp',site_id=1)
    monkeypatch.setattr(ap,'source_tasks',lambda *a,**kw:[{'hash':'a'*40,'progress':1,'mteam':True}])
    monkeypatch.setattr(publish,'main',lambda *a,**kw:pytest.fail('must not publish twice'))
    assert ap.publish_and_seed(package_path,output,options)==receipt
    monkeypatch.setattr(ap,'source_tasks',lambda *a,**kw:[])
    with pytest.raises(ValueError,match='seed_receipt_out_of_sync'):
        ap.publish_and_seed(package_path,output,options)


def test_source_tasks_matches_one_file_directory_qb_content_path(tmp_path, monkeypatch):
    from media_title_renamer import qb_seed, seed_official
    source = tmp_path / 'Film'
    source.mkdir()
    (source / 'disc.iso').write_bytes(b'media')
    monkeypatch.setattr(qb_seed, 'mapped_source', lambda *_: Path('/downloads/Film'))

    class Session:
        def get(self, url, **kwargs):
            if url.endswith('/torrents/info'):
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [{
                    'hash': 'a' * 40, 'content_path': '/downloads/Film/disc.iso',
                    'name': 'Film', 'size': 5, 'progress': 1, 'state': 'stalledUP',
                    'save_path': '/downloads', 'tags': 'Mteam'}])
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: [
                {'url': 'https://tracker.m-team.io/announce'}])
        def close(self):
            pass

    monkeypatch.setattr(seed_official, '_qb_api', lambda *_: Session())
    monkeypatch.setattr(seed_official, '_qb_config_key', lambda *_: 'not-a-real-key')
    records = ap.source_tasks(source, qb_url='http://local', qb_config=tmp_path / 'cfg', qb_container='qb')
    assert len(records) == 1
    assert records[0]['mteam'] is True
