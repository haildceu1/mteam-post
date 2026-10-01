from unittest.mock import patch
from media_title_renamer.prepare import TmdbClient


def test_english_release_of_chinese_film_scores_english_search_title():
    client = TmdbClient(read_token='test-token')
    def get(path, **params):
        if path.startswith('/search/'):
            return {'results': [{'id': 1, 'title': 'Armour of God' if params['language'] == 'en-US' else '龙兄虎弟',
                                  'original_title': '龍兄虎弟', 'release_date': '1986-08-16'}]}
        return {'title': 'Armour of God' if params['language'] == 'en-US' else '龙兄虎弟',
                'original_title': '龍兄虎弟', 'release_date': '1986-08-16', 'original_language': 'cn'}
    with patch.object(client, '_get', side_effect=get):
        matches = client.search('movie', 'Armour of God', '1986')
    assert matches[0].score >= 85
    assert matches[0].chinese_name == '龙兄虎弟'
    assert matches[0].original_name == '龍兄虎弟'


def test_chinese_query_keeps_chinese_search_language():
    client = TmdbClient(read_token='test-token')
    with patch.object(client, '_get', return_value={'results': []}) as get:
        client.search('movie', '龙兄虎弟', '1986')
    assert get.call_args.kwargs['language'] == 'zh-CN'
