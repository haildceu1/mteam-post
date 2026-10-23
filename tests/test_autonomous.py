import copy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import media_title_renamer.autonomous as autonomous
from media_title_renamer.autonomous import (VERSION, validate_output, evidence,
    apply_review, operation_decision, files_snapshot, deterministic_category, normalize_draft, main,
    _resolved_policy_issues)


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


def test_verified_douban_url_defers_synopsis_fetch_to_publish_form(data):
    package, request, result = data
    result['issues'] = ['缺少必需的豆瓣简介文字，无法确认豆瓣资料要求已完整满足。']
    updated, blockers = apply_review(package, request, result, attachments=False)
    assert not blockers
    assert updated['douban_url'] == 'https://movie.douban.com/subject/123/'

    result['douban_id'] = 'not-a-candidate'
    with pytest.raises(ValueError, match='model_invented_douban_id'):
        apply_review(package, request, result, attachments=False)


def test_mixed_torrent_root_resolves_only_proven_episode_group_variation():
    package = {'group':'MIXED', 'kind':'tv', 'source':'WEB-DL',
               'media':{'resolution':'1080p'}}
    rows = [
        {'name':'Chicago.Fire.S14E01.1080p.WEB-DL.H.264-Kitsune.mkv'},
        {'name':'Chicago.Fire.S14E20.1080p.WEB-DL.H.264-FLUX.mkv'},
    ]
    issue = 'E20 文件名发布组为 FLUX，其余列出的集数为 Kitsune；这不影响作品及季身份判断。'

    assert _resolved_policy_issues(package, rows, [issue]) == []
    assert _resolved_policy_issues({**package, 'group':'Kitsune'}, rows, [issue]) == [issue]


def test_verified_douban_movie_can_resolve_missing_tmdb_only(data):
    package, _, _ = data
    package.update(kind='movie', year='2024', tmdb=None,
        douban_url='https://movie.douban.com/subject/36903421/',
        douban_match={'id':'36903421','url':'https://movie.douban.com/subject/36903421/',
                      'title':'舔舐','original_title':'Lick','year':'2024','score':100})
    result={'kind':'movie','confidence':.97,'douban_id':'36903421'}
    candidates=[{'id':'36903421','title':'舔舐','original_title':'Lick','year':'2024','site':'douban'}]
    issue='缺少 TMDB 身份信息（id、名称及年份），无法核验影片身份与年份。'
    assert _resolved_policy_issues(package, [], [issue], candidates, result)==[]
    candidates[0]['year']='2023'
    assert _resolved_policy_issues(package, [], [issue], candidates, result)==[issue]


def test_old_cached_fps_spelling_is_normalized_before_and_after_review(data):
    package, request, result = data
    package.update(
        title='Blue Lights 2023 S04 1080p WEB-DL 50Fps H.264 AAC2.0-NoGrp',
        release_name='Blue Lights 2023 S04 1080p WEB-DL 50Fps H.264 AAC2.0-NoGrp',
        kind='tv', year='2023', episode='S04', group='NoGrp', source='WEB-DL',
        media={'resolution':'1080p','video_codec':'H.264','audio_codec':'AAC',
               'audio_channels':'2.0','hfr':'50Fps'},
        tmdb={'id':218344,'name':'Blue Lights','original_name':'Blue Lights',
              'year':'2023','genre_ids':[]},
        douban_match={'id':'123','title':'Blue Lights Season 4','year':'2026','season_number':4},
    )
    rows=[{'name':'Blue.Lights.S04.1080p.WEB.H264-NoGrp.mkv','bytes':10}]
    normalized=normalize_draft(package, rows)
    assert ' HFR ' in normalized['title']
    assert '50Fps' not in normalized['title']
    assert normalized['media']['hfr']=='HFR'
    request=evidence(normalized, rows)
    updated, blockers=apply_review(normalized, request, result, attachments=False)
    assert not blockers
    assert updated['title']=='Blue Lights 2023 S04 1080p WEB-DL HFR H.264 AAC 2.0-NoGrp'
    assert updated['media']['hfr']=='HFR'


def test_normalize_draft_deduplicates_uhd_before_bluray(data):
    package, _, _ = data
    package.update(
        input_path='/media/The.Last.Boy.Scout.1991.UHD.BluRay.iso',
        title='The Last Boy Scout 1991 UHD UHD BluRay 2160p HEVC LPCM2.0-MTeam',
        release_name='The Last Boy Scout 1991 UHD UHD BluRay 2160p HEVC LPCM2.0-MTeam',
        source='UHD BluRay', kind='movie', year='1991',
        media={'resolution':'2160p','video_codec':'HEVC','audio_codec':'TrueHD Atmos',
               'audio_channels':'7.1'},
        tmdb={'genre_ids':[],'name':'The Last Boy Scout','original_name':'The Last Boy Scout'},
    )

    normalized = normalize_draft(package, [{'name':'The.Last.Boy.Scout.1991.UHD.BluRay.iso','bytes':1}])

    assert normalized['title'].startswith('The Last Boy Scout 1991 UHD BluRay ')
    assert 'UHD UHD BluRay' not in normalized['title']


