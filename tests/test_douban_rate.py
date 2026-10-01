from media_title_renamer import douban_rate


def test_douban_requests_are_paced_and_other_sites_are_not(tmp_path, monkeypatch):
    state = tmp_path / 'rate.txt'
    clock = [100.0]
    delays = []
    monkeypatch.setenv('DOUBAN_REQUEST_INTERVAL', '30')
    monkeypatch.setenv('DOUBAN_RATE_STATE', str(state))
    monkeypatch.setattr(douban_rate.time, 'time', lambda: clock[0])
    def sleep(seconds):
        delays.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(douban_rate.time, 'sleep', sleep)

    douban_rate.pace_douban_request('https://search.douban.com/movie/subject_search?search_text=test')
    douban_rate.pace_douban_request('https://www.bing.com/search?q=test')
    douban_rate.pace_douban_request('https://movie.douban.com/subject/3011235/')
    clock[0] += 7
    douban_rate.pace_douban_request('https://movie.douban.com/j/subject_suggest?q=test')

    assert delays == [23]
    assert float(state.read_text()) == 130.0
