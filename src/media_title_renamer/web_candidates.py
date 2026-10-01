"""Discover with hosted search, then independently verify real Douban pages."""
import html
import json
import os
import re
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from .ai import CliproxyAssistant,CliproxyError
from .douban_rate import pace_douban_request

def douban_failure_class(package, diagnostics):
    # Old draft diagnostics describe an earlier attempt. Once we have fresh
    # authenticated search evidence, it must not keep a repaired run marked
    # as rate-limited forever.
    refreshed=any(item.get('site')=='douban_authenticated_search' for item in diagnostics)
    messages = ([] if refreshed else list((package.get('identification_evidence') or {}).get('douban_diagnostics') or []))
    for item in diagnostics:
        if item.get('rate_limited') or item.get('status') in {'rate_limited','verification_deferred_rate_limited'}:
            messages.append('豆瓣详情页访问受限')
    text = ' '.join(map(str, messages))
    if re.search(r'搜索访问太频繁|访问频率过高|验证码|安全验证|豆瓣详情页访问受限|(?<!非 )HTTP\s*(?:403|429)', text, re.I):
        return 'douban_search_rate_limited'
    return 'douban_no_verified_candidates'

def search_names(package):
    tmdb = package.get('tmdb') or {}
    identity = (package.get('identification_evidence') or {}).get('identity') or {}
    names = [tmdb.get('chinese_name'), tmdb.get('original_name'), tmdb.get('name')]
    if float(identity.get('confidence') or 0) >= .85 and identity.get('kind') == package.get('kind'):
        names.extend([identity.get('search_title'), identity.get('title')])
        names.extend((identity.get('search_aliases') or [])[:2])
    return list(dict.fromkeys(str(name).strip() for name in names if name and str(name).strip()))[:4]

def canonical_douban_url(value):
    if not isinstance(value, str): return None
    try:
        parsed = urlsplit(value.strip())
        valid = (parsed.scheme == 'https' and parsed.hostname == 'movie.douban.com'
                 and not parsed.username and not parsed.password and parsed.port is None)
    except ValueError:
        return None
    if not valid:
        return None
    match = re.fullmatch(r'/subject/([1-9]\d*)/?', parsed.path)
    return 'https://movie.douban.com/subject/'+match[1]+'/' if match else None

def open_search(request):
    pace_douban_request(request.full_url)
    try:return urllib.request.urlopen(request,timeout=15)
    except urllib.error.HTTPError:
        # A server-side 403/429 is rate limiting, not a transport failure.
        # Do not immediately retry it via another IP address.
        raise
    except (urllib.error.URLError,TimeoutError):
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({'https':'http://127.0.0.1:7890','http':'http://127.0.0.1:7890'}))
        return opener.open(request,timeout=15)

def public_search_urls(context,diagnostics):
    """Read actual RSS search results; model memories are not candidate evidence."""
    tmdb=context.get('tmdb') or {};season=re.search(r'S(\d+)',str(context.get('season') or ''))
    suffix=f' 第{int(season[1])}季' if season else ''
    names=context.get('names') or [tmdb.get('chinese_name'),tmdb.get('name')]
    query='site:movie.douban.com/subject/ '+str(next((name for name in names if name),'')).strip()+suffix
    if not query.split('subject/ ')[-1].strip():
        diagnostics.append({'site':'public_search','status':'missing_title'});return []
    url='https://www.bing.com/search?'+urllib.parse.urlencode({'q':query,'format':'rss'})
    try:
        with open_search(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})) as response:
            data=response.read(2*1024*1024)
        root=ET.fromstring(data)
        urls=[canonical_douban_url(node.text) for node in root.findall('.//item/link') if node.text]
        diagnostics.append({'site':'public_search','status':'ok','query':query,'candidate_count':len(urls)})
        return list(dict.fromkeys(url for url in urls if url))[:3]
    except Exception as exc:
        diagnostics.append({'site':'public_search','status':'error','error_type':type(exc).__name__});return []