def test_submit_normalizes_legacy_title_in_reused_package(tmp_path, monkeypatch):
    package_path=tmp_path/'mteam-prepare.json'
    package_path.write_text(json.dumps({
        'input_path':str(tmp_path/'source'),
        'title':'Blue Lights 2023 S04 1080p WEB-DL 50Fps H.264 AAC 2.0-NoGrp',
        'media':{'hfr':'50Fps'},
    }), encoding='utf-8')
    snapshot=[{'name':'episode.mkv','bytes':10}]
    monkeypatch.setattr(autonomous, 'files_snapshot', lambda _source: snapshot)

    def fake_publish(path, _output, _args):
        submitted=json.loads(path.read_text(encoding='utf-8'))
        assert submitted['title']=='Blue Lights 2023 S04 1080p WEB-DL HFR H.264 AAC 2.0-NoGrp'
        assert submitted['media']['hfr']=='HFR'
        return {'status':'seeded','mteam_torrent_id':'123','qb_torrent_hash':'abc'}

    monkeypatch.setattr('media_title_renamer.auto_publish.publish_and_seed', fake_publish)
    result=autonomous.submit_ready_package(
        package_path, tmp_path, SimpleNamespace(qb_export_torrent=False), {}, snapshot)
    assert result==0


def test_english_animation_requires_bangumi(data):
    package, _, result = data
    package['tmdb'].update(genre_ids=[16], original_language='en')
    request = evidence(package, [{'name':'Westworld.S03-GROUP.mkv','bytes':10}])
    assert request['requirements']['bangumi_required'] is True
    result['issues'] = ['Bangumi 没有匹配条目']
    updated, blockers = apply_review(package, request, result, attachments=False)
    assert updated['category'] == '动画'
    assert 'bangumi_unconfirmed' in blockers


def test_japanese_animation_still_requires_bangumi(data):
    package, _, result = data
    package['tmdb'].update(genre_ids=[16], original_language='ja')
    request = evidence(package, [{'name':'Westworld.S03-GROUP.mkv','bytes':10}])
    assert request['requirements']['bangumi_required'] is True
    assert 'bangumi_unconfirmed' in apply_review(package, request, result, attachments=False)[1]


def test_animation_category_requires_bangumi_without_tmdb_genre(data):
    package, _, _ = data
    package['category'] = '動畫/Bluray'
    package['tmdb'] = {'genre_ids': [], 'original_language': 'en'}
    request = evidence(package, [{'name':'Anime.S01-GROUP.mkv','bytes':10}])
    assert request['requirements']['bangumi_required'] is True


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


def test_tv_season_entry_keeps_tmdb_series_year_and_can_resolve_unique_candidate(data):
    package, request, result = data
    package.update(
        input_path='/media/The.UnBelievable.S02-iVy.mkv',
        prepared_path='/media/The.UnBelievable.S02-iVy.mkv',
        title='The UnBelievable with Dan Aykroyd 2023 S02 1080p WEB-DL x265 DDP 2.0-iVy',
        subtitle='The UnBelievable with Dan Aykroyd',
        kind='tv', year='2023', episode='S02', group='iVy', source='WEB-DL',
        tmdb={'id': 227521, 'name': 'The UnBelievable with Dan Aykroyd',
              'original_name': 'The UnBelievable with Dan Aykroyd', 'year': '2023', 'genre_ids': []},
        media={'resolution': '1080p', 'video_codec': 'x265', 'audio_codec': 'DDP',
               'audio_channels': '2.0'},
    )
    request = evidence(package, [{'name': 'The.UnBelievable.S02-iVy.mkv', 'bytes': 100}])
    request['candidates']['douban'] = [{
        'id': '37113792', 'title': '丹·艾克罗伊德讲述奇闻异事 第二季',
        'original_title': 'The UnBelievable with Dan Aykroyd Season 2',
        'year': '2024', 'season_number': 2, 'site': 'douban',
    }]
    result.update(status='pending', confidence=.96, kind='tv', douban_id=None,
                  issues=['TMDB 系列首播年为 2023，Douban 第二季条目年份为 2024；这是系列年份与季年份的区别，不影响身份匹配。'])

    updated, blockers = apply_review(package, request, result, attachments=False)

    assert not blockers
    assert updated['title'].startswith('The UnBelievable with Dan Aykroyd 2023 S02 ')
    assert updated['year'] == '2023'
    assert updated['douban_url'] == 'https://movie.douban.com/subject/37113792/'
    assert updated['douban_match']['year'] == '2024'
    assert updated['identification_evidence']['douban_selection']['method'] == 'unique_exact_series_title_and_season'


def test_tv_season_fallback_refuses_ambiguous_or_wrong_season_candidates(data):
    package, request, result = data
    package.update(kind='tv', episode='S02', tmdb={'name': 'The Example', 'original_name': 'The Example'})
    request = evidence(package, [{'name': 'The.Example.S02-GROUP.mkv', 'bytes': 10}])
    request['candidates']['douban'] = [
        {'id': '101', 'title': 'The Example Season 2', 'original_title': 'The Example Season 2', 'year': '2024', 'season_number': 2},
        {'id': '102', 'title': 'The Example Season 2', 'original_title': 'The Example Season 2', 'year': '2024', 'season_number': 2},
    ]
    result.update(kind='tv', status='pending', douban_id=None, issues=['豆瓣季年份与系列首播年份不一致'])
    assert 'douban_unconfirmed' in apply_review(package, request, result, attachments=False)[1]


