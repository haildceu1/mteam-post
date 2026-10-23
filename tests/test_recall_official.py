from pathlib import Path
import base64
import hashlib
import json
import subprocess

import pytest

from media_title_renamer.prepare import _bdecode, _bencode
from media_title_renamer.recall_official import torrent_id, validate_manifest, verify_piece_hashes


def link_validator():
    import ast
    from urllib.parse import urlsplit,parse_qs
    from media_title_renamer.recall_official import _CONTAINER_DOWNLOAD
    tree=ast.parse(_CONTAINER_DOWNLOAD)
    function=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='validate')
    scope={'urlsplit':urlsplit,'parse_qs':parse_qs,'tid':'123','last_link_checks':None}
    exec(compile(ast.Module(body=[function],type_ignores=[]),'validator','exec'),scope)
    return scope['validate']


def test_official_signed_cdn_redirect_is_bound_to_validated_api_entry():
    validate=link_validator()
    validate('https://api.m-team.cc/api/torrent/download?tid=123&sign=test')
    cdn='https://fr1.halomt.com/download?app_id=test&playload=test&sign=test&t=1&v=1'
    validate(cdn,redirect=True)
    with pytest.raises(ValueError):validate(cdn)
    with pytest.raises(ValueError):validate(cdn+'&tid=999',redirect=True)


@pytest.mark.parametrize('url',[
    'http://fr1.halomt.com/download?app_id=x&playload=x&sign=x&t=1&v=1',
    'https://fr1.halomt.com.evil.example/download?app_id=x&playload=x&sign=x&t=1&v=1',
    'https://api2.m-team.cc/api/torrent/download?tid=999&sign=x',
    'https://fr1.halomt.com/download?app_id=x&sign=x',
])
def test_cdn_policy_still_rejects_untrusted_redirects(url):
    with pytest.raises(ValueError):link_validator()(url,redirect=True)


