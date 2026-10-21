"""Offline regression tests. Run from project root: python -m unittest discover -s tests -v"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from selenium import webdriver
from selenium.common.exceptions import WebDriverException, SessionNotCreatedException
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
import scan_control
import seaart_browser as browser
import scanner
from scanners import http_retry as http, tensorhub, seaart


class HttpTests(unittest.TestCase):
    def setUp(self):
        scan_control.reset()
        http.reset_retry_stats()
        self.session = Mock()

    def tearDown(self):
        scan_control.reset()

    def get(self, **kw):
        return http.get_with_backoff(self.session, 'https://example.invalid', provider='Test', cancellable=True, **kw)

    def test_remote_disconnect_recovers_with_jitter(self):
        response = Mock(status_code=200)
        self.session.get.side_effect = [requests.ConnectionError('Remote end closed connection without response'), response]
        with patch.object(scan_control, 'wait_or_cancel') as wait:
            self.assertIs(self.get(), response)
        self.assertEqual(self.session.get.call_count, 2)
        self.assertGreaterEqual(wait.call_args.args[0], 1)
        self.assertLessEqual(wait.call_args.args[0], 1.5)
        self.assertEqual(http.get_retry_stats()['Test']['recovered'], 1)
        self.assertEqual(self.session.get.call_args.kwargs['timeout'], (10, 30))

    def test_dns_exhausts_bounded_budget(self):
        self.session.get.side_effect = requests.ConnectionError('getaddrinfo failed')
        with patch.object(scan_control, 'wait_or_cancel'), self.assertRaises(requests.ConnectionError):
            self.get(max_retries=2)
        self.assertEqual(self.session.get.call_count, 3)
        self.assertIn('DNS', http.get_retry_stats()['Test']['last_error'])

    def test_ssl_not_retried(self):
        self.session.get.side_effect = requests.exceptions.SSLError('certificate')
        with self.assertRaises(requests.exceptions.SSLError):
            self.get()
        self.assertEqual(self.session.get.call_count, 1)

    def test_permanent_http_not_retried(self):
        for code in (400, 401, 403, 404, 500):
            self.session.get.reset_mock()
            response = Mock(status_code=code)
            self.session.get.return_value = response
            self.assertIs(self.get(), response)
            self.assertEqual(self.session.get.call_count, 1)

    def test_rate_limit_closes_response_and_preserves_retry_after(self):
        response = Mock(status_code=429, headers={'Retry-After': '7'})
        self.session.get.side_effect = [response, Mock(status_code=200)]
        with patch.object(scan_control, 'wait_or_cancel') as wait:
            self.get()
        response.close.assert_called_once()
        wait.assert_called_once_with(7)

    def test_long_retry_after_returns_without_early_retry(self):
        response = Mock(status_code=429, headers={'Retry-After': '3600'})
        self.session.get.return_value = response
        self.assertIs(self.get(), response)
        self.assertEqual(self.session.get.call_count, 1)

    def test_non_scan_request_ignores_previous_stop(self):
        scan_control.stop_scan()
        response = Mock(status_code=200)
        self.session.get.return_value = response
        self.assertIs(http.get_with_backoff(self.session, 'https://example.invalid', provider='Test'), response)

    def test_stop_before_request(self):
        scan_control.stop_scan()
        with self.assertRaises(scan_control.ScanCancelled):
            self.get()
        self.session.get.assert_not_called()

    def test_stop_during_backoff(self):
        self.session.get.side_effect = requests.Timeout('timeout')
        def stop(_):
            scan_control.stop_scan()
            scan_control.check_cancelled()
        with patch.object(scan_control, 'wait_or_cancel', side_effect=stop), self.assertRaises(scan_control.ScanCancelled):
            self.get()
        self.assertEqual(self.session.get.call_count, 1)

    def test_stop_after_pacing_does_not_send(self):
        with patch.object(http, '_pace_request', side_effect=lambda *a: scan_control.stop_scan()), self.assertRaises(scan_control.ScanCancelled):
            self.get()
        self.session.get.assert_not_called()


class BrowserTests(unittest.TestCase):
    def setUp(self):
        scan_control.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.root_patch = patch.object(browser, '_ROOT', self.root)
        self.root_patch.start()
        self.live = browser.SeaArtLiveSession(cancellable=True)
        self.options = Options()
        self.options.binary_location = '/fake/firefox'

    def tearDown(self):
        scan_control.reset()
        self.root_patch.stop()
        self.tmp.cleanup()

    def start(self):
        return self.live._start_firefox(webdriver, self.options, self.root)

    def test_windows_service_flags_and_error_only_logs(self):
        with patch.object(browser, '_console_kwargs', return_value={'creationflags': 0x08000000}), patch.object(webdriver, 'Firefox') as launch:
            self.start()
            service = launch.call_args.kwargs['service']
            self.assertEqual(service.creation_flags, 0x08000000)
            self.assertIn('error', service.service_args)
            self.assertIn('seaart-startup', service.log_output.name)
            service.stop()

    def test_transient_failure_retries_once_and_stops_service(self):
        services = []
        def fail(**kw):
            services.append(kw['service'])
            raise WebDriverException('Process unexpectedly closed with status 0')
        with patch.object(webdriver, 'Firefox', side_effect=fail), patch.object(Service, 'stop') as stop, patch.object(scan_control, 'wait_or_cancel') as wait, self.assertRaises(browser.BrowserStartupError):
            self.start()
        self.assertEqual(len(services), 2)
        self.assertGreaterEqual(stop.call_count, 2)
        wait.assert_called_once_with(2.0)
        for service in services:
            service.log_output.close()

    def test_permanent_startup_failure_no_retry(self):
        with patch.object(webdriver, 'Firefox', side_effect=SessionNotCreatedException('version mismatch')) as launch, patch.object(scan_control, 'wait_or_cancel') as wait, self.assertRaises(browser.BrowserStartupError):
            self.start()
        self.assertEqual(launch.call_count, 1)
        wait.assert_not_called()

    def test_profile_cleanup_targets_only_matched_pid(self):
        profile = self.root
        service = Mock()
        with patch.object(browser.os, 'name', 'nt'), patch.object(browser, '_profile_process_ids', return_value=[1234]), patch.object(browser.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
            self.live._cleanup_failed_firefox(service, profile)
        self.assertEqual(run.call_args.args[0], ['taskkill', '/PID', '1234', '/T', '/F'])
        service.stop.assert_called_once()

    def test_windows_console_helper_flags(self):
        with patch.object(browser.os, 'name', 'nt'), patch.object(browser.subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True):
            self.assertEqual(browser._console_kwargs(), {'creationflags': 0x08000000})

    def test_firefox_configuration(self):
        with patch.object(browser, '_PROFILE_ROOT', self.root), patch.object(browser, '_browser_executable', return_value='/fake/firefox'), patch.object(browser, '_profile_process_ids', return_value=[]), patch.object(self.live, '_start_firefox', return_value=Mock()) as launch:
            self.live.browser = 'firefox'
            self.live._open_driver()
        options = launch.call_args.args[1]
        self.assertIn('-headless', options.arguments)
        self.assertIn('-no-remote', options.arguments)
        self.assertIn('-profile', options.arguments)
        self.assertTrue(options.enable_bidi)

    def test_download_session_ignores_stopped_scan(self):
        live = browser.SeaArtLiveSession()
        scan_control.stop_scan()
        live._check_cancelled()

    def test_lock_wait_cancellable(self):
        scan_control.stop_scan()
        with patch.object(browser, 'browser_session_saved', return_value=True), patch.object(browser, '_LIVE_LOCK') as lock, self.assertRaises(scan_control.ScanCancelled):
            lock.acquire.return_value = False
            self.live.__enter__()

    def test_reconnect_refuses_to_interrupt_live_work(self):
        with patch.object(browser, '_THREAD', None), patch.object(browser, '_LIVE_LOCK') as lock, self.assertRaises(RuntimeError):
            lock.acquire.return_value = False
            browser.start_browser_connection()

    def test_disconnect_refuses_to_delete_live_profile(self):
        with patch.object(browser, '_LIVE_LOCK') as lock, patch.object(browser.shutil, 'rmtree') as delete, self.assertRaises(RuntimeError):
            lock.acquire.return_value = False
            browser.disconnect_browser_session()
        delete.assert_not_called()

    def test_exception_releases_live_lock(self):
        with patch.object(browser, 'browser_session_saved', return_value=True), patch.object(self.live, '_open_driver', side_effect=browser.BrowserStartupError('failure')), patch.object(browser, '_LIVE_LOCK') as lock, self.assertRaises(browser.BrowserStartupError):
            self.live.__enter__()
        lock.release.assert_called_once()


class IsolationAndMossTests(unittest.TestCase):
    def setUp(self):
        scan_control.reset()

    def test_startup_failure_stops_only_that_source(self):
        jobs = [{'watch': 'one'}, {'watch': 'two'}]
        fail = {'models': [], 'error': 'startup failed', 'fatal_source': True}
        ok = {'models': [], 'error': None}
        with patch.object(scanner, '_run_one_source_job', side_effect=[fail, dict(ok), dict(ok)]) as run:
            sea = scanner._scan_source_jobs('seaart', Mock(), jobs, {})
            other = scanner._scan_source_jobs('huggingface', Mock(), jobs, {})
        self.assertEqual(len(sea['jobs']), 1)
        self.assertEqual(len(other['jobs']), 2)
        self.assertEqual(run.call_count, 3)

    def test_single_and_combined_coordinator_report_failure(self):
        import scan_status
        for sources in (['seaart'], ['seaart', 'huggingface']):
            registry = {name: SimpleNamespace(ENABLED=True, DISPLAY=name) for name in sources}
            def result(name, *args):
                return {'models': [], 'jobs': [{'error': 'Firefox startup failed' if name == 'seaart' else None}], 'duration': 0}
            counts = dict(processed=0, added=0, updated=0, unchanged=0, images=0, videos=0)
            with patch.object(scanner, 'ALL_SCANNERS', registry), patch.object(scanner, 'database'), patch.object(scanner, 'get_search_settings', return_value={}), patch.object(scanner, '_apply_scan_preflights'), patch.object(scanner, '_scan_source_jobs', side_effect=result), patch.object(scanner, '_commit_source_models', return_value=counts):
                scanner.run_scan(selected_sources=sources, explicit_plan={name: [{'watch': 'test'}] for name in sources})
            self.assertEqual(scan_status.get_source_health()['seaart']['status'], 'error')
            if 'huggingface' in sources:
                self.assertEqual(scan_status.get_source_health()['huggingface']['status'], 'ok')

    def test_tensorhub_base_and_sft_are_distinct(self):
        self.assertEqual(tensorhub._architecture_name('MOSS-Image-Base'), 'MOSS Image Base')
        self.assertEqual(tensorhub._architecture_name('MOSS-Image-SFT'), 'MOSS Image')
        item = {'model': {'baseModel': 'MOSS-Image-Base'}}
        self.assertTrue(tensorhub._matches_base_model(item, 'MOSS-Image-Base'))
        self.assertFalse(tensorhub._matches_base_model(item, 'MOSS-Image-SFT'))
        config = json.loads(Path('app_config/architectures.json').read_text())
        for name, term in [('MOSS Image', 'MOSS-Image-SFT'), ('MOSS Image Base', 'MOSS-Image-Base')]:
            self.assertEqual(config[name]['source_searches']['tensorhub'], {'mode': 'base_model', 'terms': [term]})

    def test_seaart_wrapper_always_unwinds_resources(self):
        resource = Mock()
        def failing(*args):
            stack = args[-1]
            stack.callback(resource.close)
            raise RuntimeError('detail failure')
        with patch.object(seaart, '_scan_impl', side_effect=failing), self.assertRaises(RuntimeError):
            seaart.scan('Krea 2')
        resource.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
