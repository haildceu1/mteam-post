import json
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

def test_duplicate_requires_media_and_season_identity():
    rows=[{'id':'1','title':'Westworld S03 1080p BluRay-GROUP','size_bytes':50},
          {'id':'2','title':'Westworld S02 1080p BluRay-GROUP','size_bytes':10},
          {'id':'3','title':'Otherworld S03 1080p BluRay-GROUP','size_bytes':10}]
    assert [r['id'] for r in ap.duplicate_evidence(package(),rows,10)]==['1']

def test_same_size_candidate_is_recorded_without_signed_urls():
    row={'id':'4','title':'Westworld S03 1080p WEB-DL-OTHER','size_bytes':10}
    evidence=ap.duplicate_evidence(package(),[row],10)
    assert evidence[0]['rule']=='same_size'
    assert evidence[0]['detail_url']=='https://kp.m-team.cc/detail/4'


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
