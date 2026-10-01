from pathlib import Path

import pytest

from media_title_renamer.prepare import _bencode
from media_title_renamer.recall_official import torrent_id, validate_manifest


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


def test_unsafe_paths_symlinks_and_false_success_are_rejected(tmp_path: Path):
    root=tmp_path/"Diva";root.mkdir();(root/"ep.mkv").write_bytes(b"v")
    with pytest.raises(ValueError,match="路径不安全"):
        validate_manifest(bundle("Diva",[(["..","ep.mkv"],1)]),root)
    with pytest.raises(ValueError,match="重复路径"):
        validate_manifest(bundle("Diva",[(["ep.mkv"],1),(["ep.mkv"],1)]),root)
    (root/"link.mkv").symlink_to(root/"ep.mkv")
    with pytest.raises(ValueError,match="符号链接"):
        validate_manifest(bundle("Diva",[(["ep.mkv"],1)]),root)