def test_user_confirmed_first_season_entry_can_represent_verified_collection(data):
    import hashlib
    package, _, result = data
    package.update(
        input_path='/media/Crossing.Lines.S01-S03-d3g',
        prepared_path='/media/Crossing.Lines.S01-S03-d3g',
        title='Crossing Lines 2013 S01-S03 1080p WEB-DL H.265 DDP 5.1-d3g',
        subtitle='纵横案线 第一季 / Crossing Lines [英语]',
        kind='tv', year='2013', episode='S01-S03', group='d3g', source='WEB-DL',
        media={'resolution':'1080p','video_codec':'H.265','audio_codec':'DDP',
               'audio_channels':'5.1','audio_language':'en'},
        tmdb={'name':'Crossing Lines','original_name':'Crossing Lines',
              'chinese_name':'纵横案线','year':'2013','genre_ids':[]},
        files=[{'episode':f'S{season:02d}E01'} for season in (1, 2, 3)],
        user_confirmed_facts=[{'field':'douban_collection_policy',
                               'value':'first_season_representative',
                               'source':'user_instruction'}],
    )
    package['douban_match'] = None
    package['douban_url'] = None
    rows=[{'name':f'S{season:02d}/Crossing.Lines.S{season:02d}E01-d3g.mkv',
           'bytes':100+season,'device':7,'inode':1000+season,'mtime_ns':2000+season}
          for season in (1, 2, 3)]
    request=evidence(package, rows)
    request['candidates']['douban']=[{
        'id':'24700586','title':'纵横案线 第一季',
        'original_title':'Crossing Lines Season 1','year':'2013',
        'season_number':1,'site':'douban'}]
    result.update(
        status='pending',confidence=.96,kind='tv',douban_id=None,
        issues=[
            '唯一豆瓣候选是《纵横案线 第一季》，只匹配第 1 季，不能作为 S01–S03 完整合集的精确条目。',
            '唯一匹配候选是《纵横案线 第一季》（season_number=1），与资源覆盖完整三季不一致；不能将其作为整部完整系列的确定匹配。',
            'S03E10–S03E12 文件名标示 H.264，而其余文件标示 H.265；与统一的实测 HEVC/H.265 参数及草稿统一标注 H.265 存在不一致。',
        ])

    # The season-1 page alone is not enough: full file-by-file probe evidence
    # must be complete, match the exact source snapshot, and agree on codecs.
    without_audit, blockers=apply_review(package, request, result, attachments=False)
    assert 'douban_unconfirmed' not in blockers
    assert any('文件名标示 H.264' in str(item) for item in blockers)
    assert without_audit['douban_url']=='https://movie.douban.com/subject/24700586/'

    package['file_media_audit']={
        'method':'ffprobe','complete':True,
        'snapshot_sha256':hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest(),
        'files':[{'name':row['name'],'bytes':row['bytes'],'probe_status':'ok',
                  'video_codec':'hevc','audio_codec':'eac3','resolution':'1080p'}
                 for row in rows],
    }
    updated, blockers=apply_review(package, request, result, attachments=False)
    assert not blockers
    assert updated['douban_url']=='https://movie.douban.com/subject/24700586/'
    assert updated['subtitle']=='纵横案线 / Crossing Lines [英语]'
    assert updated['identification_evidence']['douban_selection']['method'] == \
        'representative_first_season_for_user_confirmed_collection'
    assert updated['identification_evidence']['douban_selection']['collection_range']=='S01-S03'

    # The model may itself choose the verified first-season candidate.  The
    # representative policy still needs to remove "第一季" from a collection subtitle.
    result['douban_id']='24700586'
    model_selected, blockers=apply_review(package, request, result, attachments=False)
    assert not blockers
    assert model_selected['subtitle']=='纵横案线 / Crossing Lines [英语]'
    assert model_selected['douban_match']['selection_method'] == \
        'representative_first_season_for_user_confirmed_collection'


def test_first_season_collection_fallback_is_opt_in_and_requires_matching_show_year_and_coverage(data):
    package, _, result=data
    package.update(kind='tv',episode='S01-S03',year='2013',group='d3g',source='WEB-DL',
        title='Crossing Lines 2013 S01-S03 1080p WEB-DL H.265 DDP 5.1-d3g',
        subtitle='Crossing Lines',
        media={'resolution':'1080p','video_codec':'H.265','audio_codec':'DDP'},
        tmdb={'name':'Crossing Lines','original_name':'Crossing Lines','year':'2013'},
        files=[{'episode':f'S{season:02d}E01'} for season in (1,2,3)])
    rows=[{'name':f'Crossing.Lines.S{season:02d}E01-d3g.mkv','bytes':season}
          for season in (1,2,3)]
    request=evidence(package,rows)
    request['candidates']['douban']=[{'id':'1','title':'Crossing Lines Season 1',
        'original_title':'Crossing Lines Season 1','year':'2013','season_number':1}]
    result.update(status='pending',confidence=.96,kind='tv',douban_id=None,issues=[])

    assert 'douban_unconfirmed' in apply_review(package,request,result,attachments=False)[1]
    package['user_confirmed_facts']=[{'field':'douban_collection_policy',
        'value':'first_season_representative','source':'user_instruction'}]
    package['files']=[{'episode':'S01E01'}]
    assert 'douban_unconfirmed' in apply_review(package,request,result,attachments=False)[1]
    package['files']=[{'episode':f'S{season:02d}E01'} for season in (1,2,3)]
    request['candidates']['douban'][0]['year']='2014'
    assert 'douban_unconfirmed' in apply_review(package,request,result,attachments=False)[1]
    request['candidates']['douban'][0]['year']='2013'
    request['candidates']['douban'][0]['title']='不同作品 第一季'
    request['candidates']['douban'][0]['original_title']='A Different Series Season 1'
    assert 'douban_unconfirmed' in apply_review(package,request,result,attachments=False)[1]

    request['candidates']['douban'] = [
        {'id': '103', 'title': 'The Example Season 3', 'original_title': 'The Example Season 3', 'year': '2025', 'season_number': 3},
    ]
    assert 'douban_unconfirmed' in apply_review(package, request, result, attachments=False)[1]


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


