import copy
import json
import pytest
from media_title_renamer.autonomous import (VERSION, validate_output, evidence,
    apply_review, operation_decision, files_snapshot, deterministic_category, normalize_draft, main)


@pytest.fixture
def data():
    package={'input_path':'/media/Westworld.S03-GROUP.mkv','prepared_path':'/media/Westworld.S03-GROUP.mkv',
             'title':'Westworld S03 1080p WEB-DL H.265 DD 5.1-GROUP',
             'subtitle':'西部世界 / Westworld', 'kind':'tv','year':'2016','episode':'S03','group':'GROUP',
             'source':'WEB-DL','category':'影剧/综艺/HD','media':{'resolution':'1080p','video_codec':'H.265','audio_codec':'DD'},
             'tmdb':{'genre_ids':[]},'douban_match':{'id':'123','title':'西部世界 第三季','year':'2020','season_number':3}}
    request=evidence(package,[{'name':'Westworld.S03-GROUP.mkv','bytes':10}])
    result={'schema_version':VERSION,'status':'ready','confidence':.96,'kind':'tv',
            'douban_id':'123','bangumi_id':None,'reasons':['正确系列和第三季'],'issues':[]}
    return package,request,result


def test_valid_candidate_is_mapped_to_canonical_url(data):
    package,request,result=data
    updated,blocks=apply_review(package,request,result,attachments=False)
    assert not blocks and updated['douban_url']=='https://movie.douban.com/subject/123/'
    assert package.get('douban_url') is None
    assert updated['media']==package['media']
    assert updated['douban_match']['id']=='123'
    assert evidence(updated,request['resource']['files'])['candidates']['douban'][0]['id']=='123'
    assert request['requirements']['bangumi_required'] is False


def test_english_animation_does_not_require_bangumi(data):
    package, _, result = data
    package['tmdb'].update(genre_ids=[16], original_language='en')
    request = evidence(package, [{'name':'Westworld.S03-GROUP.mkv','bytes':10}])
    assert request['requirements']['bangumi_required'] is False
    result['issues'] = ['Bangumi 没有匹配条目']
    updated, blockers = apply_review(package, request, result, attachments=False)
    assert updated['category'] == '动画'
    assert 'bangumi_unconfirmed' not in blockers
    assert not blockers


def test_japanese_animation_still_requires_bangumi(data):
    package, _, result = data
    package['tmdb'].update(genre_ids=[16], original_language='ja')
    request = evidence(package, [{'name':'Westworld.S03-GROUP.mkv','bytes':10}])
    assert request['requirements']['bangumi_required'] is True
    assert 'bangumi_unconfirmed' in apply_review(package, request, result, attachments=False)[1]


@pytest.mark.parametrize('change', [{'douban_id':'999'},{'command':'rm -rf anything'},
 {'confidence':float('nan')},{'confidence':True},{'kind':'game'},{'schema_version':'v999'},
 {'reasons':['Cookie: secret']},{'status':'publish_now'}])
def test_invalid_or_unsafe_model_outputs_fail_closed(data,change):
    _,request,result=data
    result.update(change)
    with pytest.raises(ValueError): validate_output(result,request)


def test_low_confidence_never_ready(data):
    package,request,result=data;result['confidence']=.70
    assert 'identity_not_high_confidence' in apply_review(package,request,result,attachments=False)[1]


def test_douban_other_season_is_blocked(data):
    package,request,result=data
    request['candidates']['douban'][0]['season_number']=2
    assert 'douban_season_conflict' in apply_review(package,request,result,attachments=False)[1]


def test_candidate_title_explicit_season_is_not_lost(data):
    package, _, _=data
    package['douban_match']['season_number']=None
    request=evidence(package, [{'name':'Westworld.S03-GROUP.mkv','bytes':10}])
    assert request['candidates']['douban'][0]['season_number']==3


def test_bangumi_wrong_explicit_season_is_blocked(data):
    package, request, result=data
    package['tmdb']['genre_ids']=[16]
    request['candidates']['bangumi']=[{'id':'456','title':'某动画 第二季','year':'2020','season_number':2}]
    result['bangumi_id']='456'
    assert 'bangumi_season_conflict' in apply_review(package,request,result,attachments=False)[1]


