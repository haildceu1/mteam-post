from types import SimpleNamespace
from media_title_renamer import web_candidates as wc

def test_rate_limit_is_not_classified_as_missing_title():
    package={'identification_evidence':{'douban_diagnostics':[
        'HTTP 200 返回空结果（疑似豆瓣频控/风控，非 HTTP 403/429）',
        '豆瓣普通搜索页返回：搜索访问太频繁。']}}
    before=list(package['identification_evidence']['douban_diagnostics'])
    assert wc.douban_failure_class(package,[])=='douban_search_rate_limited'
    assert package['identification_evidence']['douban_diagnostics']==before
    assert wc.douban_failure_class({'identification_evidence':{}},[])=='douban_no_verified_candidates'
    assert wc.douban_failure_class({'identification_evidence':{}},
                                   [{'site':'douban_detail','status':'unverifiable'}])=='douban_no_verified_candidates'
    assert wc.douban_failure_class({'identification_evidence':{}},
                                   [{'site':'douban_detail','status':'rate_limited'}])=='douban_search_rate_limited'
    assert wc.douban_failure_class({'identification_evidence':{}},
                                   [{'site':'douban_authenticated_search','rate_limited':True}])=='douban_search_rate_limited'
    assert wc.douban_failure_class({'identification_evidence':{}},
                                   [{'site':'douban_detail','status':'verification_deferred_rate_limited'}])=='douban_search_rate_limited'
    old={'identification_evidence':{'douban_diagnostics':['豆瓣普通搜索页返回：搜索访问太频繁。']}}
    assert wc.douban_failure_class(old,[{'site':'douban_authenticated_search','rate_limited':False,
                                         'candidate_count':0}])=='douban_no_verified_candidates'

def test_web_search_uses_high_confidence_identity_when_tmdb_missing():
    package={'tmdb':None,'kind':'movie','year':'2026',
             'identification_evidence':{'identity':{'kind':'movie','confidence':.98,
                 'search_title':'Billie Eilish Hit Me Hard and Soft The Tour'}}}
    assert wc.search_names(package)==['Billie Eilish Hit Me Hard and Soft The Tour']
    assert wc.canonical_douban_url('https://movie.douban.com/subject/37447627/?from=search')=='https://movie.douban.com/subject/37447627/'
    assert wc.canonical_douban_url('https://evil.example/subject/37447627/') is None
    assert wc.canonical_douban_url('http://movie.douban.com/subject/37447627/') is None
    assert wc.canonical_douban_url('https://movie.douban.com.evil.example/subject/37447627/') is None
    package['identification_evidence']['identity']['confidence']=.3
    assert wc.search_names(package)==[]

def test_cookie_search_candidates_are_real_and_durable(monkeypatch):
    from media_title_renamer import prepare
    row=prepare.DoubanMatch(id='4075084',url='https://movie.douban.com/subject/4075084/',title='白宫风云 第四季',original_title='The West Wing',year='2002',score=100,season_number=4,source='suggest')
    monkeypatch.setattr(prepare,'_douban_request_headers',lambda:{'Cookie':'test-cookie-never-save'})
    monkeypatch.setattr(prepare,'_douban_candidates',lambda *a,**kw:[row])
    diagnostics=[]
    result=wc.verified_douban_candidates({'tmdb':{'name':'The West Wing','chinese_name':'白宫风云'},'episode':'S04','year':'1999'},diagnostics)
    assert result[0]['id']=='4075084' and result[0]['season_number']==4
    assert diagnostics[0]['cookie_used']
    assert 'test-cookie-never-save' not in str(result)+str(diagnostics)


