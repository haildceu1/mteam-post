import json
from types import SimpleNamespace

import pytest

from media_title_renamer import mteam_fill


class PublishButton:
    text = "發 布"
    clicks = 0

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def click(self):
        self.clicks += 1


class Driver:
    current_url = "https://kp.m-team.cc/detail/1269999"

    def __init__(self):
        self.button = PublishButton()

    def find_elements(self, *args):
        return [self.button]


@pytest.fixture
def prepared(monkeypatch):
    monkeypatch.setattr(mteam_fill, "_publish_preflight", lambda *args: {"title":"Diva"})
    monkeypatch.setattr(mteam_fill, "_capture_full_page", lambda *args: None)


def test_publish_uses_redirect_id_even_when_resource_is_not_searchable(tmp_path, prepared, monkeypatch):
    driver=Driver();calls=[]
    def recall(package,result,args,path):
        calls.append(result["mteam_torrent_id"])
        return result
    monkeypatch.setattr(mteam_fill,"_recall_published",recall)
    path=tmp_path/"result.json"
    result=mteam_fill._submit_publish(driver,{"title":"Diva"},SimpleNamespace(recall_official=True),path)
    assert result["mteam_torrent_id"]=="1269999"
    assert calls==["1269999"] and driver.button.clicks==1
    assert json.loads(path.read_text())["status"]=="published"
    with pytest.raises(ValueError,match="拒绝重复"):
        mteam_fill._submit_publish(driver,{"title":"Diva"},SimpleNamespace(recall_official=True),path)
    assert driver.button.clicks==1


def test_uncertain_click_is_persisted_and_never_repeated(tmp_path, prepared):
    driver=Driver()
    def broken():
        driver.button.clicks+=1
        raise RuntimeError("browser disconnected")
    driver.button.click=broken
    path=tmp_path/"result.json"
    with pytest.raises(RuntimeError):
        mteam_fill._submit_publish(driver,{"title":"Diva"},SimpleNamespace(recall_official=False),path)
    assert json.loads(path.read_text())["status"]=="publish_ambiguous"
    with pytest.raises(ValueError):
        mteam_fill._submit_publish(driver,{"title":"Diva"},SimpleNamespace(recall_official=False),path)


def test_explicit_api_rejection_is_recorded_without_sensitive_message(tmp_path, prepared):
    driver=Driver()
    driver.execute_script=lambda script: {'code':'4','message':'credential-bearing server text'} if script.startswith('return ') else None
    path=tmp_path/'rejected.json'
    with pytest.raises(RuntimeError,match='code=4'):
        mteam_fill._submit_publish(driver,{'title':'Diva'},SimpleNamespace(recall_official=False),path)
    receipt=json.loads(path.read_text())
    assert receipt['status']=='publish_rejected' and receipt['api_code']=='4'
    assert 'credential-bearing' not in path.read_text()
    assert driver.button.clicks == 1
    assert driver.button.clicks==1


def test_failed_recall_preserves_confirmed_publication_id(tmp_path, prepared, monkeypatch):
    driver=Driver()
    def broken(package,result,args,path):
        result.update(status="recall_failed")
        mteam_fill._save_publish_result(path,result)
        raise RuntimeError("candidate_download_rejected")
    monkeypatch.setattr(mteam_fill,"_recall_published",broken)
    path=tmp_path/"result.json"
    with pytest.raises(RuntimeError):
        mteam_fill._submit_publish(driver,{"title":"Diva"},SimpleNamespace(recall_official=True),path)
    saved=json.loads(path.read_text())
    assert saved["status"]=="recall_failed" and saved["mteam_torrent_id"]=="1269999"


def test_preview_remains_default():
    args=mteam_fill._parser().parse_args(["package.json","--upload","--yes"])
    assert not args.submit and not args.recall_official