def test_release_group_in_actual_iso_parent_directory_is_proven(data, monkeypatch):
    package, _, result = data
    source = Path('/media/The.Twilight.Saga.New.Moon.2009.RERIP.COMPLETE.UHD.BLURAY-B0MBARDiERS/TWILIGHT_NEW_MOON_UHD.iso')
    package.update(input_path=str(source), prepared_path=str(source), group='B0MBARDiERS')
    original_is_file = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda path: path == source or original_is_file(path))
    rows = [{'name': source.name, 'bytes': 95_403_376_640}]

    request = evidence(package, rows)
    updated, blockers = apply_review(package, request, result, attachments=False)

    assert request['resource']['source_context_names'] == [source.parent.name]
    assert 'release_group_unproven' not in blockers
    assert updated['group'] == 'B0MBARDiERS'


def test_parenthesized_release_group_is_proven_by_torrent_name(data):
    package, _, result=data
    package.update(group='Tigole',source='BluRay BDRip',
                   title='Westworld 2016 S03 1080p BluRay x265 AAC5.1-Tigole')
    rows=[{'name':'Westworld.S03.(1080p.BluRay.x265.HEVC.AAC5.1.Tigole)','bytes':10}]
    request=evidence(package,rows)
    updated, blockers=apply_review(package,request,result,attachments=False)
    assert not blockers
    assert updated['group']=='Tigole'


def test_dvdrip_filename_does_not_override_webdl_fallback_policy(data):
    package, _, result=data
    package.update(kind='tv',episode='S03',source='WEB-DL',year='1998',
                   title='Seven Days 1998 S03 480p WEB-DL x265 DDP2.0-iVy')
    rows=[{'name':'7.Days.S03E01.480p.DVDRip.x265-iVy.mkv','bytes':10}]
    request=evidence(package,rows)
    issue='草稿标题将来源写为WEB-DL，与文件名中的DVDRip标记不一致；应按识别出的来源归类为DVDRip。'
    result.update(kind='tv',status='pending',confidence=.96,douban_id='123',issues=[issue])
    updated,blockers=apply_review(package,request,result,attachments=False)
    assert not blockers
    assert 'WEB-DL' in updated['title']


def test_tv_title_year_uses_tmdb_series_year_not_douban_season_year(data):
    package, _, result=data
    package.update(kind='tv',episode='S04',source='WEB-DL',year='2023',
                   title='Blue Lights 2023 S04 1080p WEB-DL H.264 AAC2.0-NoGrp',
                   group='NOGRP',douban_match={'id':'456','title':'Blue Lights Season 4',
                       'original_title':'Blue Lights','year':'2026','season_number':4},
                   tmdb={'id':218344,'name':'Blue Lights','original_name':'Blue Lights','year':'2023','genre_ids':[]},
                   media={'resolution':'1080p','video_codec':'H.264','audio_codec':'AAC'})
    rows=[{'name':'Blue.Lights.S04.1080p.WEB.H264-NoGrp.mkv','bytes':10}]
    request=evidence(package,rows)
    result.update(status='pending',confidence=.99,kind='tv',douban_id='456',bangumi_id=None,
                  issues=[
                      'identity.year填写2023，是系列首播年；第四季播出年份为2026。',
                      '当前资料同时出现第四季身份和2023年份，存在年份冲突。',
                      '豆瓣候选对应第四季，候选年份为2026；草稿标题中的2023不应作为第四季年份。',
                  ])
    updated,blockers=apply_review(package,request,result,attachments=False)
    assert not blockers
    assert updated['title'].startswith('Blue Lights 2023 S04 ')


def test_chinese_ninth_season_year_difference_is_resolved_by_exact_candidate(data):
    package, _, result = data
    package.update(
        input_path='/media/Kitchen.Nightmares.US.S09-MeGusta',
        prepared_path='/media/Kitchen.Nightmares.US.S09-MeGusta',
        title='Kitchen Nightmares 2007 S09 1080p WEB-DL AV1 DDP 5.1-MeGusta',
        subtitle='厨房噩梦 / Kitchen Nightmares',
        kind='tv', year='2007', episode='S09', group='MeGusta', source='WEB-DL',
        tmdb={'id': 3207, 'name': 'Kitchen Nightmares',
              'original_name': 'Kitchen Nightmares', 'year': '2007', 'genre_ids': []},
        douban_match={'id': '38535024', 'title': '厨房噩梦(美版) 第九季',
                      'original_title': 'Kitchen Nightmares Season 9',
                      'year': '2025', 'season_number': 9},
        media={'resolution': '1080p', 'video_codec': 'AV1', 'audio_codec': 'DDP',
               'audio_channels': '5.1'},
    )
    request = evidence(package, [{'name': 'Kitchen.Nightmares.US.S09E01-MeGusta.mkv', 'bytes': 100}])
    request['candidates']['douban'] = [{
        'id': '38535024', 'title': '厨房噩梦(美版) 第九季',
        'original_title': 'Kitchen Nightmares Season 9', 'year': '2025',
        'season_number': 9, 'site': 'douban',
    }]
    result.update(
        status='pending', confidence=.96, kind='tv', douban_id='38535024',
        issues=['该季豆瓣候选年份为 2025，而当前日期为 2026 年 10 月 1 日，属已播出的季；年份差异按季年份与系列年份区分，不阻止匹配。'],
    )

    updated, blockers = apply_review(package, request, result, attachments=False)

    assert not blockers
    assert updated['title'].startswith('Kitchen Nightmares 2007 S09 ')
    assert updated['year'] == '2007'
    assert updated['douban_url'] == 'https://movie.douban.com/subject/38535024/'
    assert updated['douban_match']['year'] == '2025'


