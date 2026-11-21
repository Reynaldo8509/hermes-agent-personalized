import json
import threading
import time
import unittest
from unittest.mock import patch

import torrent_search as search_module
from test_torrent_search import FakeQBit, MAGNET_A, MAGNET_B, MAGNET_C, candidate


class ScenarioQBit(FakeQBit):
    def __init__(self, modes=()):
        super().__init__([])
        self.modes = list(modes)
        self.added = []
        self.started = []
        self.files_by_hash = {}
        self.cleanup_failure = False
        self.fail_all = False

    def request(self, method, path, fields=None):
        fields = fields or {}
        if self.fail_all:
            raise ConnectionError("qBittorrent unavailable")
        if path == "/api/v2/torrents/info" and fields.get("filter") == "all":
            return json.dumps(list(self.torrents.values()))
        if path == "/api/v2/torrents/add":
            infohash = search_module.parse_magnet(fields["urls"])[1]
            mode = self.modes[len(self.added)]
            self.added.append(infohash)
            self.torrents[infohash] = {
                "hash": infohash,
                "tags": fields["tags"],
                "size": 3 * search_module.GIB,
                "amount_downloaded": 0,
                "progress": 0,
                "state": "metaDL",
            }
            if mode == "timeout":
                files = []
            elif mode == "invalid":
                files = [{"index": 0, "name": "Other 2026 720p.mp4", "size": 2 * search_module.GIB, "priority": 0}]
            else:
                files = [{"index": 0, "name": "Movie 2026 1080p.mkv", "size": 2 * search_module.GIB, "priority": 0}]
            self.files_by_hash[infohash] = files
            if mode == "lost_add":
                raise ConnectionError("response lost after add")
            return "Ok."
        if path == "/api/v2/torrents/files" and fields.get("hash") in self.files_by_hash:
            return json.dumps(self.files_by_hash[fields["hash"]])
        if path == "/api/v2/torrents/filePrio":
            for item in self.files_by_hash[fields["hash"]]:
                item["priority"] = int(fields["priority"])
            return "Ok."
        if path == "/api/v2/torrents/start":
            infohash = fields["hashes"]
            self.started.append(infohash)
            self.torrents[infohash]["state"] = "downloading"
            self.torrents[infohash]["progress"] = 0.001
            if self.modes[len(self.started) - 1] == "lost_start":
                raise ConnectionError("response lost after start")
            return "Ok."
        if path == "/api/v2/torrents/delete" and self.cleanup_failure:
            return "Ok."
        return super().request(method, path, fields)


