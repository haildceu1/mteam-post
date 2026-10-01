#!/usr/bin/env python3
"""Batch existing auto CLI using authenticated TL S.T.; preview by default."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit, urlunsplit

# Works with the installed CLI Python or directly from the current checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import requests
from media_title_renamer.auto_publish import STATE_ROOT, duplicate_evidence, search_identity, search_site
from media_title_renamer.autonomous import files_snapshot
from media_title_renamer.batch_metadata import Resource, _classify_resource
from media_title_renamer.seed_official import DEFAULT_QB_CONFIG, DEFAULT_QB_URL, _qb_api, _qb_config_key

DEFAULT_PARSER = Path('/data/Zhyw/media-stack/moviepilot-maintenance/deploy/moviepilot-patched/brushflow_tl_seeding.py')
ARCHIVES = re.compile(r'\.(?:rar|r\d{2,3}|zip|7z|tar|tgz|tbz2?|txz|gz|bz2?|xz|zst|lz|lz4|lzma|lzo|cab|ace|arj|\d{3})$', re.I)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.writing')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def redact(line):
    if re.search(r'(?:cookie|authorization|x-api-key|passkey)\s*[:=]', line, re.I):
        return '[已隐藏认证字段]\n'
    def clean(match):
        p = urlsplit(match[0])
        if re.search(r'/(?:download|dl)/[^/?]+', p.path, re.I):
            return p.scheme + '://' + (p.hostname or '') + '/[下载地址已隐藏]'
        return urlunsplit((p.scheme, p.hostname or '', p.path, '[REDACTED]' if p.query else '', ''))
    return re.sub(r'https?://[^\s<>"\']+', clean, line)


def release_key(name, normalize, *, relaxed=False):
    text = normalize(name)
    if not relaxed:
        return re.sub(r'[^\w]', '', text)
    text = re.sub(r'(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])', ' ', text)
    # Do not erase title, year, season, release group, or video/audio codec.
    text = re.sub(r'\bhdr\s*10\b', 'hdr', text)
    return tuple(sorted(w for w in text.split() if w != 'uhd'))


def size_matches(text, size):
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*([KMGT]?i?B)\s*', text, re.I)
    if not match:
        return False
    number, unit = match.groups()
    power = {'b': 0, 'kb': 1, 'kib': 1, 'mb': 2, 'mib': 2, 'gb': 3, 'gib': 3, 'tb': 4, 'tib': 4}[unit.lower()]
    precision = len(number.split('.')[1]) if '.' in number else 0
    return abs(float(number) * 1024**power - size) <= (0.5 * 10**-precision + 0.001) * 1024**power


def match_entry(task, entries, normalize):
    for relaxed in (False, True):
        matches = [e for e in entries if release_key(e['name'], normalize, relaxed=relaxed) ==
                   release_key(task['name'], normalize, relaxed=relaxed) and size_matches(e['size_text'], task['size'])]
        if len(matches) == 1:
            return matches[0], 'release_tokens' if relaxed else 'release_name'
        if len(matches) > 1:
            return None, 'ambiguous_tl_entry'
    return None, 'no_matching_tl_entry'


def fetch_tl(args):
    if urlsplit(args.tl_url).scheme != 'https' or (urlsplit(args.tl_url).hostname or '').lower() not in {'www.torrentleech.me', 'torrentleech.me'}:
        raise ValueError('tl_url_must_be_official_https')
    spec = importlib.util.spec_from_file_location('batch_tl_parser', args.tl_parser)
    if spec is None or spec.loader is None:
        raise ValueError('tl_parser_unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cookies = module.load_torrentleech_cookies(args.tl_cookie)
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(args.tl_url, cookies=cookies,
            headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125 Safari/537.36'},
            proxies={'http': args.proxy, 'https': args.proxy} if args.proxy else None, timeout=(10, 40))
        if response.status_code != 200:
            raise ValueError('tl_http_' + str(response.status_code))
        if (urlsplit(response.url).hostname or '').lower() not in {'www.torrentleech.me', 'torrentleech.me'}:
            raise ValueError('tl_unexpected_redirect')
        entries = module.parse_seeding_page(response.text)
        if not entries:
            raise ValueError('tl_login_or_table_unavailable')
        # DataTables hides rows in the browser; the actual HTML must contain the full table.
        counts = re.findall(r'Showing\s+[\d,]+\s+to\s+[\d,]+\s+of\s+([\d,]+)\s+entries', response.text, re.I)
        if counts and len(entries) < max(int(v.replace(',', '')) for v in counts):
            raise ValueError('tl_table_incomplete')
    return entries, module.normalize_release_name


def host_path(path, args):
    virtual, mounted = Path(path), Path(args.qb_tl_root)
    if not virtual.is_absolute() or '..' in virtual.parts or not virtual.is_relative_to(mounted) or virtual == mounted:
        raise ValueError('source_outside_tl_root')
    source = args.tl_root / virtual.relative_to(mounted)
    if any(p.is_symlink() for p in (source, *source.parents)):
        raise ValueError('source_symlink')
    return source


def source_inventory(task, args, qb):
    source = host_path(task['content_path'], args)
    rows = files_snapshot(source)
    if not rows:
        raise ValueError('empty_source')
    response = qb.get(args.qb_url + '/api/v2/torrents/files', params={'hash': task['hash']}, timeout=20)
    response.raise_for_status()
    expected = set()
    for item in response.json():
        file = Path(item['name'])
        if file.is_absolute() or '..' in file.parts:
            raise ValueError('unsafe_torrent_file')
        path = host_path(str(Path(task['save_path']) / file), args)
        if path != source and not path.is_relative_to(source):
            raise ValueError('torrent_file_outside_resource')
        relative = path.relative_to(source).as_posix() if source.is_dir() else path.name
        expected.add((relative, int(item['size'])))
    actual = {(r['name'], r['bytes']) for r in rows}
    if actual != expected or sum(r['bytes'] for r in rows) != task['size'] or task['size'] != task['total_size']:
        raise ValueError('source_manifest_mismatch')
    if any(ARCHIVES.search(r['name']) for r in rows):
        raise ValueError('compressed_media_disallowed')
    if any(Path(r['name']).suffix.lower() in {'.exe', '.dll', '.pak', '.msi', '.apk', '.obb'} for r in rows):
        raise ValueError('game_or_software_payload')
    if re.search(r'\b(?:snooker|darts|badminton|table[ ._-]*tennis)\b', task['name'], re.I):
        raise ValueError('sports_event_requires_review')
    resource = Resource(task['hash'][:12], task['name'], source.name, str(source), task['size'], len(rows),
        files=[{'relative_path': r['name'], 'size_bytes': r['bytes']} for r in rows])
    _classify_resource(resource, args.tl_root)
    if resource.classification not in {'movie', 'tv'}:
        raise ValueError('classification_' + resource.classification)
    return source, rows, resource.media_kind


def inode_counter(rows):
    return Counter((r['device'], r['inode'], r['bytes']) for r in rows)


def published_history(source):
    for file in sorted(STATE_ROOT.glob('*.json')):
        if file.name == 'search-rate.json':
            continue
        item = json.loads(file.read_text())
        if not isinstance(item, dict) or 'source' not in item or 'receipt' not in item:
            continue
        prior_source = Path(item['source']).absolute()
        if prior_source != source.absolute() and not prior_source.is_relative_to(source.absolute()):
            continue
        receipt_path = Path(item['receipt'])
        if not receipt_path.is_file():
            raise ValueError('publication_history_receipt_missing')
        receipt = json.loads(receipt_path.read_text())
        if receipt.get('mteam_torrent_id'):
            return {'reason': 'already_published', 'mteam_torrent_id': receipt['mteam_torrent_id']}
        if receipt.get('submit_attempted_at'):
            return {'reason': 'prior_submission_requires_review'}
    return None


def run_auto(command, output, timeout, heartbeat=20):
    messages = queue.Queue()
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src') + os.pathsep + env.get('PYTHONPATH', '')
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True, env=env)
    def read():
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)
    threading.Thread(target=read, daemon=True).start()
    started = time.monotonic()
    last_heartbeat = started
    last_phase = '启动中'
    try:
        with (output / 'auto.log').open('a') as log:
            while True:
                if time.monotonic() - started > timeout:
                    raise ValueError('auto_timeout_requires_review')
                try:
                    line = messages.get(timeout=1)
                except queue.Empty:
                    now = time.monotonic()
                    if now - last_heartbeat >= heartbeat:
                        progress = f'[等待] 已运行 {now-started:.0f} 秒；最近阶段：{last_phase}\n'
                        log.write(progress); log.flush()
                        print(progress, end='', flush=True)
                        last_heartbeat = now
                    continue
                if line is None:
                    break
                clean = redact(line)
                if clean.startswith('[阶段]'):
                    last_phase = clean.strip()
                log.write(clean); log.flush()
                print(clean, end='', flush=True)
        return process.wait(timeout=10)
    except BaseException:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
        raise


def summary(output, payload):
    write_json(output / 'batch.json', payload)
    with (output / 'results.jsonl').open('w') as handle:
        for row in payload['resources']:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    counts = dict(Counter(r['status'] for r in payload['resources']))
    text = ['# TL 批量发布结果', '', '模式：' + payload['mode'], '更新：' + datetime.now(timezone.utc).isoformat(),
            '', '状态统计：' + json.dumps(counts, ensure_ascii=False), '', '| 资源 | 状态 | 原因 / ID |', '| --- | --- | --- |']
    for row in payload['resources']:
        reason = row.get('reason') or ', '.join(row.get('blockers') or []) or str(row.get('mteam_torrent_id') or '')
        text.append('| ' + str(row['name']).replace('|', '\\|') + ' | ' + row['status'] + ' | ' + str(reason).replace('|', '\\|') + ' |')
    (output / 'summary.md').write_text('\n'.join(text) + '\n')


def must_stop_batch(row, receipt):
    """Only stop when a final submission may have happened or its result is unclear."""
    if row.get('status') in {'publishing', 'published', 'adding', 'seed_pending', 'publish_ambiguous'}:
        return True
    if receipt.get('mteam_torrent_id') or receipt.get('submit_attempted_at'):
        return True
    # Browser or recall errors after preflight cannot be assumed to be safe to retry.
    if row.get('status') == 'publish_incomplete':
        return not (row.get('publication_status') == 'preflight'
                    and bool(row.get('blockers'))
                    and set(row.get('blockers') or []) <= {
                        'mteam_duplicate', 'mteam_search_unavailable',
                        'mteam_rate_limited', 'search_identity_missing', 'already_in_qb'
                    })
    return False


def must_stop_for_douban_rate_limit(row):
    return 'douban_search_rate_limited' in (row.get('blockers') or [])


def batch_interval_seconds(*, submit, publish_interval, preview_interval):
    return publish_interval if submit else preview_interval


def ready_hashes_from_report(path):
    """Select only still-ready, unchanged metadata previews for submission."""
    report_path = Path(path).resolve(strict=True)
    data = json.loads(report_path.read_text())
    if data.get('mode') != 'preview':
        raise ValueError('ready_report_not_preview')
    ready = set()
    for row in data.get('resources', []):
        task_hash = str(row.get('hash') or '')
        if row.get('status') != 'metadata_ready' or not re.fullmatch(r'[a-f0-9]{40}', task_hash):
            continue
        review_file = report_path.parent / 'tasks' / task_hash / 'review.json'
        if not review_file.is_file() or review_file.is_symlink():
            continue
        review = json.loads(review_file.read_text())
        if (review.get('status') == 'metadata_ready' and review.get('source_unchanged') is True
                and not review.get('blockers') and review.get('fingerprint') == row.get('fingerprint')):
            ready.add(task_hash)
    return ready


def main(argv=None):
    parser = argparse.ArgumentParser(description='TL 官方 S.T. >10天的影视资源批量 AI 预览/发布/召回/qB 做种')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--list', action='store_true', help='只盘点候选，读取官网和 qB，不调用模型或发布')
    modes.add_argument('--preview', action='store_true', help='默认：识别、制备及查重，生成预览')
    modes.add_argument('--submit', action='store_true', help='完整且高置信度时自动发布、官方召回、添加 qB，标签 Mteam')
    parser.add_argument('--hours', type=float, default=240, help='官网做种时长门槛，单位小时；默认 240 小时（10 天），严格大于')
    parser.add_argument('--limit', type=int, default=0, help='0=处理全部候选；建议首次使用 2')
    parser.add_argument('--interval', type=float, default=180, help='每个发布任务结束后的间隔秒数')
    parser.add_argument('--preview-interval', type=float, default=0, help='预览任务之间额外等待秒数；默认 0，豆瓣搜索另按 --douban-interval 节流')
    parser.add_argument('--douban-interval', type=float, default=30, help='豆瓣 HTTP 请求之间的最短间隔秒数；默认 30 秒')
    parser.add_argument('--metadata-only', action='store_true', help='仅补充和校验资料；不截图、不制种、不查重或发布')
    parser.add_argument('--ready-from', type=Path, help='提交时只处理指定预览批次中仍为 metadata_ready 的条目')
    parser.add_argument('--timeout', type=float, default=7200)
    parser.add_argument('--max-gib', type=float, default=0, help='可选单资源大小上限 GiB；默认 0 表示不限制')
    parser.add_argument('--output', type=Path, default=None, help='复用同一目录可续接已生成的预览资料')
    parser.add_argument('--tl-root', type=Path, default=Path('/data/Zhyw/media-stack/downloads/TL'))
    parser.add_argument('--qb-tl-root', default='/downloads/TL')
    parser.add_argument('--tl-url', default='https://www.torrentleech.me/profile/TronEvan/seeding')
    parser.add_argument('--tl-cookie', type=Path, default=Path('/data/Zhyw/media-stack/moviepilot-maintenance/config/torrentleech.xlsx'))
    parser.add_argument('--tl-parser', type=Path, default=DEFAULT_PARSER)
    parser.add_argument('--douban-cookie-file', type=Path, default=Path('/data/Zhyw/media-stack/moviepilot-maintenance/douban.xlsx'))
    parser.add_argument('--proxy', default='http://127.0.0.1:7890')
    web_search = parser.add_mutually_exclusive_group()
    web_search.add_argument('--web-search', dest='web_search', action='store_true', default=True,
                            help='默认启用：豆瓣候选缺失时通过 CLIProxyAPI 联网发现并核验')
    web_search.add_argument('--no-web-search', dest='web_search', action='store_false',
                            help='禁用 CLIProxyAPI 联网候选回退')
    parser.add_argument('--qb-url', default=DEFAULT_QB_URL)
    parser.add_argument('--qb-config', type=Path, default=DEFAULT_QB_CONFIG)
    parser.add_argument('--qb-container', default='qbittorrent')
    parser.add_argument('--moviepilot-container', default='moviepilot')
    parser.add_argument('--site-id', type=int, default=1)
    args = parser.parse_args(argv)
    if (args.hours < 0 or args.limit < 0 or args.interval < 60 or args.preview_interval < 0
            or args.douban_interval < 0 or args.timeout <= 0 or args.max_gib < 0):
        parser.error('hours/limit 非负，interval >=60，preview/douban interval 非负，timeout >0，max-gib >=0')
    if args.submit and args.metadata_only:
        parser.error('--metadata-only 禁止与 --submit 一起使用')
    if args.ready_from and not args.submit:
        parser.error('--ready-from 仅用于 --submit')
    try:
        ready_hashes = ready_hashes_from_report(args.ready_from) if args.ready_from else None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error('无法读取有效的待发布资料批次：' + type(exc).__name__)
    args.qb_url = args.qb_url.rstrip('/')
    args.tl_root = args.tl_root.resolve(strict=True)
    output = (args.output or Path('/data/Zhyw/media-stack/downloads/.mteam-transfer/batch-publish') /
              datetime.now().strftime('%Y%m%d-%H%M%S')).resolve()
    if output.is_relative_to(Path('/tmp')) or output.is_relative_to(args.tl_root):
        parser.error('输出目录不能位于 /tmp 或 TL 资源目录内')
    output.mkdir(parents=True, exist_ok=True)
    cache = output / 'cache'; cache.mkdir(exist_ok=True)
    os.environ['TMPDIR'] = str(cache)
    if args.proxy:
        # fetch_tl uses an explicit proxy; urllib-based TMDB/Douban lookups in
        # child CLIs must inherit the same route even from a clean screen job.
        os.environ['HTTP_PROXY'] = args.proxy
        os.environ['HTTPS_PROXY'] = args.proxy
        os.environ['http_proxy'] = args.proxy
        os.environ['https_proxy'] = args.proxy
    os.environ['DOUBAN_REQUEST_INTERVAL'] = str(args.douban_interval)
    os.environ['DOUBAN_RATE_STATE'] = str(cache / 'douban-request-rate.txt')
    os.environ.setdefault('CLIPROXY_WEB_TIMEOUT', '30')
    payload = {'mode': 'list' if args.list else 'submit' if args.submit else 'preview',
               'hours': args.hours, 'started_at': datetime.now(timezone.utc).isoformat(), 'resources': []}
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    with (STATE_ROOT / 'batch-publish.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('[停止] 已有批量任务运行', flush=True); return 2
        print('[盘点] 读取 TL 官网 S.T. 和 qB 任务…', flush=True)
        try:
            entries, normalize = fetch_tl(args)
            write_json(output / 'tl-snapshot.json', {'sampled_at': datetime.now(timezone.utc).isoformat(), 'entries': entries})
            qb = _qb_api(args.qb_url, _qb_config_key(args.qb_config))
            with qb:
                response = qb.get(args.qb_url + '/api/v2/torrents/info', timeout=20)
                response.raise_for_status(); tasks = response.json()
                eligible = []
                for task in tasks:
                    if ready_hashes is not None and task.get('hash') not in ready_hashes:
                        continue
                    if not any('torrentleech' in tag.lower() for tag in task.get('tags', '').split(',')):
                        continue
                    entry, method = match_entry(task, entries, normalize)
                    row = {k: task.get(k) for k in ('hash', 'name', 'size', 'content_path')}
                    row.update(status='skipped', reason=method)
                    payload['resources'].append(row)
                    if not entry:
                        continue
                    row.update(tl_seconds=entry['seeding_seconds'], tl_duration=entry['seeding_text'], tl_id=entry['torrent_id'])
                    if (entry['seeding_seconds'] or 0) <= args.hours * 3600:
                        row['reason'] = 'tl_time_not_over_threshold'; continue
                    if task.get('progress') != 1 or task.get('amount_left') != 0:
                        row['reason'] = 'incomplete_tl_task'; continue
                    if args.max_gib > 0 and task['size'] > args.max_gib * 1024**3:
                        row['reason'] = 'over_size_limit'; continue
                    if 'mteam' in {x.strip().lower() for x in task['tags'].split(',')}:
                        row['reason'] = 'already_in_qb'; continue
                    try:
                        source, rows, kind = source_inventory(task, args, qb)
                        history = published_history(source)
                        if history:
                            row.update(history); continue
                        for mt in tasks:
                            if 'mteam' not in {x.strip().lower() for x in mt.get('tags', '').split(',')}:
                                continue
                            if mt['content_path'] == task['content_path']:
                                raise ValueError('already_in_qb')
                            if mt['size'] == task['size']:
                                try:
                                    mt_source = Path('/data/Zhyw/media-stack/downloads') / Path(mt['content_path']).relative_to('/downloads')
                                    if inode_counter(files_snapshot(mt_source)) == inode_counter(rows):
                                        raise ValueError('already_in_qb')
                                except (OSError, ValueError) as exc:
                                    if str(exc) == 'already_in_qb': raise
                        row.update(status='candidate', reason=None, source=str(source), kind=kind,
                                   fingerprint=hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest())
                        eligible.append((task, row, rows))
                    except (ValueError, OSError, requests.RequestException) as exc:
                        row['reason'] = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[a-z0-9_]+', str(exc)) else 'source_inspection_failed'
                eligible.sort(key=lambda item: item[1]['tl_seconds'], reverse=True)
                payload.update(tl_count=len(entries), tl_over_threshold=sum((e['seeding_seconds'] or 0) > args.hours*3600 for e in entries), candidates=len(eligible))
                summary(output, payload)
                print(f"[盘点完成] 官网 {len(entries)} 条；本地符合条件 {len(eligible)} 项；报告：{output / 'summary.md'}", flush=True)
                if args.list:
                    for _, row, _ in eligible:
                        print(f"  {row['tl_duration']} | {row['size']/1024**3:.2f} GiB | {row['name']}")
                    return 0
                completed = 0
                for task, row, before in eligible[:args.limit or None]:
                    task_output = output / 'tasks' / task['hash']
                    task_output.mkdir(parents=True, exist_ok=True)
                    try:
                        # BrushFlow may remove resources after the initial inventory.
                        response = qb.get(args.qb_url + '/api/v2/torrents/info', params={'hashes': task['hash']}, timeout=20)
                        response.raise_for_status(); current = response.json()
                        if len(current) != 1 or current[0]['progress'] != 1 or current[0]['content_path'] != task['content_path']:
                            raise ValueError('tl_task_changed_or_removed')
                        if files_snapshot(Path(row['source'])) != before:
                            raise ValueError('source_changed')
                        if args.submit and args.ready_from:
                            prior_package = args.ready_from.resolve().parent / 'tasks' / task['hash'] / 'prepare' / 'mteam-prepare.json'
                            if not prior_package.is_file() or prior_package.is_symlink():
                                raise ValueError('ready_metadata_package_missing')
                            prior_draft = json.loads(prior_package.read_text())
                            if (Path(prior_draft.get('input_path', '')).resolve() != Path(row['source']).resolve()
                                    or not prior_draft.get('remain_mode')):
                                raise ValueError('ready_metadata_source_mismatch')
                            keyword = search_identity(prior_draft)
                            prior_search = search_site(keyword, container=args.moviepilot_container, site_id=args.site_id)
                            prior_matches = duplicate_evidence(prior_draft, prior_search['candidates'],
                                                               sum(item['bytes'] for item in before))
                            write_json(task_output / 'metadata-duplicate-check.json',
                                       {**prior_search, 'keyword': keyword, 'matches': prior_matches})
                            if prior_matches:
                                row.update(status='skipped_duplicate', reason='mteam_duplicate', output=str(task_output))
                                print(f"[跳过重复] {row['name']}：M-Team 已有同大小或同发布组资源", flush=True)
                                completed += 1
                                continue
                        package = task_output / 'mteam-prepare.json'
                        review_path = task_output / 'review.json'
                        input_path = Path(row['source'])
                        if args.submit and package.is_file() and review_path.is_file():
                            old = json.loads(review_path.read_text())
                            if old.get('status') == 'ready' and old.get('fingerprint') == row['fingerprint']:
                                input_path = package
                        command = [sys.executable, '-u', '-c', 'from media_title_renamer.cli import main; main()', 'auto',
                            str(input_path), '--gpt', '--submit' if args.submit else '--preview', '--output', str(task_output),
                            '--qb-url', args.qb_url, '--qb-config', str(args.qb_config), '--qb-container', args.qb_container,
                            '--moviepilot-container', args.moviepilot_container, '--site-id', str(args.site_id)]
                        if args.web_search: command.append('--web-search')
                        if args.metadata_only: command.append('--metadata-only')
                        command.extend(['--kind', row['kind']])
                        if args.douban_cookie_file.is_file(): command.extend(['--douban-cookie-file', str(args.douban_cookie_file)])
                        row.update(status='running', output=str(task_output)); summary(output, payload)
                        print(f"[{completed+1}/{min(len(eligible), args.limit or len(eligible))}] {row['name']}", flush=True)
                        code = run_auto(command, task_output, args.timeout)
                        review = json.loads(review_path.read_text()) if review_path.is_file() else {}
                        row.update(status=review.get('status', 'failed'), blockers=review.get('blockers', []), returncode=code)
                        if code and row['status'] in {'ready', 'seeded', 'metadata_ready'}:
                            row.update(status='failed', blockers=['auto_process_failed'])
                        for field in ('mteam_torrent_id', 'qb_torrent_hash', 'confidence', 'publication_status'):
                            if field in review: row[field] = review[field]
                        if not args.submit and not args.metadata_only and package.is_file():
                            draft = json.loads(package.read_text())
                            keyword = search_identity(draft)
                            result = search_site(keyword, container=args.moviepilot_container, site_id=args.site_id)
                            dup = duplicate_evidence(draft, result['candidates'], sum(r['bytes'] for r in before))
                            write_json(task_output / 'duplicate-check.json', {**result, 'matches': dup})
                            if dup: row.update(status='skipped_duplicate', reason='mteam_duplicate')
                        if files_snapshot(Path(row['source'])) != before:
                            raise ValueError('source_changed')
                        blockers = row.get('blockers', [])
                        if blockers and set(blockers) <= {'already_in_qb', 'mteam_duplicate'}:
                            row.update(status='skipped_duplicate', reason=blockers[0])
                        receipt_path = task_output / 'publish-result.json'
                        receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
                        if must_stop_batch(row, receipt):
                            payload['stopped_reason'] = 'possible_submission_requires_review'; break
                        if must_stop_for_douban_rate_limit(row):
                            payload['stopped_reason'] = 'douban_rate_limited_wait_before_retry'; break
                        if row['status'] not in {'seeded', 'ready', 'skipped_duplicate', 'metadata_ready'}:
                            print(f"[跳过本条] {row['name']}：{', '.join(blockers) or row['status']}", flush=True)
                    except (ValueError, OSError, requests.RequestException, subprocess.SubprocessError) as exc:
                        row.update(status='pending', reason=str(exc) if isinstance(exc, ValueError) and re.fullmatch('[a-z0-9_]+', str(exc)) else type(exc).__name__)
                        receipt_path = task_output / 'publish-result.json'
                        receipt = json.loads(receipt_path.read_text()) if receipt_path.is_file() else {}
                        if receipt.get('submit_attempted_at') or receipt.get('mteam_torrent_id') or row['reason'] == 'auto_timeout_requires_review':
                            payload['stopped_reason'] = 'possible_submission_requires_review'; break
                        if row['reason'] in {'mteam_rate_limited', 'mteam_search_unavailable'}:
                            payload['stopped_reason'] = row['reason']; break
                        print(f"[跳过本条] {row['name']}：{row['reason']}", flush=True)
                    finally:
                        summary(output, payload)
                    completed += 1
                    if completed < min(len(eligible), args.limit or len(eligible)):
                        interval = batch_interval_seconds(submit=args.submit, publish_interval=args.interval,
                                                          preview_interval=args.preview_interval)
                        if interval > 0:
                            deadline = time.monotonic() + interval
                            print(f'[间隔] 等待 {interval:.0f} 秒', flush=True)
                            while time.monotonic() < deadline:
                                time.sleep(max(0, min(30, deadline - time.monotonic())))
        except (ValueError, OSError, requests.RequestException, subprocess.SubprocessError) as exc:
            payload['stopped_reason'] = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[a-z0-9_]+', str(exc)) else type(exc).__name__
            print('[停止] ' + payload['stopped_reason'], flush=True)
        except KeyboardInterrupt:
            payload['stopped_reason'] = 'interrupted'
            for row in payload['resources']:
                if row['status'] == 'running':
                    row.update(status='pending', reason='interrupted_requires_review')
            print('[停止] 已中断并保存进度', flush=True)
        finally:
            summary(output, payload)
    print('[报告] ' + str(output / 'summary.md'), flush=True)
    return 2 if payload.get('stopped_reason') or any(r['status'] in {'pending', 'failed', 'publish_incomplete'} for r in payload['resources']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