def test_mp3_channel_spacing_is_corrected_without_mutating_probe(data):
    package,request,result=data
    package['media'].update(audio_codec='MP3',audio_channels='2.0')
    package['title']='Westworld 2016 S03 1080p WEB-DL H.265 MP32.0-GROUP'
    updated,blocks=apply_review(package,request,result,attachments=False)
    assert not blocks and 'MP3 2.0' in updated['title']


def test_verified_selected_douban_candidate_resolves_draft_url_empty_warning(data):
    package, request, result = data
    package.update(input_path='/media/Beast.2026.1080p.BluRay.x264-PTNK.mkv', kind='movie',
                   episode='', year='2026', group='PTNK',
                   title='Beast 2026 2026 WEB-DL 1080p x264 DDP 5.1-PTNK',
                   source='WEB-DL', douban_url='', douban_match={})
    package['tmdb'] = {}
    package['media'].update(resolution='1080p', video_codec='x264', audio_codec='DDP')
    request = evidence(package, [{'name':'Beast.2026.1080p.BluRay.x264-PTNK.mkv','bytes':100}])
    request['candidates']['douban'] = [{
        'id':'33428144','title':'怒兽出笼','original_title':'Beast','year':'2026',
        'season_number':None,'site':'douban',
    }]
    result.update(status='pending', confidence=.96, kind='movie', douban_id='33428144',
                  issues=['douban_required=true，但草稿中的 douban_url 为空；虽有高匹配度候选，仍需补齐豆瓣条目信息后才能判为 ready。'])

    normalized = normalize_draft(package, request['resource']['files'])
    updated, blockers = apply_review(normalized, request, result, attachments=False)

    assert not blockers
    assert updated['douban_url'] == 'https://movie.douban.com/subject/33428144/'
    assert updated['source'] == 'BluRay BDRip'
    assert updated['title'] == 'Beast 2026 BluRay 1080p x264 DDP 5.1-PTNK'


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


def test_normalize_english_language_metadata_and_subtitle(data):
    package, _, _ = data
    package.update(kind='movie', title='Westworld 2016 BluRay 1080p x264 AAC 2.0-GROUP',
                   subtitle='西部世界 / Westworld',
                   douban_match={'id':'123','title':'西部世界','original_title':'Westworld',
                                 'year':'2016'},
                   media={'resolution':'1080p','video_codec':'x264','audio_codec':'AAC',
                          'audio_channels':'2.0','audio_language':'English'})
    normalized=normalize_draft(package,[{'name':'Westworld.mkv','bytes':10}])
    assert normalized['media']['audio_language']=='en'
    assert normalized['source_language']=='英语'
    assert normalized['subtitle'].endswith('[英语]')
    request=evidence(normalized,[{'name':'Westworld.mkv','bytes':10}])
    assert request['measured_media']['audio_language']=='en'
    assert request['measured_media']['audio_language_name']=='英语'
    package['imdb_url']='https://www.imdb.com/title/tt0000000/'
    assert 'imdb_url' not in evidence(package,[{'name':'Westworld.mkv','bytes':10}])['draft']


def test_movie_disc_category_preserves_source_case(data):
    package,_,_=data;package.update(kind='movie',source='UHD BluRay')
    assert deterministic_category(package,[{'name':'movie.iso'}])=='电影/BluRay'


def test_sports_event_uses_mteam_sports_category_and_does_not_require_douban(data):
    package, _, result = data
    package.update(
        input_path='/media/NASCAR.Americana.S01.USA.WEB-DL.1080p.H264-egortech',
        title='NASCAR Americana 2026 S01 1080p WEB-DL H.264 AC3 2.0-egortech',
        subtitle='NASCAR Americana', kind='tv', episode='S01', group='egortech',
        category='影剧/综艺/HD', source='WEB-DL',
        media={'resolution':'1080p','video_codec':'H.264','audio_codec':'AC3','audio_channels':'2.0'},
        tmdb={'genre_ids':[]}, douban_match={},
    )
    rows = [{'name':'NASCAR.Americana.S01.USA.WEB-DL.1080p.H264-egortech.mkv','bytes':100}]
    request = evidence(package, rows)
    assert request['requirements']['douban_required'] is False
    assert request['requirements']['sports_description_images_only'] is True
    assert deterministic_category(package, rows) == '運動'
    request['candidates']['douban'] = []
    result.update(status='pending', confidence=.96, kind='tv', douban_id=None,
                  issues=['douban_required=true，但没有找到可用豆瓣候选。'])

    updated, blockers = apply_review(package, request, result, attachments=False)

    assert not blockers
    assert updated['category'] == '運動'