def verified_douban_candidates(package, diagnostics):
    from .prepare import _douban_request_headers,_season_number,TmdbClient,_douban_candidates
    assistant=CliproxyAssistant(web_search=True,timeout=60)
    context={'tmdb':package.get('tmdb'),'kind':package.get('kind'),'season':package.get('episode'),'year':package.get('year')}
    context['names']=search_names(package)
    season=re.fullmatch(r'S(\d{2})',str(package.get('episode') or ''))
    tmdb=package.get('tmdb') or {}
    if season and tmdb.get('id'):
        try:
            client=TmdbClient(read_token=os.environ.get('TMDB_READ_ACCESS_TOKEN',''),api_key=os.environ.get('TMDB_API_KEY',''))
            if client.available:context['season_air_year']=client.season(tmdb['id'],int(season[1])).year
        except Exception:pass
    # Historical diagnostics belong to the old attempt; a refreshed, valid
    # cookie deserves one bounded current search before using web fallback.
    if context['names'] and 'Cookie' in _douban_request_headers():
        from dataclasses import asdict
        number=int(season[1]) if season else None
        query=str(next(iter(context['names']),''))
        messages=[]
        search_year=context.get('season_air_year') or (package.get('year') if number in {None,1} else None)
        rows=_douban_candidates([query],search_year,expected_season=number,diagnostics=messages)
        rows=[r for r in rows if r.score>=75 and (number is None or r.season_number in {number,None})]
        fresh_limited=any(re.search(r'搜索访问太频繁|访问频率过高|验证码|安全验证|HTTP\s*(?:403|429)',m,re.I) for m in messages)
        diagnostics.append({'site':'douban_authenticated_search','candidate_count':len(rows),'cookie_used':True,
                            'rate_limited':fresh_limited})
        if rows:
            return [{**asdict(row),'site':'douban','source':'authenticated_douban_search'} for row in rows]
        if fresh_limited:
            # Neither web proposals nor direct detail probes can verify a
            # candidate while the site is actively limiting this session.
            diagnostics.append({'site':'douban_detail','status':'verification_deferred_rate_limited'})
            return []
    try:
        result=assistant._complete_responses(
            '必须调用 web_search 根据 names、year、kind、season 查找真实豆瓣 subject 页面；不能仅依赖 tmdb（可能为空）。系列首播年不能当成当前季年份；当前季优先用 season_air_year。不要凭记忆猜 ID。只返回 JSON {"urls":[最多3个真实 https://movie.douban.com/subject/数字/ 链接]}，找不到则为空数组。',
            json.dumps(context,ensure_ascii=False))
    except CliproxyError:
        diagnostics.append({'site':'douban_web','status':'search_unavailable'});result={}
    if not assistant.last_web_search_used:diagnostics.append({'site':'douban_web','status':'tool_not_used'})
    values=result.get('urls') if assistant.last_web_search_used else []
    if not values:values=public_search_urls(context,diagnostics)
    if not isinstance(values,list):
        diagnostics.append({'site':'douban_web','status':'invalid_schema'});return []
    values=list(dict.fromkeys(url for url in (canonical_douban_url(value) for value in values) if url))
    if not values: values=public_search_urls(context,diagnostics)
    diagnostics.append({'site':'douban_web','status':'searched','proposal_count':len(values),
                        'proposal_ids':[urlsplit(url).path.split('/')[2] for url in values],
                        'proposal_urls':values,
                        'web_search_used':assistant.last_web_search_used})
    found=[]
    for url in values[:3]:
        if not isinstance(url,str):continue
        match=re.fullmatch(r'https://movie\.douban\.com/subject/([1-9]\d*)/',url)
        if not match:
            diagnostics.append({'site':'douban_detail','status':'invalid_candidate'})
            continue
        try:
            request=urllib.request.Request(url,headers=_douban_request_headers())
            with open_search(request) as response:
                final=urlsplit(response.geturl())
                if final.hostname!='movie.douban.com' or final.path.rstrip('/')!='/subject/'+match[1]:
                    diagnostics.append({'site':'douban_detail','id':match[1],'status':'redirected'})
                    continue
                body=response.read(2*1024*1024).decode('utf-8','replace')
            heading=re.search(r'<span[^>]*property=["\']v:itemreviewed["\'][^>]*>(.*?)</span>',body,re.S)
            year=re.search(r'<span[^>]*class=["\']year["\'][^>]*>\((\d{4})\)</span>',body)
            if not heading or not year:
                diagnostics.append({'site':'douban_detail','id':match[1],'status':'rate_limited' if re.search(r'访问太频繁|验证码|安全验证',body) else 'unverifiable'});continue
            title=html.unescape(re.sub('<[^>]+>','',heading[1])).strip()
            tmdb=package.get('tmdb') or {};original=str(tmdb.get('original_name') or tmdb.get('name') or '')
            original=original if original.casefold() in title.casefold() else ''
            chinese=title[:title.casefold().find(original.casefold())].strip() if original else title
            found.append({'id':match[1],'title':chinese or title,'original_title':original,'year':year[1],
                          'season_number':_season_number(title),'site':'douban','source':'hosted_web_search_and_verified_subject'})
            diagnostics.append({'site':'douban_detail','id':match[1],'status':'verified','web_search_used':True})
        except urllib.error.HTTPError as exc:
            diagnostics.append({'site':'douban_detail','id':match[1],
                                'status':'rate_limited' if exc.code in {403,429} else 'http_error',
                                'http_status':exc.code})
        except Exception as exc:
            diagnostics.append({'site':'douban_detail','id':match[1],'status':'error','error_type':type(exc).__name__})
    return found
