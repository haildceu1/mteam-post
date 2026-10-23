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


class NoticeButton:
    text = "知道了"

    def __init__(self, dialog):
        self.dialog = dialog
        self.clicks = 0

    def click(self):
        self.clicks += 1
        self.dialog.visible = False


class NoticeDialog:
    text = "侦测到可连接服务器 已切换可用服务器，重新载入网页时生效"

    def __init__(self):
        self.visible = True
        self.button = NoticeButton(self)

    def is_displayed(self):
        return self.visible

    def find_elements(self, *_args):
        return [self.button]


class NoticeDriver(Driver):
    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog

    def find_elements(self, _by, selector):
        return [self.dialog] if selector == ".ant-modal-wrap" else [self.button]


class ElementClickInterceptedException(Exception):
    pass


class LateNoticeDriver(NoticeDriver):
    def __init__(self, dialog):
        super().__init__(dialog)
        self.modal_appeared = False
        self.request_started = False
        original_click = self.button.click

        def click():
            if not self.modal_appeared:
                self.modal_appeared = True
                dialog.visible = True
                raise ElementClickInterceptedException("overlay")
            original_click()

        self.button.click = click

    def find_elements(self, _by, selector):
        if selector == ".ant-modal-wrap":
            return [self.dialog] if self.modal_appeared else []
        return [self.button]

    def execute_script(self, script, *_args):
        if "__mteamPublishRequestStarted === true" in script:
            return self.request_started
        if "__mteamPublishResponse" in script:
            return None
        return None


class UnknownLateModalDriver(LateNoticeDriver):
    def __init__(self, dialog):
        super().__init__(dialog)
        dialog.text = "请求失败，请稍后重试"


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


def test_only_explicitly_authorized_pre_dispatch_receipt_can_be_retried(
    tmp_path, prepared, monkeypatch
):
    driver=Driver()
    path=tmp_path/'pre-dispatch.json'
    path.write_text(json.dumps({
        'status':'submission_not_dispatched','title':'Diva','submit_attempted_at':123,
        'click_intercepted':True,'create_request_started':False,'retry_authorized':True,
        'error':'ElementClickInterceptedException before dispatch'
    }))
    result=mteam_fill._submit_publish(
        driver,{'title':'Diva'},SimpleNamespace(recall_official=False),path
    )
    assert result['status']=='published'
    assert len(result['prior_non_dispatched_attempts'])==1
    assert result['prior_non_dispatched_attempts'][0]['submit_attempted_at']==123


def test_user_requested_ambiguous_retry_preserves_original_receipt(tmp_path, prepared):
    driver = Driver()
    path = tmp_path / 'result.json'
    previous = {'status':'publish_ambiguous', 'title':'Diva',
                'submit_attempted_at':123, 'error':'redirect was not confirmed'}
    path.write_text(json.dumps(previous))
    result = mteam_fill._submit_publish(
        driver, {'title':'Diva'},
        SimpleNamespace(recall_official=False, retry_ambiguous=True), path,
    )
    assert driver.button.clicks == 1
    assert result['mteam_torrent_id'] == '1269999'
    assert result['ambiguous_retry_used'] is True
    assert json.loads((tmp_path/'result.ambiguous-123.json').read_text()) == previous
    assert result['prior_ambiguous_attempts'][0]['submit_attempted_at'] == 123


def test_user_requested_retry_is_not_repeated_after_another_ambiguous_click(tmp_path, prepared):
    driver = Driver()
    path = tmp_path / 'result.json'
    path.write_text(json.dumps({'status':'publish_ambiguous','submit_attempted_at':123}))
    def disconnected():
        driver.button.clicks += 1
        raise RuntimeError('browser disconnected')
    driver.button.click = disconnected
    args = SimpleNamespace(recall_official=False, retry_ambiguous=True)
    with pytest.raises(RuntimeError):
        mteam_fill._submit_publish(driver, {'title':'Diva'}, args, path)
    assert json.loads(path.read_text())['ambiguous_retry_used'] is True
    with pytest.raises(ValueError):
        mteam_fill._submit_publish(driver, {'title':'Diva'}, args, path)
    assert driver.button.clicks == 1