def test_sports_series_douban_entry_without_season_number_is_allowed_when_exact(data):
    package, _, result = data
    package.update(
        input_path='/media/NASCAR.Americana.S01.USA.WEB-DL.1080p.AC3.2.0.H.264-egortech',
        title='NASCAR Americana 2026 S01 1080p USA WEB-DL HFR H.264 DD 5.1-egortech',
        subtitle='NASCAR Americana', kind='tv', year='2026', episode='S01',
        group='egortech', source='WEB-DL', category='運動',
        media={'resolution':'1080p','video_codec':'H.264','audio_codec':'DD','audio_channels':'5.1'},
        tmdb={'id':336861,'name':'NASCAR Americana','original_name':'NASCAR Americana',
              'chinese_name':'NASCAR Americana','year':'2026','genre_ids':[]},
    )
    rows = [{'name':'NASCAR.Americana.S01E01.mkv','bytes':100}]
    request = evidence(package, rows)
    request['candidates']['douban'] = [{
        'id':'38646882','title':'NASCAR Americana','original_title':'NASCAR Americana',
        'year':'2026','season_number':None,'site':'douban',
    }]
    result.update(status='pending', confidence=.98, kind='tv', douban_id='38646882',
                  issues=['候选条目没有季号，无法确认它是与第 1 季对应的精确季条目，因此暂不判 ready。'])

    updated, blockers = apply_review(package, request, result, attachments=False)

    assert not blockers
    assert updated['category'] == '運動'
    assert updated['douban_url'] == 'https://movie.douban.com/subject/38646882/'


def test_sports_series_does_not_accept_wrong_year_series_candidate(data):
    package, _, result = data
    package.update(kind='tv', year='2026', episode='S01', category='運動', group='GROUP',
                   tmdb={'name':'NASCAR Americana','original_name':'NASCAR Americana','year':'2026','genre_ids':[]})
    rows=[{'name':'NASCAR.Americana.S01-GROUP.mkv','bytes':10}]
    request=evidence(package, rows)
    request['candidates']['douban']=[{'id':'12','title':'NASCAR Americana','original_title':'NASCAR Americana',
                                      'year':'1999','season_number':None,'site':'douban'}]
    result.update(kind='tv',status='pending',confidence=.98,douban_id='12',
                  issues=['候选条目没有季号，无法确认它是与第 1 季对应的精确季条目，因此暂不判 ready。'])

    assert 'identity_not_high_confidence' in apply_review(package, request, result, attachments=False)[1]


def test_exact_imdb_backfill_allows_series_level_douban_entry_without_season(data):
    package, _, result = data
    package.update(
        input_path='/media/The.Kids.of.Degrassi.Street.S01-iCX',
        title='The Kids of Degrassi Street 1979 S01 480p WEB-DL H.264 DDP 2.0-iCX',
        subtitle='无德格拉西街的孩子们 / The Kids of Degrassi Street [英语]',
        kind='tv', year='1979', episode='S01', group='iCX',
        imdb_url='https://www.imdb.com/title/tt0083435/',
        tmdb={'id': 995, 'name': 'The Kids of Degrassi Street',
              'original_name': 'The Kids of Degrassi Street', 'year': '1979', 'genre_ids': []},
        douban_url='https://movie.douban.com/subject/2150460/',
        douban_match={'id': '2150460', 'url': 'https://movie.douban.com/subject/2150460/',
                      'title': '无德格拉西街的孩子们',
                      'original_title': 'The Kids of Degrassi Street', 'year': '1984'},
        douban_selection={'id': '2150460', 'url': 'https://movie.douban.com/subject/2150460/',
                          'matched_by': 'exact_imdb_id_type_override', 'confidence': .99,
                          'tmdb_id': '995', 'imdb_id': 'tt0083435'},
        identification_evidence={'identity': {'search_title': 'The Kids of Degrassi Street'}},
        media={'resolution': '480p', 'video_codec': 'H.264', 'audio_codec': 'DDP'},
    )
    rows = [{'name': 'The.Kids.of.Degrassi.Street.S01E01.iCX.mkv', 'bytes': 100}]
    request = evidence(package, rows)
    request['candidates']['douban'] = [{
        'id': '2150460', 'title': '无德格拉西街的孩子们',
        'original_title': 'The Kids of Degrassi Street', 'year': '1984',
        'season_number': None, 'site': 'douban',
    }]
    result.update(status='pending', confidence=.99, kind='tv', douban_id=None,
                  issues=['缺少可确认对应第一季的豆瓣候选条目。'])

    updated, blockers = apply_review(package, request, result, attachments=False)

    assert not blockers
    assert updated['douban_url'] == 'https://movie.douban.com/subject/2150460/'
    assert updated['identification_evidence']['douban_selection']['method'] == \
        'persisted_exact_imdb_series_page'