def test_model_inferred_group_not_present_in_files_is_blocked(data):
    package,request,result=data;package['group']='INVENTED'
    assert 'release_group_unproven' in apply_review(package,request,result,attachments=False)[1]


def test_mp3_channel_spacing_is_corrected_without_mutating_probe(data):
    package,request,result=data
    package['media'].update(audio_codec='MP3',audio_channels='2.0')
    package['title']='Westworld 2016 S03 1080p WEB-DL H.265 MP32.0-GROUP'
    updated,blocks=apply_review(package,request,result,attachments=False)
    assert not blocks and 'MP3 2.0' in updated['title']


def test_measured_mayan_language_and_encode_category_are_in_model_input(data):
    package, request, _ = data
    package.update(source='BluRay', category='电影/BluRay', kind='movie')
    package['media'].update(audio_language='myn', audio_codec='MP3', audio_channels='2.0')
    package['title']='Apocalypto 2006 REPACK BluRay 1080p x265 MP32.0-GROUP'
    package['subtitle']='启示 / Apocalypto [英语]'
    package['tmdb'].update(chinese_name='启示', original_name='Apocalypto')
    normalized=normalize_draft(package, request['resource']['files'])
    inputs=evidence(normalized, request['resource']['files'])
    assert '[玛雅语系]' in inputs['draft']['subtitle']
    assert inputs['measured_media']['audio_language_name']=='玛雅语系'
    assert inputs['packaging']['is_disc'] is False
    assert inputs['draft']['category']=='电影/HD'
    assert 'MP3 2.0' in inputs['draft']['title']
    assert package['subtitle'].endswith('[英语]')


def test_movie_disc_category_preserves_source_case(data):
    package,_,_=data;package.update(kind='movie',source='UHD BluRay')
    assert deterministic_category(package,[{'name':'movie.iso'}])=='电影/BluRay'


def test_movie_remake_year_conflict_is_blocked(data):
    package,request,result=data
    package['kind']=result['kind']='movie';package['year']='1978'
    assert 'douban_year_conflict' in apply_review(package,request,result,attachments=False)[1]


def test_mkv_anime_is_not_bluray_disc(data):
    package,request,result=data
    package['tmdb'].update(genre_ids=[16], original_language='ja');package['source']='BluRay BDRip'
    request=evidence(package,request['resource']['files'])
    assert deterministic_category(package,request['resource']['files'])=='动画'
    assert 'bangumi_unconfirmed' in apply_review(package,request,result,attachments=False)[1]


def test_anime_bdmv_is_disc(data):
    package,_,_=data;package['tmdb']['genre_ids']=[16];package['source']='BluRay'
    assert deterministic_category(package,[{'name':'BDMV/STREAM/0001.m2ts'}])=='动画/Bluray'


def test_missing_attachments_block_publish(data):
    package,request,result=data
    assert {'screenshots_missing','torrent_missing','mediainfo_missing'} <= set(apply_review(package,request,result)[1])


def test_interlaced_bdinfo_does_not_claim_progressive_resolution(data):
    package,request,result=data
    package['media']['resolution']='1080i'
    blockers=apply_review(package,request,result,attachments=False)[1]
    assert 'resolution_interlaced_not_supported' in blockers
    assert 'resolution_unmeasured' not in blockers


def test_compressed_media_is_blocked(data):
    package,request,result=data;request['resource']['files'].append({'name':'movie.part1.rar','bytes':5})
    assert 'compressed_media_disallowed' in apply_review(package,request,result,attachments=False)[1]


def test_source_symlink_rejected(tmp_path):
    p=tmp_path/'link';p.symlink_to(tmp_path/'missing')
    with pytest.raises(ValueError,match='symlink'): files_snapshot(p)