def test_ambiguous_retry_cannot_use_a_new_receipt_path_or_confirmed_id(tmp_path, prepared):
    driver = Driver()
    path = tmp_path / 'new-result.json'
    args = SimpleNamespace(recall_official=False, retry_ambiguous=True)
    with pytest.raises(ValueError, match='已有回执路径'):
        mteam_fill._submit_publish(driver, {'title':'Diva'}, args, path)
    path.write_text(json.dumps({'status':'published','mteam_torrent_id':'1269999'}))
    with pytest.raises(ValueError, match='拒绝重复点击'):
        mteam_fill._submit_publish(driver, {'title':'Diva'}, args, path)
    assert driver.button.clicks == 0


def test_unknown_not_dispatched_receipt_without_authorization_stays_locked(
    tmp_path, prepared
):
    driver=Driver()
    path=tmp_path/'unknown-pre-dispatch.json'
    path.write_text(json.dumps({
        'status':'submission_not_dispatched','submit_attempted_at':123,
        'click_intercepted':True,'create_request_started':False
    }))
    with pytest.raises(ValueError,match='拒绝重复点击'):
        mteam_fill._submit_publish(
            driver,{'title':'Diva'},SimpleNamespace(recall_official=False),path
        )
    assert driver.button.clicks==0


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


def test_dismisses_only_known_server_switch_notice():
    dialog = NoticeDialog()
    driver = NoticeDriver(dialog)

    assert mteam_fill._dismiss_known_server_switch_notice(driver) is True
    assert dialog.visible is False
    assert dialog.button.clicks == 1


def test_does_not_dismiss_unrelated_modal():
    dialog = NoticeDialog()
    dialog.text = "确认删除这个种子？"
    driver = NoticeDriver(dialog)

    assert mteam_fill._dismiss_known_server_switch_notice(driver) is False
    assert dialog.visible is True
    assert dialog.button.clicks == 0


def test_late_known_server_notice_is_dismissed_then_revalidated_and_clicked_once(
    tmp_path, prepared, monkeypatch
):
    dialog = NoticeDialog()
    dialog.visible = False
    driver = LateNoticeDriver(dialog)
    checks = []
    monkeypatch.setattr(mteam_fill, "_publish_preflight",
                        lambda *_args: checks.append(True) or {"title": "Diva"})
    path = tmp_path / "late-notice.json"

    result = mteam_fill._submit_publish(
        driver, {"title": "Diva"}, SimpleNamespace(recall_official=False), path
    )

    assert result["status"] == "published"
    assert result["late_server_notice_dismissed"] is True
    assert driver.button.clicks == 1  # only the successful, unblocked click dispatches
    assert len(checks) == 2


def test_unknown_late_modal_is_recorded_as_not_dispatched_and_stops(
    tmp_path, prepared, monkeypatch
):
    dialog = NoticeDialog()
    dialog.visible = False
    driver = UnknownLateModalDriver(dialog)
    path = tmp_path / "unknown-modal.json"

    with pytest.raises(RuntimeError, match="未知弹窗"):
        mteam_fill._submit_publish(
            driver, {"title": "Diva"}, SimpleNamespace(recall_official=False), path
        )

    receipt = json.loads(path.read_text())
    assert receipt["status"] == "submission_not_dispatched"
    assert receipt["create_request_started"] is False
    assert receipt["intercepted_modal"][0]["text"] == "请求失败，请稍后重试"
    assert driver.button.clicks == 0


def test_intercept_with_started_request_remains_ambiguous(tmp_path, prepared):
    driver = Driver()
    driver.execute_script = lambda script: True if "__mteamPublishRequestStarted === true" in script else None

    def intercepted():
        raise ElementClickInterceptedException("intercepted after request")

    driver.button.click = intercepted
    path = tmp_path / "started-request.json"

    with pytest.raises(RuntimeError, match="无法证明请求未发出"):
        mteam_fill._submit_publish(
            driver, {"title": "Diva"}, SimpleNamespace(recall_official=False), path
        )

    receipt = json.loads(path.read_text())
    assert receipt["status"] == "publish_ambiguous"
    assert receipt["create_request_started"] is True