@pytest.mark.parametrize('mismatch', ['imdb', 'imdb_url', 'title', 'tmdb'])
def test_series_level_douban_backfill_rejects_mismatched_identity(data, mismatch):
    package, _, result = data
    package.update(
        kind='tv', episode='S01', year='1979',
        imdb_url='https://www.imdb.com/title/tt0083435/',
        tmdb={'id': 995, 'name': 'The Kids of Degrassi Street',
              'original_name': 'The Kids of Degrassi Street', 'year': '1979', 'genre_ids': []},
        douban_url='https://movie.douban.com/subject/2150460/',
        douban_match={'id': '2150460', 'url': 'https://movie.douban.com/subject/2150460/',
                      'title': '无德格拉西街的孩子们',
                      'original_title': 'The Kids of Degrassi Street', 'year': '1984'},
        douban_selection={'id': '2150460', 'url': 'https://movie.douban.com/subject/2150460/',
                          'matched_by': 'exact_imdb_id_type_override', 'confidence': .99,
                          'tmdb_id': '995', 'imdb_id': 'tt0083435'},
        identification_evidence={'identity': {'search_title': 'The Kids of Degrassi Street'}},
        media={'resolution': '480p', 'video_codec': 'H.264', 'audio_codec': 'DDP'},
    )
    if mismatch == 'imdb':
        package['douban_selection']['imdb_id'] = 'tt0000000'
    elif mismatch == 'imdb_url':
        package['imdb_url'] = 'https://attacker.invalid/title/tt0083435/'
    elif mismatch == 'tmdb':
        package['douban_selection']['tmdb_id'] = '996'
    rows = [{'name': 'The.Kids.of.Degrassi.Street.S01E01.iCX.mkv', 'bytes': 100}]
    request = evidence(package, rows)
    candidate_row = {'id': '2150460', 'title': '无德格拉西街的孩子们',
                     'original_title': 'The Kids of Degrassi Street', 'year': '1984',
                     'season_number': None, 'site': 'douban'}
    if mismatch == 'title':
        candidate_row['original_title'] = 'Different Show'
    request['candidates']['douban'] = [candidate_row]
    result.update(status='pending', confidence=.99, kind='tv', douban_id=None,
                  issues=['缺少可确认对应第一季的豆瓣候选条目。'])

    _, blockers = apply_review(package, request, result, attachments=False)

    assert 'douban_unconfirmed' in blockers


def test_high_confidence_pending_with_exact_candidate_and_no_issues_can_be_ready(data):
    package, request, result = data
    result.update(status='pending', confidence=.97, issues=[])

    updated, blockers = apply_review(package, request, result, attachments=False)

    assert not blockers
    assert updated['douban_url'] == 'https://movie.douban.com/subject/123/'


@pytest.mark.parametrize('kind,source,issue,expected', [
    ('movie', 'BluRay REMUX', 'packaging.is_disc=false，不能将该文件判定为原盘；分类应依据实际封装方式，而非文件名中的 Remux。', '电影/Remux'),
    ('tv', 'BluRay BDRip', '来源标注 BluRay，但文件仅为单个 MKV、没有原盘结构；不应判为蓝光原盘。', '影剧/综艺/HD'),
])
def test_model_warnings_resolved_by_source_policy_are_nonblocking(data,kind,source,issue,expected):
    package,_,result=data
    package.update(kind=kind,source=source,category=expected,year='2020',episode=None)
    package['title']=('Westworld 2020 S03 1080p BluRay H.265 DD 5.1-GROUP'
                      if source.endswith('BDRip') else 'Westworld 2020 S03 1080p BluRay REMUX H.265 DD 5.1-GROUP')
    rows=[{'name':'Westworld.S03.BluRay.x265-GROUP.mkv','bytes':10}]
    request=evidence(package,rows)
    result.update(kind=kind,issues=[issue])
    updated,blockers=apply_review(package,request,result,attachments=False)
    assert not blockers
    assert updated['category']==expected


def test_bdrip_policy_keeps_bluray_in_title_without_claiming_disc(data):
    package, _, result = data
    package.update(kind='movie', source='UHD BluRay BDRip', year='2020', episode=None,
                   title='Example Movie 2020 BluRay 1080p x265 DD-GROUP')
    rows = [{'name': 'Example.Movie.2020.UHD.BluRay.1080p.x265-GROUP.mkv', 'bytes': 10}]
    request = evidence(package, rows)
    result.update(kind='movie', issues=[
        '资源标注 UHD BluRay，但实测封装为单文件 MKV，且未提供原盘证据；不应据此认定为蓝光原盘。'
    ])

    updated, blockers = apply_review(package, request, result, attachments=False)

    assert not blockers
    assert updated['source'] == 'UHD BluRay BDRip'
    assert updated['title'].startswith('Example Movie 2020 BluRay ')


def test_bdrip_is_not_rejected_by_legacy_webdl_source_rule(data):
    package, _, result = data
    package.update(kind='tv', episode='S01', source='BluRay BDRip',
                   title='Robin Hood 2006 S01 1080p BluRay x265 AAC5.1-NOGRP',
                   group='NOGRP', media={'resolution':'1080p','video_codec':'x265','audio_codec':'AAC'})
    package['douban_match']={'id':'456','title':'侠盗罗宾汉 第一季',
                             'original_title':'Robin Hood Season 1','year':'2006','season_number':1}
    rows=[{'name':'Robin.Hood.2006.S01.1080p.BluRay.x265.mkv','bytes':10}]
    request=evidence(package, rows)
    result.update(status='pending',confidence=.98,kind='tv',douban_id='456',bangumi_id=None)
    legacy_issue=(
        '媒体为非光盘MKV（packaging.is_disc=false），不应按蓝光原盘处理；'
        '项目规则下非REMUX、非BDMV/蓝光ISO来源统一为WEB-DL。'
        'identity.source及draft.title标注BluRay BDRip/BluRay，与该来源归一规则不一致。'
    )
    result['issues']=[legacy_issue]
    updated, blockers=apply_review(package, request, result, attachments=False)
    assert not blockers
    assert updated['title'].startswith('Robin Hood 2006 S01 1080p BluRay x265')


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
    assert deterministic_category(package,[
        {'name':'BDMV/index.bdmv'}, {'name':'BDMV/STREAM/0001.m2ts'}
    ])=='动画/Bluray'


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