@pytest.mark.parametrize('selected,expected', [('123','metadata_ready'),('999','pending')])
def test_cli_preview_is_noninteractive_and_structured(data,tmp_path,monkeypatch,selected,expected):
    from media_title_renamer import autonomous
    package, _, result=data
    source=tmp_path/'Westworld.S03-GROUP.mkv'
    source.write_bytes(b'fixture media')
    package.update(input_path=str(source),prepared_path=str(source))
    source_before=files_snapshot(source)
    package_file=tmp_path/'mteam-prepare.json'
    package_file.write_text(json.dumps(package))
    result['douban_id']=selected
    class Assistant:
        model='mock-model'
        last_web_search_used=False
        def __init__(self,**kwargs): pass
        def _complete_json(self,system,user): return result
    monkeypatch.setattr(autonomous,'CliproxyAssistant',Assistant)
    monkeypatch.setattr('builtins.input',lambda *a: pytest.fail('must not prompt'))
    # Restore process-wide temp settings at test teardown.
    monkeypatch.setenv('TMPDIR',str(tmp_path))
    monkeypatch.setattr(autonomous.tempfile,'tempdir',str(tmp_path))
    output=tmp_path/'output'
    code=main([str(package_file),'--gpt','--preview','--metadata-only','--output',str(output)])
    report=json.loads((output/'review.json').read_text())
    assert code==(0 if expected=='metadata_ready' else 2)
    assert report['status']==expected and report['source_unchanged']
    assert files_snapshot(source)==source_before
    assert (output/'input-schema.json').is_file() and (output/'output-schema.json').is_file()
    assert not (output/'publish-result.json').exists()


