"""Recall an official M-Team torrent by detail ID using MoviePilot's site configuration.

This does not search, publish, add a downloader task, or modify media files.
The container owns authentication and constructs the same indirect download
request as its installed MTorrentSpider. Signed URLs never leave the child.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from .prepare import _bdecode, _bencode

MAX_TORRENT_BYTES = 16 * 1024 * 1024

# No second MoviePilot lifecycle is started. Only the installed spider's pure
# request descriptor builder and runtime settings are used. Site credentials
# stay inside the existing container and are never returned to the caller.
_CONTAINER_DOWNLOAD = r'''
import base64, json, sqlite3, sys
from urllib.parse import urlsplit, parse_qs, urljoin
import requests
from app.modules.indexer.spider.mtorrent import MTorrentSpider
from app.runtime.settings import get_runtime_setting

args = json.loads(sys.argv[1])
tid = args['torrent_id']
phase = 'configuration'
try:
    db = sqlite3.connect('file:/config/user.db?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    row = db.execute('SELECT id,name,domain,url,apikey,ua,proxy,timeout FROM site WHERE id=? AND is_active=1', (args['site_id'],)).fetchone()
    db.close()
    if row is None or not row['apikey']:
        raise ValueError('active_site_missing')
    domain = str(row['domain']).strip().strip('/')
    if domain != 'm-team.cc':
        raise ValueError('not_mteam_site')
    ua = row['ua'] or get_runtime_setting('USER_AGENT')
    if not ua:
        raise ValueError('user_agent_missing')
    # __get_download_url only reads these attributes; bypass the constructor,
    # which otherwise needs a running host configuration service in this child.
    spider = object.__new__(MTorrentSpider)
    spider._domain, spider._ua, spider._apikey = domain, ua, row['apikey']
    spider._proxy = get_runtime_setting('PROXY') if row['proxy'] else None
    descriptor = spider._MTorrentSpider__get_download_url(tid)
    closing = descriptor.index(']')
    params = json.loads(base64.b64decode(descriptor[1:closing]))
    endpoint = descriptor[closing + 1:]
    session = requests.Session()
    session.trust_env = False
    proxies = spider._proxy
    phase = 'generate_token'
    response = session.request(params['method'], endpoint, params=params['params'], headers=params['header'], proxies=proxies, timeout=(10,35), allow_redirects=False)
    if response.status_code != 200:
        print(json.dumps({'ok':False,'phase':phase,'http_status':response.status_code}));sys.exit(2)
    payload = response.json()
    link = payload.get(params.get('result', 'data')) if isinstance(payload,dict) else None
    if not isinstance(link,str):
        code = payload.get('code') if isinstance(payload,dict) else None
        print(json.dumps({'ok':False,'phase':phase,'api_code':code if isinstance(code,(str,int)) else None}));sys.exit(2)
    last_link_checks = None
    def validate(value, redirect=False):
        global last_link_checks
        p = urlsplit(value)
        query = parse_qs(p.query)
        ids = query.get('tid') or query.get('torrentid') or query.get('torrent_id') or query.get('id')
        last_link_checks = {'scheme':p.scheme,'host':p.hostname,'query_keys':list(query),
                            'api_path':p.path.startswith('/api/'),'has_id':bool(ids),'id_matches':bool(ids) and all(x==tid for x in ids),
                            'port_allowed':p.port in (None,443),'userinfo':bool(p.username or p.password),'fragment':bool(p.fragment)}
        api_link = p.hostname in ('api.m-team.cc','api2.m-team.cc') and p.path.startswith('/api/') and ids and all(x==tid for x in ids)
        # The authenticated API redirects official downloads to this signed CDN.
        # CDN URLs carry opaque payload/signature rather than a plain torrent ID.
        # Only accept it after the original API URL's requested ID was verified.
        cdn_link = redirect and p.hostname=='fr1.halomt.com' and {'app_id','playload','sign','t','v'}<=set(query) and (not ids or all(x==tid for x in ids))
        if p.scheme!='https' or p.port not in (None,443) or p.username or p.password or p.fragment or not (api_link or cdn_link):
            raise ValueError('unsafe_download_link')
    phase = 'download'
    validate(link)
    content = None
    for redirect in range(4):
        response = session.get(link, headers={'User-Agent':ua,'Referer':'https://kp.m-team.cc/detail/'+tid}, proxies=proxies, timeout=(10,45), allow_redirects=False, stream=True)
        if response.status_code in (301,302,303,307,308):
            phase = 'download_redirect'
            next_link = urljoin(link,response.headers.get('Location',''))
            response.close()
            validate(next_link, redirect=True);link=next_link;continue
        if response.status_code != 200:
            print(json.dumps({'ok':False,'phase':phase,'http_status':response.status_code}));sys.exit(2)
        chunks=[];size=0
        for chunk in response.iter_content(65536):
            size+=len(chunk)
            if size>16*1024*1024:raise ValueError('oversized_torrent')
            chunks.append(chunk)
        content=b''.join(chunks);response.close();break
    if not content or not content.startswith(b'd'):
        raise ValueError('not_binary_torrent')
    print(json.dumps({'ok':True,'torrent_id':tid,'site_id':row['id'],'content':base64.b64encode(content).decode('ascii')}))
except Exception as exc:
    # Exception messages and API messages may contain credentials or URLs.
    reasons = {'active_site_missing','not_mteam_site','user_agent_missing',
               'unsafe_download_link','oversized_torrent','not_binary_torrent'}
    reason = str(exc) if isinstance(exc, ValueError) and str(exc) in reasons else None
    print(json.dumps({'ok':False,'phase':phase,'error_type':type(exc).__name__,'error_reason':reason,
                      'link_checks':last_link_checks if reason=='unsafe_download_link' else None}))
    sys.exit(2)
'''


def torrent_id(value: str) -> str:
    if re.fullmatch(r"[1-9]\d*", value):
        return value
    parsed = urlsplit(value)
    match = re.fullmatch(r"/detail/([1-9]\d*)/?", parsed.path)
    if (parsed.scheme == "https" and parsed.hostname == "kp.m-team.cc"
            and parsed.port in (None, 443) and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment and match):
        return match[1]
    raise ValueError("需要 M-Team 数字 ID 或 https://kp.m-team.cc/detail/<ID> 详情页")


def _component(raw: bytes) -> str:
    if not isinstance(raw, bytes):
        raise ValueError("种子路径格式无效")
    name = raw.decode("utf-8")
    if not name or name in {".", ".."} or any(c in name for c in "/\\\x00"):
        raise ValueError("种子路径不安全")
    return name


def validate_manifest(content: bytes, source: Path) -> dict:
    """Require every original regular file, with identical paths and sizes."""
    if not content or len(content) > MAX_TORRENT_BYTES:
        raise ValueError("官方种子内容为空或体积异常")
    meta = _bdecode(content)
    info = meta.get(b"info") if isinstance(meta, dict) else None
    if not isinstance(info, dict) or info.get(b"private") != 1:
        raise ValueError("官方种子缺少私有 info 字典")
    root = _component(info.get(b"name.utf-8") or info.get(b"name"))
    piece_length, pieces = info.get(b"piece length"), info.get(b"pieces")
    if not isinstance(piece_length, int) or piece_length <= 0 or not isinstance(pieces, bytes):
        raise ValueError("官方种子 V1 分片信息无效")
    if source.is_symlink() or not source.exists():
        raise ValueError("源路径不存在或是符号链接")
    # Check every parent without resolving away a symlink first.
    if any(parent.is_symlink() for parent in source.parents):
        raise ValueError("源路径父目录包含符号链接")
    actual = {}
    if source.is_dir():
        for directory, dirs, names in os.walk(source, followlinks=False):
            for name in dirs + names:
                path = Path(directory) / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                    raise ValueError("源目录包含符号链接或特殊文件")
                if stat.S_ISREG(mode):
                    actual[path.relative_to(source).as_posix()] = path.stat().st_size
        records = info.get(b"files")
        if not isinstance(records, list) or not records:
            raise ValueError("文件夹对应的官方种子没有多文件清单")
        expected = {}
        for record in records:
            parts = record.get(b"path.utf-8") or record.get(b"path")
            if not isinstance(parts, list) or not parts:
                raise ValueError("官方种子文件路径无效")
            name = "/".join(_component(part) for part in parts)
            size = record.get(b"length")
            if name in expected or not isinstance(size, int) or size < 0:
                raise ValueError("官方种子包含重复路径或无效大小")
            expected[name] = size
    elif source.is_file():
        if b"files" in info:
            raise ValueError("单文件源与多文件官方种子不一致")
        expected = {root: info.get(b"length")}
        actual = {source.name: source.stat().st_size}
    else:
        raise ValueError("源路径不是普通文件或目录")
    if root != source.name or expected != actual:
        missing = len(set(actual) - set(expected))
        extra = len(set(expected) - set(actual))
        mismatched = sum(actual[k] != expected[k] for k in actual.keys() & expected.keys())
        raise ValueError(f"官方种子与源资源不一致：根目录一致={root == source.name}，缺失={missing}，额外={extra}，大小不符={mismatched}")
    size = sum(actual.values())
    if len(pieces) != ((size + piece_length - 1) // piece_length) * 20:
        raise ValueError("官方种子分片数量与总大小不一致")
    return {"root_name":root,"file_count":len(actual),"size_bytes":size,
            "info_hash":hashlib.sha1(_bencode(info)).hexdigest(),"private":True,
            "manifest_verified":True}


def recall(value: str, *, source: Path, output: Path, container: str = "moviepilot", site_id: int = 1) -> dict:
    tid = torrent_id(value)
    if output.exists():
        result = validate_manifest(output.read_bytes(), source)
        receipt_path = output.with_suffix(".receipt.json")
        receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
        if receipt.get("mteam_torrent_id") != tid or receipt.get("info_hash") != result["info_hash"]:
            raise ValueError("已有输出缺少匹配的官方召回凭据，拒绝复用或覆盖")
        return {**receipt,"cache_reused":True}
    process = subprocess.run(
        ["docker","exec","-i",container,"/opt/venv/bin/python","-",json.dumps({"torrent_id":tid,"site_id":site_id})],
        input=_CONTAINER_DOWNLOAD,text=True,capture_output=True,timeout=150,
    )
    # Runtime imports may emit unrelated lines. Only consume our JSON object.
    payload = None
    for line in process.stdout.splitlines():
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and "ok" in candidate:
            payload = candidate
    if not payload or not payload.get("ok") or process.returncode:
        safe = {key:payload.get(key) for key in ("phase","http_status","api_code","error_type","error_reason","link_checks")} if payload else {"error_type":"container_response_missing"}
        raise RuntimeError("MoviePilot 官方种子召回失败：" + json.dumps(safe,ensure_ascii=False))
    content = base64.b64decode(payload["content"],validate=True)
    result = validate_manifest(content, source)
    result.update({"mteam_torrent_id":tid,"mteam_detail_url":f"https://kp.m-team.cc/detail/{tid}",
                   "site_id":site_id,"torrent_path":str(output.resolve()),"cache_reused":False,
                   "channel":"MoviePilot installed MTorrentSpider + configured M-Team site"})
    output.parent.mkdir(parents=True,exist_ok=True)
    descriptor = os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,"wb") as handle:
        handle.write(content)
    receipt_path = output.with_suffix(".receipt.json")
    with receipt_path.open("x",encoding="utf-8") as handle:
        json.dump(result,handle,ensure_ascii=False,indent=2)
    return result


def main(argv: list[str] | None = None) -> None:
    parser=argparse.ArgumentParser(description="通过 MoviePilot 已配置的 M-Team 通道按详情页 ID 下载官方种子；不依赖搜索、不加入 qB")
    parser.add_argument("detail",help="M-Team 详情页 URL 或数字 ID")
    parser.add_argument("--source",required=True,type=Path,help="原始文件或资源目录，核对全部路径和大小")
    parser.add_argument("--output",required=True,type=Path,help="官方 .torrent 输出路径")
    parser.add_argument("--moviepilot-container",default="moviepilot")
    parser.add_argument("--site-id",type=int,default=1)
    args=parser.parse_args(argv)
    try:
        result=recall(args.detail,source=args.source.absolute(),output=args.output,container=args.moviepilot_container,site_id=args.site_id)
    except (ValueError,RuntimeError,OSError,subprocess.SubprocessError):
        # Network/subprocess exception strings can contain a signed URL.
        raise
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