def test_qb_export_preview_skips_private_torrent_but_keeps_screenshots(tmp_path, monkeypatch):
    source = tmp_path / 'disc'
    source.mkdir()
    (source / 'feature.mkv').write_bytes(b'fixture')
    output = tmp_path / 'preview'
    calls = []

    def stop_after_prepare_command(command, **kwargs):
        calls.append(command)
        raise RuntimeError('test_stop')

    monkeypatch.setattr(autonomous.subprocess, 'run', stop_after_prepare_command)
    assert main([str(source), '--gpt', '--preview', '--qb-export-torrent',
                 '--kind', 'movie', '--output', str(output)]) == 2
    assert len(calls) == 1
    assert '--skip-torrent' in calls[0]
    assert '--skip-screenshots' not in calls[0]
    report = json.loads((output / 'review.json').read_text())
    assert report['source_unchanged'] is True


def test_qb_export_preview_does_not_require_unbuilt_torrent(data, tmp_path):
    package, request, result = data
    screenshot = tmp_path / 'frame.jpg'
    screenshot.write_bytes(b'image fixture')
    package['screenshots'] = [str(screenshot)] * 4
    package['technical_info_text'] = 'validated media info'
    package['torrent'] = {'path': str(tmp_path / 'not-built.torrent')}

    _, preview_blockers = apply_review(
        package, request, result, attachments=True, require_torrent=False)
    _, strict_blockers = apply_review(package, request, result, attachments=True)

    assert 'torrent_missing' not in preview_blockers
    assert 'torrent_missing' in strict_blockers


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
 ({'status':'submission_not_dispatched','submit_attempted_at':1,'retry_authorized':True,
   'click_intercepted':True,'create_request_started':False},'retry_pre_dispatch'),
 ({'status':'submission_not_dispatched','submit_attempted_at':1,'click_intercepted':True,
   'create_request_started':False},'ambiguous_stop'),
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


def test_submit_can_replace_preview_torrent_with_sanitized_qb_export(ready_preview, monkeypatch):
    from media_title_renamer import autonomous, auto_publish, qb_seed
    from media_title_renamer.recall_official import validate_manifest
    source, output = ready_preview
    original_package = json.loads((output / 'mteam-prepare.json').read_text())
    original_torrent = Path(original_package['torrent']['path'])
    exported_path = output / 'qb-upload.torrent'
    exported_info = validate_manifest(original_torrent.read_bytes(), source)
    calls = []

    def fake_export(given_source, destination, **kwargs):
        assert given_source == source
        destination.write_bytes(original_torrent.read_bytes())
        calls.append((given_source, destination, kwargs))
        return {'path': str(destination), 'info_hash': exported_info['info_hash'],
                'size_bytes': exported_info['size_bytes'], 'file_count': 1,
                'manifest_verified': True, 'trackers_stripped': True}

    monkeypatch.setattr(qb_seed, 'export_qb_torrent', fake_export)
    monkeypatch.setattr(autonomous, 'CliproxyAssistant', lambda **_: pytest.fail('preview should be reused'))
    monkeypatch.setattr(autonomous.subprocess, 'run', lambda *a, **k: pytest.fail('prepare should be skipped'))
    monkeypatch.setattr(auto_publish, 'STATE_ROOT', output / 'state')
    monkeypatch.setattr(auto_publish, 'publish_and_seed',
                        lambda *a, **k: {'status': 'seeded', 'mteam_torrent_id': '123', 'qb_torrent_hash': 'a'*40})
    monkeypatch.setenv('TMPDIR', str(output))
    monkeypatch.setattr(autonomous.tempfile, 'tempdir', str(output))
    assert main([str(source), '--gpt', '--submit', '--qb-export-torrent', '--output', str(output)]) == 0
    saved = json.loads((output / 'mteam-prepare.json').read_text())
    assert len(calls) == 1
    assert saved['torrent']['path'] == str(exported_path)
    assert saved['torrent']['source'] == 'qbittorrent_export'
    assert saved['torrent']['info_hash'] == exported_info['info_hash']


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


def test_complete_duplicate_preview_can_be_reused_for_official_recall(ready_preview):
    from media_title_renamer.autonomous import reusable_preview
    source,output=ready_preview
    report=json.loads((output/'review.json').read_text())
    report['status']='skipped_duplicate'
    report['blockers']=['mteam_duplicate']
    (output/'review.json').write_text(json.dumps(report))
    assert reusable_preview(source,output/'mteam-prepare.json',files_snapshot(source)) is not None
    report['blockers']=['mteam_duplicate','search_incomplete']
    (output/'review.json').write_text(json.dumps(report))
    assert reusable_preview(source,output/'mteam-prepare.json',files_snapshot(source)) is None
    report['status']='publish_incomplete'
    report['blockers']=['existing_torrent_content_mismatch']
    (output/'review.json').write_text(json.dumps(report))
    assert reusable_preview(source,output/'mteam-prepare.json',files_snapshot(source)) is not None