def bundle(root: str, files: list[tuple[list[str], int]]) -> bytes:
    size=sum(n for _,n in files)
    return _bencode({b"info":{b"name":root.encode(),b"private":1,b"piece length":1024,
        b"pieces":b"p"*(((size+1023)//1024)*20),
        b"files":[{b"path":[p.encode() for p in path],b"length":n} for path,n in files]}})


def content_bundle(root: str, files: list[tuple[str,bytes]], piece_length: int) -> bytes:
    stream=b''.join(content for _,content in files)
    pieces=b''.join(hashlib.sha1(stream[i:i+piece_length]).digest()
                    for i in range(0,len(stream),piece_length))
    return _bencode({b'info':{b'name':root.encode(),b'private':1,
        b'piece length':piece_length,b'pieces':pieces,
        b'files':[{b'path':[name.encode()],b'length':len(content)} for name,content in files]}})


def test_piece_hash_verification_accepts_reordered_files_and_different_piece_sizes(tmp_path):
    root=tmp_path/'Release';root.mkdir()
    (root/'a.bin').write_bytes(b'abcdefgh')
    (root/'b.bin').write_bytes(b'ijklmnopq')
    uploaded=content_bundle('Release',[('a.bin',b'abcdefgh'),('b.bin',b'ijklmnopq')],4)
    official=content_bundle('Release',[('b.bin',b'ijklmnopq'),('a.bin',b'abcdefgh')],3)
    assert verify_piece_hashes(uploaded,root)['pieces_verified']==5
    assert verify_piece_hashes(official,root)['pieces_verified']==6
    damaged=_bdecode(official)
    damaged[b'info'][b'pieces']=bytes([damaged[b'info'][b'pieces'][0]^1])+damaged[b'info'][b'pieces'][1:]
    with pytest.raises(ValueError,match='torrent_piece_hash_mismatch'):
        verify_piece_hashes(_bencode(damaged),root)


def test_candidate_detail_id_does_not_require_search():
    assert torrent_id("https://kp.m-team.cc/detail/1261744")=="1261744"
    assert torrent_id("1261744")=="1261744"
    for value in ("https://evil.example/detail/1261744","https://kp.m-team.cc/detail/1261744?sign=secret","https://kp.m-team.cc/detail/0"):
        with pytest.raises(ValueError):torrent_id(value)


def test_entire_multiseason_tree_is_required(tmp_path: Path):
    root=tmp_path/"Diva";root.mkdir()
    for season in ("S01","S02"):
        (root/season).mkdir();(root/season/"episode.mkv").write_bytes(b"video")
    content=bundle("Diva",[(["S01","episode.mkv"],5),(["S02","episode.mkv"],5)])
    result=validate_manifest(content,root)
    assert result["file_count"]==2 and result["size_bytes"]==10
    with pytest.raises(ValueError,match="缺失=1"):
        validate_manifest(bundle("Diva",[(["S01","episode.mkv"],5)]),root)
    with pytest.raises(ValueError,match="大小不符=1"):
        validate_manifest(bundle("Diva",[(["S01","episode.mkv"],4),(["S02","episode.mkv"],5)]),root)


def test_one_file_multifile_torrent_matches_release_folder_file(tmp_path: Path):
    root = tmp_path / "Film"
    root.mkdir()
    source = root / "Film.mkv"
    source.write_bytes(b"video")
    result = validate_manifest(bundle("Film", [(["Film.mkv"], 5)]), source)
    assert result["root_name"] == "Film"
    assert result["file_count"] == 1
    assert result["size_bytes"] == 5
    with pytest.raises(ValueError, match="布局不一致"):
        validate_manifest(bundle("Other", [(["Film.mkv"], 5)]), source)


def test_moviepilot_recall_passes_clash_fallback_without_persisting_proxy(tmp_path,monkeypatch):
    from media_title_renamer import recall_official
    source=tmp_path/'source';source.mkdir();output=tmp_path/'official.torrent'
    monkeypatch.setattr(recall_official,'validate_manifest',lambda content,path:
        {'info_hash':'a'*40,'size_bytes':1,'manifest_verified':True})
    captured={}
    payload={'ok':True,'torrent_id':'123','site_id':1,
             'content':base64.b64encode(b'dummy').decode('ascii')}
    def fake_run(command,**kwargs):
        captured['args']=json.loads(command[-1])
        return subprocess.CompletedProcess(command,0,json.dumps(payload),'')
    monkeypatch.setattr(recall_official.subprocess,'run',fake_run)
    result=recall_official.recall('123',source=source,output=output,proxy_url='http://127.0.0.1:7890')
    assert captured['args']['proxy_url']=='http://127.0.0.1:7890'
    assert 'proxy_url' not in result
    assert '127.0.0.1' not in output.with_suffix('.receipt.json').read_text()


def test_official_recall_does_not_inherit_search_proxy_when_site_proxy_is_disabled(
    tmp_path, monkeypatch
):
    from media_title_renamer import recall_official
    source=tmp_path/'source';source.mkdir();output=tmp_path/'official.torrent'
    monkeypatch.setenv('MTEAM_SEARCH_PROXY_URL','http://127.0.0.1:7890')
    monkeypatch.delenv('MTEAM_RECALL_PROXY_URL',raising=False)
    monkeypatch.setattr(recall_official,'validate_manifest',lambda content,path:
        {'info_hash':'b'*40,'size_bytes':1,'manifest_verified':True})
    captured={}
    payload={'ok':True,'torrent_id':'123','site_id':1,
             'content':base64.b64encode(b'dummy').decode('ascii')}
    def fake_run(command,**kwargs):
        captured['args']=json.loads(command[-1])
        return subprocess.CompletedProcess(command,0,json.dumps(payload),'')
    monkeypatch.setattr(recall_official.subprocess,'run',fake_run)

    recall_official.recall('123',source=source,output=output)

    assert captured['args']=={'torrent_id':'123','site_id':1}


def test_explicit_empty_recall_proxy_means_direct_access(tmp_path,monkeypatch):
    from media_title_renamer import recall_official
    source=tmp_path/'source';source.mkdir();output=tmp_path/'official.torrent'
    monkeypatch.setenv('MTEAM_RECALL_PROXY_URL','http://127.0.0.1:7890')
    monkeypatch.setattr(recall_official,'validate_manifest',lambda content,path:
        {'info_hash':'c'*40,'size_bytes':1,'manifest_verified':True})
    captured={}
    payload={'ok':True,'torrent_id':'123','site_id':1,
             'content':base64.b64encode(b'dummy').decode('ascii')}
    def fake_run(command,**kwargs):
        captured['args']=json.loads(command[-1])
        return subprocess.CompletedProcess(command,0,json.dumps(payload),'')
    monkeypatch.setattr(recall_official.subprocess,'run',fake_run)

    recall_official.recall('123',source=source,output=output,proxy_url='')

    assert captured['args']=={'torrent_id':'123','site_id':1,'proxy_url':''}


def test_moviepilot_descriptor_resolves_api_domain_before_token_request():
    import ast
    from media_title_renamer.recall_official import _CONTAINER_DOWNLOAD

    tree = ast.parse(_CONTAINER_DOWNLOAD)
    assignments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Attribute) and target.attr == '_api_domain'
                for target in node.targets)
    ]
    descriptor_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == '_MTorrentSpider__get_download_url'
    ]
    assert assignments and descriptor_calls
    assignment = min(assignments, key=lambda node: node.lineno)
    descriptor_call = min(descriptor_calls, key=lambda node: node.lineno)
    assert assignment.lineno < descriptor_call.lineno
    assert any(isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
               and child.func.id == 'resolver' for child in ast.walk(assignment.value))


def test_unsafe_paths_symlinks_and_false_success_are_rejected(tmp_path: Path):
    root=tmp_path/"Diva";root.mkdir();(root/"ep.mkv").write_bytes(b"v")
    with pytest.raises(ValueError,match="路径不安全"):
        validate_manifest(bundle("Diva",[(["..","ep.mkv"],1)]),root)
    with pytest.raises(ValueError,match="重复路径"):
        validate_manifest(bundle("Diva",[(["ep.mkv"],1),(["ep.mkv"],1)]),root)
    (root/"link.mkv").symlink_to(root/"ep.mkv")
    with pytest.raises(ValueError,match="符号链接"):
        validate_manifest(bundle("Diva",[(["ep.mkv"],1)]),root)