class FallbackTests(unittest.TestCase):
    query = "Movie 2026 1080p MKV"

    def setUp(self):
        self.old_timeout = search_module.METADATA_TIMEOUT_SECONDS
        self.old_candidates = search_module.search_btdig
        self.old_started = set(search_module._STARTED_FINGERPRINTS)
        search_module.METADATA_TIMEOUT_SECONDS = 0.02
        search_module._STARTED_FINGERPRINTS.clear()

    def tearDown(self):
        search_module.METADATA_TIMEOUT_SECONDS = self.old_timeout
        search_module.search_btdig = self.old_candidates
        search_module._STARTED_FINGERPRINTS.clear()
        search_module._STARTED_FINGERPRINTS.update(self.old_started)

    def run_request(self, modes, magnets=(MAGNET_A, MAGNET_B, MAGNET_C), args=None):
        fake = ScenarioQBit(modes)
        values = [candidate("Movie 2026 WEB-DL LAT", magnet, rank) for rank, magnet in enumerate(magnets)]
        search_module.search_btdig = lambda *_args, **_kwargs: values
        result = search_module.execute(fake.request, args or {"query": self.query}, sleep_fn=lambda _: None)
        return result, fake

    def test_timeout_falls_back_to_second_candidate(self):
        result, fake = self.run_request(["timeout", "valid", "valid"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(fake.added), 2)
        self.assertEqual(len(fake.started), 1)
        self.assertEqual(result["selected"]["infohash"], "b" * 40)

    def test_invalid_file_falls_back_to_second_candidate(self):
        result, fake = self.run_request(["invalid", "valid"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(fake.added), 2)
        self.assertEqual(len(fake.started), 1)

    def test_valid_first_candidate_closes_fallback(self):
        result, fake = self.run_request(["valid", "valid", "valid"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(fake.added), 1)
        self.assertEqual(len(fake.started), 1)

    def test_three_invalid_candidates_return_structured_failure(self):
        result, fake = self.run_request(["invalid", "invalid", "timeout"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "NO_VALID_CANDIDATES")
        self.assertEqual(result["preflight_attempts"], 3)
        self.assertEqual(result["torrent_count_started"], 0)
        self.assertEqual([item["reason"] for item in result["rejections"]], ["NO_VALID_VIDEO_FILE", "NO_VALID_VIDEO_FILE", "METADATA_TIMEOUT"])
        self.assertEqual(len(fake.started), 0)
        self.assertFalse(fake.torrents)

    def test_fallback_is_bounded_to_three(self):
        result, fake = self.run_request(["timeout", "timeout", "timeout"], (MAGNET_A, MAGNET_B, MAGNET_C))
        self.assertEqual(result["preflight_attempts"], 3)
        self.assertEqual(len(fake.added), 3)
        self.assertEqual(search_module.SelectionPolicy().max_preflight_candidates, 3)

    def test_cancellation_during_metadata_does_not_try_next(self):
        fake = ScenarioQBit(["timeout", "valid"])
        values = [candidate("Movie 2026 WEB-DL LAT", MAGNET_A), candidate("Movie 2026 WEB-DL LAT", MAGNET_B)]
        event = threading.Event()
        token = search_module.set_operation_cancellation_event(event)
        search_module.search_btdig = lambda *_args, **_kwargs: values
        try:
            result = search_module.execute(fake.request, {"query": self.query}, sleep_fn=lambda _delay: event.set())
        finally:
            search_module.reset_operation_cancellation_event(token)
        self.assertEqual(result["error"], "REQUEST_CANCELLED")
        self.assertEqual(len(fake.added), 1)
        self.assertFalse(fake.torrents)

    def test_cleanup_failure_stops_fallback(self):
        fake = ScenarioQBit(["timeout", "valid"])
        fake.cleanup_failure = True
        values = [candidate("Movie 2026 WEB-DL LAT", MAGNET_A), candidate("Movie 2026 WEB-DL LAT", MAGNET_B)]
        search_module.search_btdig = lambda *_args, **_kwargs: values
        result = search_module.execute(fake.request, {"query": self.query}, sleep_fn=lambda _: None)
        self.assertEqual(result["error"], "CLEANUP_FAILED")
        self.assertEqual(len(fake.added), 1)
        self.assertEqual(len(fake.started), 0)

    def test_lost_add_response_is_not_retried(self):
        result, fake = self.run_request(["lost_add"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(fake.added), 1)
        self.assertEqual(len(fake.started), 1)

    def test_lost_start_response_is_reconciled_without_fallback(self):
        result, fake = self.run_request(["lost_start", "valid"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["reconciled"])
        self.assertEqual(len(fake.added), 1)
        self.assertEqual(len(fake.started), 1)

    def test_started_slow_torrent_does_not_trigger_fallback(self):
        result, fake = self.run_request(["valid", "valid"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(fake.started), 1)

    def test_preexisting_infohash_is_not_deleted(self):
        fake = ScenarioQBit(["valid"])
        fake.existing.add("a" * 40)
        values = [candidate("Movie 2026 WEB-DL LAT", MAGNET_A)]
        search_module.search_btdig = lambda *_args, **_kwargs: values
        result = search_module.execute(fake.request, {"query": self.query}, sleep_fn=lambda _: None)
        self.assertEqual(result["error"], "ALREADY_DOWNLOADING")
        self.assertFalse(any(path == "/api/v2/torrents/delete" for _, path, _ in fake.calls))

    def test_restart_with_owned_preflight_stops_without_new_candidate(self):
        fake = ScenarioQBit(["valid"])
        args = {"query": self.query}
        resolution, extension = search_module.extract_media_preferences(self.query)
        policy = search_module.SelectionPolicy(preferred_resolution=resolution, preferred_extension=extension)
        request_tag = search_module._request_tag(search_module.request_fingerprint(args, policy))
        infohash = "a" * 40
        fake.torrents[infohash] = {"hash": infohash, "tags": f"hermes-preflight,hermes-task-restart,{request_tag}", "state": "metaDL", "progress": 0, "amount_downloaded": 0}
        fake.files_by_hash[infohash] = []
        search_module.search_btdig = lambda *_args, **_kwargs: self.fail("search must not resume after restart")
        result = search_module.execute(fake.request, args, sleep_fn=lambda _: None)
        self.assertEqual(result["error"], "REQUEST_STATE_UNKNOWN")
        self.assertFalse(fake.torrents)

    def test_qbittorrent_unavailable_does_not_fallback(self):
        fake = ScenarioQBit(["valid", "valid"])
        fake.fail_all = True
        search_module.search_btdig = lambda *_args, **_kwargs: self.fail("search must not hide qBittorrent outage")
        result = search_module.execute(fake.request, {"query": self.query}, sleep_fn=lambda _: None)
        self.assertEqual(result["error"], "QBITTORRENT_UNAVAILABLE")
        self.assertEqual(len(fake.added), 0)

    def test_no_two_preflights_are_started_by_one_request(self):
        result, fake = self.run_request(["timeout", "valid"])
        self.assertEqual(len(fake.added), 2)
        self.assertEqual(len(fake.started), 1)
        self.assertTrue(result["ok"])

    def test_explicit_format_is_applied_before_fallback_acceptance(self):
        result, fake = self.run_request(["invalid", "valid"])
        self.assertTrue(result["ok"])
        selected = result["selected"]
        self.assertTrue(selected["file_name"].endswith("1080p.mkv"))
        self.assertEqual(selected["file_size_gib"], 2.0)

    def test_only_candidate_specific_codes_enable_fallback(self):
        self.assertEqual(
            search_module.FALLBACK_REJECTION_CODES,
            frozenset({"METADATA_TIMEOUT", "NO_VALID_VIDEO_FILE", "AMBIGUOUS_CONTENT", "MULTIPLE_FILES_SELECTED"}),
        )

    def test_started_download_count_is_one(self):
        self.assertEqual(search_module.MAX_TORRENTS_STARTED_PER_REQUEST, 1)
        self.assertEqual(search_module.MAX_CONCURRENT_PREFLIGHTS_PER_REQUEST, 1)

    def test_duplicate_telegram_request_does_not_start_again(self):
        fake = ScenarioQBit(["valid", "valid"])
        values = [candidate("Movie 2026 WEB-DL LAT", MAGNET_A)]
        search_module.search_btdig = lambda *_args, **_kwargs: values
        args = {"query": self.query}
        first = search_module.execute(fake.request, args, sleep_fn=lambda _: None)
        second = search_module.execute(fake.request, args, sleep_fn=lambda _: None)
        self.assertTrue(first["ok"])
        self.assertEqual(second["error"], "DUPLICATE_TORRENT")
        self.assertEqual(len(fake.started), 1)

    def test_concurrent_same_request_is_serialized(self):
        fake = ScenarioQBit(["timeout", "valid"])
        values = [candidate("Movie 2026 WEB-DL LAT", MAGNET_A), candidate("Movie 2026 WEB-DL LAT", MAGNET_B)]
        search_module.search_btdig = lambda *_args, **_kwargs: values
        entered = threading.Event()
        release = threading.Event()
        result_holder = {}

        def blocking_sleep(_delay):
            entered.set()
            release.wait(1)

        worker = threading.Thread(
            target=lambda: result_holder.setdefault(
                "first", search_module.execute(fake.request, {"query": self.query}, sleep_fn=blocking_sleep)
            )
        )
        worker.start()
        self.assertTrue(entered.wait(1))
        second = search_module.execute(fake.request, {"query": self.query}, sleep_fn=lambda _: None)
        release.set()
        worker.join(2)
        self.assertEqual(second["error"], "BUSY")
        self.assertTrue(result_holder["first"]["ok"])
        self.assertEqual(len(fake.started), 1)

    def test_cancellation_during_cleanup_does_not_start_next_candidate(self):
        fake = ScenarioQBit(["timeout", "valid"])
        values = [candidate("Movie 2026 WEB-DL LAT", MAGNET_A), candidate("Movie 2026 WEB-DL LAT", MAGNET_B)]
        search_module.search_btdig = lambda *_args, **_kwargs: values
        cancel = threading.Event()
        cleanup_entered = threading.Event()
        cleanup_release = threading.Event()
        result_holder = {}

        def signal_cancel(_delay):
            cancel.set()

        def blocked_cleanup(*_args, **_kwargs):
            cleanup_entered.set()
            cleanup_release.wait(1)
            return True

        def run_worker():
            token = search_module.set_operation_cancellation_event(cancel)
            try:
                result_holder["result"] = search_module.execute(
                    fake.request, {"query": self.query}, sleep_fn=signal_cancel
                )
            finally:
                search_module.reset_operation_cancellation_event(token)

        with patch.object(search_module, "_cleanup", side_effect=blocked_cleanup):
            worker = threading.Thread(target=run_worker)
            worker.start()
            self.assertTrue(cleanup_entered.wait(1))
            self.assertEqual(len(fake.added), 1)
            self.assertEqual(len(fake.started), 0)
            cleanup_release.set()
            worker.join(2)
        self.assertEqual(result_holder["result"]["error"], "REQUEST_CANCELLED")
        self.assertEqual(len(fake.added), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
