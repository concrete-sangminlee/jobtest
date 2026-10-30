import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
import delivery
import fetcher
import scraper

URL1 = 'https://hooks.slack.com/services/TEST/ONE/FAKE'
URL2 = 'https://hooks.slack.com/services/TEST/TWO/FAKE'
HTML = '''<ul id='articleList'>
<li class='row'><a href='/company'>로고</a><a href='/recruitment/recruits/3603171' title='강원대학교 교수 초빙'>공고</a>
<span class='td_receipt'><span class='number'>26.10.29</span><span class='number'>26.11.06</span></span></li>
</ul>'''


def job(i):
    return {'id': str(i), 'title': f'교수 초빙 {i}',
            'url': f'https://hibrain.net/recruitment/recruits/{i}', 'period': ''}


def response(status=200, text='ok', headers=None):
    return Mock(status_code=status, text=text, headers=headers or {})


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'seen.json'
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)


class ParsingTests(Isolated):
    def test_real_site_fixture_with_empty_row_and_duplicate(self):
        html = (Path(__file__).parent / 'fixtures' / 'job_list.html').read_text()
        jobs = scraper.parse_jobs(html)
        self.assertEqual(len(jobs), 6)
        self.assertEqual(jobs[0]['end_date'], '26.10.23')
        self.assertTrue(any(j['always_open'] for j in jobs))

    def test_listing_with_non_job_first_anchor_and_two_digit_dates(self):
        jobs = scraper.parse_jobs(HTML)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['id'], '3603171')
        self.assertEqual(scraper._parse_date(jobs[0]['end_date']).year, 2026)
        self.assertIsNotNone(jobs[0]['d_day'])

    def test_block_empty_and_partial_layout_fail(self):
        for text in ['<html>Access denied</html>', '<ul id="articleList"></ul>',
                     HTML.replace('</ul>', '<li class="row">broken job</li></ul>')]:
            with self.subTest(text=text), self.assertRaises(fetcher.FetchError):
                scraper.parse_jobs(text)

    def test_repeated_promoted_job_is_deduplicated(self):
        self.assertEqual(len(scraper.parse_jobs(HTML.replace('</ul>', HTML + '</ul>'))), 1)

    def test_payload_escapes_markup_and_keeps_accessible_summary(self):
        item = job(1)
        item['title'] = '<!channel> & <x|y>' + 'a' * 4000
        message = scraper.build_slack_message([item])
        text = json.dumps(message)
        self.assertNotIn('<!channel>', text)
        self.assertIn('&lt;!channel&gt;', text)
        self.assertIn('text', message)
        self.assertLess(len(message['attachments'][0]['blocks'][3]['text']['text']), 3000)

    def test_message_never_silently_truncates(self):
        with self.assertRaises(ValueError):
            scraper.build_slack_message([job(i) for i in range(21)])


class FetchTests(Isolated):
    def test_http_success_does_not_launch_browser(self):
        with patch('fetcher.requests.get', return_value=response(text=HTML)), patch('fetcher.fetch_browser') as browser:
            self.assertEqual(fetcher.fetch_page(), HTML)
            browser.assert_not_called()

    def test_block_falls_back_without_three_identical_requests(self):
        with patch('fetcher.requests.get', return_value=response(403)) as get, patch('fetcher.fetch_browser', return_value=HTML) as browser:
            self.assertEqual(fetcher.fetch_page(), HTML)
            self.assertEqual(get.call_count, 1)
            browser.assert_called_once()

    def test_transient_failure_retries(self):
        with patch('fetcher.requests.get', side_effect=[response(503), response(text=HTML)]) as get, patch('fetcher.time.sleep'):
            self.assertEqual(fetcher.fetch_http(print), HTML)
            self.assertEqual(get.call_count, 2)

    def test_http_only_does_not_launch_browser(self):
        os.environ['FETCH_MODE'] = 'http'
        with patch('fetcher.requests.get', return_value=response(403)), patch('fetcher.fetch_browser') as browser:
            with self.assertRaises(fetcher.FetchError):
                fetcher.fetch_page()
            browser.assert_not_called()

    def test_invalid_mode_fails(self):
        os.environ['FETCH_MODE'] = 'scraperapi'
        with self.assertRaises(ValueError):
            fetcher.fetch_page()

    def test_empty_200_is_not_success(self):
        with patch('fetcher.requests.get', return_value=response(text='<html>blocked</html>')):
            with self.assertRaises(fetcher.FetchError):
                fetcher.fetch_http(print)

    def test_sensitive_network_exception_is_not_logged(self):
        os.environ['FETCH_MAX_ATTEMPTS'] = '1'
        log = Mock()
        with patch('fetcher.requests.get', side_effect=requests.ConnectionError('secret-123')):
            with self.assertRaises(fetcher.FetchError):
                fetcher.fetch_http(log)
        self.assertNotIn('secret-123', str(log.call_args_list))


