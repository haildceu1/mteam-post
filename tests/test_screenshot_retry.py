from media_title_renamer import mteam_fill


class Input:
    def __init__(self): self.files=[]
    def send_keys(self, filename): self.files.append(filename)


class Driver:
    def __init__(self, removed): self.input=Input();self.removed=removed
    def find_elements(self,*args): return [self.input]
    def execute_script(self,*args): return self.removed


def test_explicit_failed_upload_is_retried_after_removal(monkeypatch):
    states=iter([False,True]);driver=Driver(True)
    monkeypatch.setattr(mteam_fill,'_wait_for_image_uploads',lambda *args:next(states))
    removed=[]
    monkeypatch.setattr(mteam_fill,'_remove_failed_or_stalled_image',lambda d,name:removed.append(name) or True)
    monkeypatch.setattr(mteam_fill.time,'sleep',lambda *args:None)
    assert mteam_fill._upload_screenshot(driver,'/workspace/a.jpg',1)
    assert driver.input.files==['/workspace/a.jpg']*2
    assert removed==['a.jpg']


def test_stalled_upload_is_retried_only_after_cancel_is_verified(monkeypatch):
    driver=Driver(True);states=iter([False,True])
    monkeypatch.setattr(mteam_fill,'_wait_for_image_uploads',lambda *args:next(states))
    monkeypatch.setattr(mteam_fill,'_remove_failed_or_stalled_image',lambda *args:True)
    monkeypatch.setattr(mteam_fill.time,'sleep',lambda *args:None)
    assert mteam_fill._upload_screenshot(driver,'/workspace/a.jpg',1)
    assert driver.input.files==['/workspace/a.jpg']*2


def test_uncertain_upload_is_not_repeated_when_row_cannot_be_cancelled(monkeypatch):
    driver=Driver(False)
    monkeypatch.setattr(mteam_fill,'_wait_for_image_uploads',lambda *args:False)
    monkeypatch.setattr(mteam_fill,'_remove_failed_or_stalled_image',lambda *args:False)
    assert not mteam_fill._upload_screenshot(driver,'/workspace/a.jpg',1)
    assert len(driver.input.files)==1