def test_historical_rate_limit_does_not_skip_fresh_authenticated_search(monkeypatch):
    from media_title_renamer import prepare
    row=prepare.DoubanMatch(id='3011235',url='https://movie.douban.com/subject/3011235/',
        title='哈利·波特与死亡圣器(下)',original_title='Harry Potter and the Deathly Hallows: Part 2',
        year='2011',score=76.32,source='html_search')
    calls=[]
    monkeypatch.setattr(prepare,'_douban_request_headers',lambda:{'Cookie':'redacted'})
    monkeypatch.setattr(prepare,'_douban_candidates',lambda names,year,**kw:calls.append((names,year)) or [row])
    package={'kind':'movie','year':'2011','tmdb':{'name':'Harry Potter and the Deathly Hallows Part 2'},
             'identification_evidence':{'douban_diagnostics':['豆瓣普通搜索页返回：搜索访问太频繁。']}}
    result=wc.verified_douban_candidates(package,[])
    assert [r['id'] for r in result]==['3011235']
    assert calls==[(['Harry Potter and the Deathly Hallows Part 2'],'2011')]


def test_fresh_rate_limit_defers_web_proposal_detail_requests(monkeypatch):
    from media_title_renamer import prepare
    monkeypatch.setattr(prepare,'_douban_request_headers',lambda:{'Cookie':'redacted'})
    def limited(_names,_year,**kwargs):
        kwargs['diagnostics'].append('豆瓣普通搜索页返回：搜索访问太频繁。')
        return []
    monkeypatch.setattr(prepare,'_douban_candidates',limited)
    class Assistant:
        last_web_search_used = False
        def __init__(self, **kwargs): pass
        def _complete_responses(self, *_args): raise AssertionError('must not search while rate-limited')
    monkeypatch.setattr(wc,'CliproxyAssistant',Assistant)
    diagnostics=[]
    rows=wc.verified_douban_candidates({'kind':'movie','year':'2011',
        'tmdb':{'name':'Harry Potter and the Deathly Hallows Part 2'}},diagnostics)
    assert rows==[]
    assert diagnostics[0]['rate_limited'] is True
    assert diagnostics[-1]['status']=='verification_deferred_rate_limited'


def test_web_search_subject_is_actually_verified_and_returned(monkeypatch):
    from media_title_renamer import prepare
    class Assistant:
        last_web_search_used = True
        def __init__(self, **kwargs): pass
        def _complete_responses(self, *_args):
            return {'urls': ['https://movie.douban.com/subject/3011235/']}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def geturl(self): return 'https://movie.douban.com/subject/3011235/'
        def read(self, *_args):
            return b'<span property="v:itemreviewed">Harry Potter and the Deathly Hallows Part 2</span><span class="year">(2011)</span>'
    monkeypatch.setattr(wc, 'CliproxyAssistant', Assistant)
    monkeypatch.setattr(wc, 'open_search', lambda *_args: Response())
    monkeypatch.setattr(prepare, '_douban_request_headers', lambda: {})
    diagnostics = []
    rows = wc.verified_douban_candidates({'kind':'movie','year':'2011',
        'tmdb':{'name':'Harry Potter and the Deathly Hallows Part 2'}}, diagnostics)
    assert [row['id'] for row in rows] == ['3011235']
    assert any(item.get('site') == 'douban_detail' and item.get('status') == 'verified' for item in diagnostics)


def test_redirected_douban_subject_is_not_treated_as_verified(monkeypatch):
    from media_title_renamer import prepare
    class Assistant:
        last_web_search_used = True
        def __init__(self, **kwargs): pass
        def _complete_responses(self, *_args):
            return {'urls': ['https://movie.douban.com/subject/3011235/']}
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def geturl(self): return 'https://www.douban.com/accounts/login'
    monkeypatch.setattr(wc, 'CliproxyAssistant', Assistant)
    monkeypatch.setattr(wc, 'open_search', lambda *_args: Response())
    monkeypatch.setattr(prepare, '_douban_request_headers', lambda: {})
    diagnostics = []
    rows = wc.verified_douban_candidates({'kind':'movie','year':'2011',
        'tmdb':{'name':'Harry Potter and the Deathly Hallows Part 2'}}, diagnostics)
    assert rows == []
    assert any(item.get('status') == 'redirected' for item in diagnostics)
    assert any(item.get('proposal_urls') == ['https://movie.douban.com/subject/3011235/'] for item in diagnostics)