def test_metadata_only_cannot_submit(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(['missing','--gpt','--submit','--metadata-only','--output',str(tmp_path)])
    assert exc.value.code==2


@pytest.mark.parametrize('failure',[False,True])
def test_submit_delegation_persists_safe_outcome(data,tmp_path,monkeypatch,failure):
    from media_title_renamer import autonomous, publish, auto_publish
    package, _, result=data
    source=tmp_path/'Westworld.S03-GROUP.mkv';source.write_bytes(b'fixture')
    package.update(input_path=str(source),prepared_path=str(source))
    package['tmdb']['name']='Westworld'
    package_file=tmp_path/'mteam-prepare.json';package_file.write_text(json.dumps(package))
    class Assistant:
        model='mock-model';last_web_search_used=False
        def __init__(self,**kwargs): pass
        def _complete_json(self,system,user): return result
    monkeypatch.setattr(autonomous,'CliproxyAssistant',Assistant)
    real_review=apply_review
    monkeypatch.setattr(autonomous,'apply_review',lambda p,r,o,**kw: real_review(p,r,o,attachments=False))
    calls=[]
    def fake_publish(args):
        calls.append(args)
        if failure: raise RuntimeError('passkey=do-not-log-this')
        path=args[args.index('--result-json')+1]
        from pathlib import Path
        Path(path).write_text(json.dumps({'status':'seeded','mteam_torrent_id':'123','qb_torrent_hash':'a'*40}))
    monkeypatch.setattr(auto_publish,'STATE_ROOT',tmp_path/'state')
    monkeypatch.setattr(auto_publish,'source_tasks',lambda *a,**kw: [])
    monkeypatch.setattr(auto_publish,'search_site',lambda *a,**kw: {'ok':True,'complete':True,'total':0,'candidates':[]})
    monkeypatch.setattr(publish,'main',fake_publish)
    monkeypatch.setenv('TMPDIR',str(tmp_path))
    monkeypatch.setattr(autonomous.tempfile,'tempdir',str(tmp_path))
    output=tmp_path/'output'
    code=main([str(package_file),'--gpt','--submit','--output',str(output)])
    raw=(output/'review.json').read_text();report=json.loads(raw)
    assert len(calls)==1 and '--yes' in calls[0]
    assert report['status']==('publish_incomplete' if failure else 'seeded')
    assert code==(2 if failure else 0)
    assert 'do-not-log-this' not in raw


@pytest.mark.parametrize('receipt,expected',[
 ({'mteam_torrent_id':'123'},'resume_recall_only'),
 ({'status':'publish_rejected','api_code':'4','submit_attempted_at':1},'cooldown'),
 ({'status':'publish_rejected','api_message':'種子已存在(123)'},'duplicate_stop'),
 ({'submit_attempted_at':1},'ambiguous_stop'),({},'no_prior_submission')])
def test_operation_policy_never_blindly_republishes(receipt,expected):
    assert operation_decision(receipt)==expected


@pytest.fixture
def ready_preview(data, tmp_path):
    import hashlib
    from media_title_renamer.prepare import _bencode
    package, _, result = data
    source = tmp_path / 'Westworld.S03-GROUP.mkv'
    source.write_bytes(b'fixture media')
    output = tmp_path / 'preview'
    output.mkdir()
    package.update(input_path=str(source), prepared_path=str(source), technical_info_text='Fixture MediaInfo')
    package['screenshots'] = []
    for index in range(4):
        image = output / f'screenshot-{index}.jpg'
        image.write_bytes(b'fixture image')
        package['screenshots'].append(str(image))
    torrent = output / 'upload.torrent'
    torrent.write_bytes(_bencode({b'info': {b'name': source.name.encode(), b'length': source.stat().st_size,
        b'private': 1, b'piece length': 16384, b'pieces': hashlib.sha1(source.read_bytes()).digest()}}))
    package['torrent'] = {'path': str(torrent)}
    rows = files_snapshot(source)
    request = evidence(package, rows)
    package, blockers = apply_review(package, request, result)
    assert not blockers
    (output / 'mteam-prepare.json').write_text(json.dumps(package))
    (output / 'ai-input.json').write_text(json.dumps(request))
    (output / 'ai-output.json').write_text(json.dumps(result))
    (output / 'review.json').write_text(json.dumps({'schema_version': VERSION, 'mode': 'preview',
        'status': 'ready', 'blockers': [], 'confidence': result['confidence'], 'model': 'fixture-model',
        'fingerprint': hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()}))
    return source, output


@pytest.mark.parametrize('direct,new_output', [(False, False), (True, False), (True, True)])
def test_submit_reuses_ready_preview_without_model_or_prepare(ready_preview, monkeypatch, direct, new_output):
    from media_title_renamer import autonomous, auto_publish
    source, output = ready_preview
    def forbidden(*args, **kwargs):
        pytest.fail('valid preview must not call model, network search, or prepare')
    monkeypatch.setattr(autonomous, 'CliproxyAssistant', forbidden)
    monkeypatch.setattr(autonomous.subprocess, 'run', forbidden)
    monkeypatch.setattr(auto_publish, 'STATE_ROOT', output / 'state')
    calls = []
    def publish_ready(package, task_output, options):
        calls.append(package)
        return {'status': 'seeded', 'mteam_torrent_id': '123', 'qb_torrent_hash': 'a' * 40}
    monkeypatch.setattr(auto_publish, 'publish_and_seed', publish_ready)
    monkeypatch.setenv('TMPDIR', str(output))
    monkeypatch.setattr(autonomous.tempfile, 'tempdir', str(output))
    input_path = output / 'mteam-prepare.json' if direct else source
    if new_output: output = output.parent / 'submission'
    assert main([str(input_path), '--gpt', '--submit', '--output', str(output)]) == 0
    assert len(calls) == 1
    report = json.loads((output / 'review.json').read_text())
    assert report['preview_reused'] is True and report['status'] == 'seeded'


@pytest.mark.parametrize('change', ['source', 'screenshot', 'torrent', 'metadata_only', 'different_source', 'package_title'])
def test_preview_cache_requires_matching_source_and_complete_attachments(ready_preview, change):
    from media_title_renamer.autonomous import reusable_preview
    source, output = ready_preview
    if change == 'source': source.write_bytes(b'changed source')
    elif change == 'screenshot': (output / 'screenshot-0.jpg').unlink()
    elif change == 'torrent': (output / 'upload.torrent').write_bytes(b'not a torrent')
    elif change == 'metadata_only':
        report = json.loads((output / 'review.json').read_text())
        report['status'] = 'metadata_ready'
        (output / 'review.json').write_text(json.dumps(report))
    elif change == 'package_title':
        package = json.loads((output / 'mteam-prepare.json').read_text())
        package['title'] = 'Wrong Work 2026 BluRay 1080p x265-GROUP'
        (output / 'mteam-prepare.json').write_text(json.dumps(package))
    else:
        other = source.with_name('Other.S03-GROUP.mkv')
        other.write_bytes(source.read_bytes())
        source = other
    assert reusable_preview(source, output / 'mteam-prepare.json', files_snapshot(source)) is None