class DeliveryTests(Isolated):
    def test_migration_preserves_ids(self):
        self.path.write_text('["1", "2", "1"]')
        state = delivery.load_state(self.path)
        with patch('delivery.post_message', return_value=True) as post:
            result = delivery.deliver([job(1), job(3)], [URL1], self.path, state, scraper.build_slack_message)
        self.assertEqual(result['delivered'], 1)
        self.assertIn('3', state['channels'][delivery.channel_id(URL1)]['sent'])
        self.assertEqual(post.call_count, 1)
        self.assertNotIn(URL1, self.path.read_text())

    def test_partial_channel_failure_and_recovery_after_listing_disappears(self):
        state = delivery.load_state(self.path)
        with patch('delivery.post_message', side_effect=[True, False]):
            first = delivery.deliver([job(1)], [URL1, URL2], self.path, state, scraper.build_slack_message)
        self.assertEqual(first, {'delivered': 1, 'failed_channels': 1, 'pending': 1})
        with patch('delivery.post_message', return_value=True) as post:
            second = delivery.deliver([], [URL1, URL2], self.path, delivery.load_state(self.path), scraper.build_slack_message)
        self.assertEqual(second['pending'], 0)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], URL2)

    def test_all_59_jobs_are_delivered_and_restart_suppresses_duplicates(self):
        jobs = [job(i) for i in range(59)]
        with patch('delivery.post_message', return_value=True) as post, patch('delivery.time.sleep'):
            result = delivery.deliver(jobs, [URL1], self.path, delivery.load_state(self.path), scraper.build_slack_message)
            self.assertEqual(result['delivered'], 59)
            sizes = [len(call.args[1]['attachments'][0]['blocks']) - 6 for call in post.call_args_list]
            self.assertEqual(sizes, [20, 20, 19])
            delivery.deliver(jobs, [URL1], self.path, delivery.load_state(self.path), scraper.build_slack_message)
            self.assertEqual(post.call_count, 3)

    def test_outbox_saved_before_post_and_after_each_success(self):
        def send(url, message, log):
            stored = delivery.load_state(self.path)['channels'][delivery.channel_id(URL1)]
            if not stored['sent']:
                self.assertEqual(len(stored['pending']), 21)
                return True
            self.assertEqual(len(stored['sent']), 20)
            self.assertEqual(len(stored['pending']), 1)
            return False
        with patch('delivery.post_message', side_effect=send), patch('delivery.time.sleep'):
            result = delivery.deliver([job(i) for i in range(21)], [URL1], self.path, delivery.load_state(self.path), scraper.build_slack_message)
        self.assertEqual(result['pending'], 1)

    def test_no_500_id_eviction(self):
        self.path.write_text(json.dumps([str(i) for i in range(600)]))
        with patch('delivery.post_message', return_value=True):
            delivery.deliver([job(600)], [URL1], self.path, delivery.load_state(self.path), scraper.build_slack_message)
        self.assertEqual(len(delivery.load_state(self.path)['channels'][delivery.channel_id(URL1)]['sent']), 601)

    def test_corrupt_state_never_resets(self):
        for value in ['not json', '{"version": 3}', '{"version":2,"baseline":[],"channels":{"bad":{}}}']:
            self.path.write_text(value)
            with self.assertRaises(ValueError):
                delivery.load_state(self.path)
            self.assertEqual(self.path.read_text(), value)

    def test_atomic_replace_failure_preserves_previous_state(self):
        self.path.write_text('["1"]')
        with patch('delivery.os.replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                delivery.save_state(self.path, {'version': 2})
        self.assertEqual(self.path.read_text(), '["1"]')
        self.assertEqual(len(list(self.path.parent.iterdir())), 1)

    def test_concurrent_process_lock_fails(self):
        with delivery.state_lock(self.path):
            with self.assertRaises(RuntimeError):
                with delivery.state_lock(self.path):
                    pass

    def test_rate_limit_retries_then_succeeds(self):
        with patch('delivery.requests.post', side_effect=[response(429, headers={'Retry-After': '2'}), response()]) as post, patch('delivery.time.sleep') as sleep:
            self.assertTrue(delivery.post_message(URL1, {}))
            self.assertEqual(post.call_count, 2)
            sleep.assert_called_once_with(2)

    def test_long_retry_after_is_deferred(self):
        with patch('delivery.requests.post', return_value=response(429, headers={'Retry-After': '3600'})) as post, patch('delivery.time.sleep') as sleep:
            self.assertFalse(delivery.post_message(URL1, {}))
            post.assert_called_once()
            sleep.assert_not_called()

    def test_permanent_failure_not_retried_and_no_url_logging(self):
        log = Mock()
        with patch('delivery.requests.post', return_value=response(404, URL1)) as post:
            self.assertFalse(delivery.post_message(URL1, {}, log))
            post.assert_called_once()
        self.assertNotIn(URL1, str(log.call_args_list))

    def test_timeout_deferred_without_exposing_secret(self):
        log = Mock()
        with patch('delivery.requests.post', side_effect=requests.Timeout(URL1)) as post:
            self.assertFalse(delivery.post_message(URL1, {}, log))
            post.assert_called_once()
        self.assertNotIn(URL1, str(log.call_args_list))

    def test_webhook_validation_and_deduplication(self):
        os.environ['SLACK_WEBHOOK_URL'] = f'{URL1}, {URL1}, {URL2}'
        self.assertEqual(delivery.webhook_urls(), [URL1, URL2])
        for value in ['', 'http://hooks.slack.com/services/A/B/C', 'https://example.com/services/A/B/C', URL1 + '?secret=x']:
            os.environ['SLACK_WEBHOOK_URL'] = value
            with self.assertRaises(ValueError):
                delivery.webhook_urls()


class CliTests(Isolated):
    def test_failure_annotation_explains_cause_without_log_download(self):
        os.environ['GITHUB_ACTIONS'] = 'true'
        output = io.StringIO()
        with patch('scraper.fetch_page', side_effect=fetcher.FetchError('blocked\nHTTP 403')), contextlib.redirect_stdout(output):
            code = scraper.main(['--dry-run', '--state-file', str(self.path)])
        self.assertEqual(code, 1)
        self.assertIn('::error::blocked%0AHTTP 403', output.getvalue())

    def test_dry_run_fetches_and_never_writes_or_sends(self):
        self.path.write_text('["9"]')
        with patch('scraper.fetch_page', return_value=HTML), patch('delivery.requests.post') as post:
            self.assertEqual(scraper.main(['--dry-run', '--state-file', str(self.path)]), 0)
        self.assertEqual(self.path.read_text(), '["9"]')
        self.assertFalse(Path(str(self.path) + '.lock').exists())
        post.assert_not_called()

    def test_fetch_failure_is_nonzero_with_report(self):
        report = self.path.parent / 'report.json'
        with patch('scraper.fetch_page', side_effect=fetcher.FetchError('HTTP 403')):
            self.assertEqual(scraper.main(['--dry-run', '--state-file', str(self.path), '--report', str(report)]), 1)
        self.assertEqual(json.loads(report.read_text())['status'], 'failed')
        self.assertFalse(self.path.exists())

    def test_missing_webhook_fails_before_collection(self):
        with patch('scraper.scrape_jobs') as scrape:
            self.assertEqual(scraper.main(['--state-file', str(self.path)]), 1)
            scrape.assert_not_called()

    def test_fetch_failure_still_flushes_existing_outbox_but_run_fails(self):
        os.environ['SLACK_WEBHOOK_URL'] = URL1
        state = {'version': 2, 'baseline': [], 'channels': {delivery.channel_id(URL1): {'sent': [], 'pending': [job(1)]}}}
        delivery.save_state(self.path, state)
        with patch('scraper.scrape_jobs', side_effect=fetcher.FetchError('blocked')), patch('delivery.post_message', return_value=True):
            self.assertEqual(scraper.main(['--state-file', str(self.path)]), 1)
        self.assertEqual(delivery.load_state(self.path)['channels'][delivery.channel_id(URL1)]['pending'], [])

    def test_partial_delivery_returns_nonzero(self):
        os.environ['SLACK_WEBHOOK_URL'] = f'{URL1},{URL2}'
        with patch('scraper.fetch_page', return_value=HTML), patch('delivery.post_message', side_effect=[True, False]):
            self.assertEqual(scraper.main(['--state-file', str(self.path)]), 1)

    def test_report_cannot_overwrite_state(self):
        self.path.write_text('["1"]')
        # Report path is invalid; it must not be written even when reporting the error.
        with patch('scraper.scrape_jobs') as scrape:
            self.assertEqual(scraper.main(['--dry-run', '--state-file', str(self.path), '--report', str(self.path)]), 1)
        self.assertEqual(self.path.read_text(), '["1"]')
        scrape.assert_not_called()


if __name__ == '__main__':
    unittest.main()
