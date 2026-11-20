import json
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from torrent_search import (  # noqa: E402
    GIB,
    RateLimited,
    SearchFailure,
    SelectionPolicy,
    TorrentCandidate,
    _preflight,
    _request,
    _validate_search_payload,
    classify_language,
    classify_quality,
    execute,
    extract_media_preferences,
    parse_magnet,
    parse_search_candidate,
    rank_candidates,
    request_fingerprint,
    search_btdig,
    select_single_file,
    title_matches,
    validate_completed_video_file,
)
import torrent_search as search_module  # noqa: E402


MAGNET_A = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=Movie"
MAGNET_B = "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=Movie"
MAGNET_C = "magnet:?xt=urn:btih:" + "c" * 40 + "&dn=Movie"


def candidate(name, magnet=MAGNET_A, rank=0, seeds=10):
    raw = {"fileName": name, "fileUrl": magnet, "nbSeeders": seeds, "nbLeechers": 1}
    result = parse_search_candidate(raw, "Movie 2026", rank)
    assert result is not None
    return result


class FakeQBit:
    def __init__(self, results=None, existing=None):
        self.results = results or []
        self.torrents = {}
        self.calls = []
        self.next_id = 100
        self.existing = set(existing or [])

    def request(self, method, path, fields=None):
        fields = fields or {}
        self.calls.append((method, path, dict(fields)))
        if path == "/api/v2/search/start":
            self.next_id += 1
            return json.dumps({"id": self.next_id})
        if path == "/api/v2/search/status":
            return json.dumps([{"id": int(fields["search_id"]), "status": "Stopped", "total": len(self.results)}])
        if path == "/api/v2/search/results":
            return json.dumps({"status": "Stopped", "total": len(self.results), "results": self.results})
        if path in {"/api/v2/search/stop", "/api/v2/search/delete"}:
            return "Ok."
        if path == "/api/v2/torrents/info":
            infohash = fields.get("hashes")
            if infohash in self.torrents:
                return json.dumps([self.torrents[infohash]])
            if infohash in self.existing:
                return json.dumps([{"hash": infohash, "tags": "other"}])
            return "[]"
        if path == "/api/v2/torrents/add":
            magnet = fields["urls"]
            infohash = parse_magnet(magnet)[1]
            self.torrents[infohash] = {
                "hash": infohash,
                "tags": fields["tags"].replace(",", ","),
                "size": 3 * GIB,
                "amount_downloaded": 0,
                "state": "stoppedDL",
            }
            return "Ok."
        if path == "/api/v2/torrents/files":
            infohash = fields["hash"]
            if infohash not in self.torrents:
                return "[]"
            return json.dumps(getattr(self, "files", [{"index": 0, "name": "Movie 2026.mkv", "size": 3 * GIB, "priority": 0, "progress": 0}]))
        if path == "/api/v2/torrents/filePrio":
            for item in getattr(self, "files", []):
                if int(item["index"]) == int(fields["id"]):
                    item["priority"] = int(fields["priority"])
            return "Ok."
        if path in {"/api/v2/torrents/stop", "/api/v2/torrents/start"}:
            return "Ok."
        if path == "/api/v2/torrents/delete":
            self.torrents.pop(fields["hashes"], None)
            return "Ok."
        raise AssertionError((method, path, fields))


class SearchPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = SelectionPolicy()

    def test_lat_webdl_beats_english_webdl(self):
        values = [candidate("Movie 2026 1080p WEB-DL English", MAGNET_A, 0), candidate("Movie 2026 1080p WEB-DL LAT", MAGNET_B, 1)]
        self.assertEqual(rank_candidates(values, self.policy)[0].language, "lat")

    def test_english_webdl_beats_lat_cam(self):
        values = [candidate("Movie 2026 HDCAM LAT", MAGNET_A), candidate("Movie 2026 WEB-DL English", MAGNET_B)]
        self.assertEqual(rank_candidates(values, self.policy)[0].language, "en")

    def test_lat_cam_beats_english_cam(self):
        values = [candidate("Movie 2026 CAM English", MAGNET_A), candidate("Movie 2026 CAM LAT", MAGNET_B)]
        self.assertEqual(rank_candidates(values, self.policy)[0].language, "lat")

    def test_cam_remains_available(self):
        result = rank_candidates([candidate("Movie 2026 CAM LAT")], self.policy)
        self.assertEqual(result[0].quality, "LOW_QUALITY_FALLBACK")

    def test_recent_source_rank_wins_same_tier(self):
        values = [candidate("Movie 2026 WEB-DL LAT", MAGNET_A, 5), candidate("Movie 2026 WEB-DL LAT", MAGNET_B, 1)]
        self.assertEqual(rank_candidates(values, self.policy)[0].source_rank, 1)

    def test_language_classification_does_not_guess_spanish(self):
        self.assertEqual(classify_language("Movie 2026 Spanish Castellano"), "unknown")

    def test_quality_classification(self):
        self.assertEqual(classify_quality("Movie 2026 WEB-DL"), "NORMAL_QUALITY")
        self.assertEqual(classify_quality("Movie 2026 TELECINE"), "LOW_QUALITY_FALLBACK")


class FilePolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = SelectionPolicy()

    def test_allowlisted_extensions(self):
        for ext in ("mkv", "mp4", "avi", "mpg"):
            with self.subTest(ext=ext):
                selected = select_single_file([{"index": 0, "name": f"Movie 2026.{ext}", "size": 2 * GIB}], "Movie 2026", self.policy)
                self.assertEqual(selected.file.index, 0)

    def test_requested_resolution_and_extension_are_applied_to_file_selection(self):
        resolution, extension = extract_media_preferences("Sintel (2010) 1080p MKV")
        policy = SelectionPolicy(preferred_resolution=resolution, preferred_extension=extension)
        files = [
            {"index": 0, "name": "Sintel 2010 720p.mp4", "size": 2 * GIB},
            {"index": 1, "name": "Sintel 2010 1080p.mkv", "size": 2 * GIB},
        ]
        selected = select_single_file(files, "Sintel (2010) 1080p MKV", policy, "Sintel 2010 1080p")
        self.assertEqual(selected.file.index, 1)

    def test_requested_format_rejects_wrong_resolution_and_extension(self):
        policy = SelectionPolicy(preferred_resolution="1080p", preferred_extension=".mkv")
        with self.assertRaises(SearchFailure):
            select_single_file(
                [{"index": 0, "name": "Sintel 2010 720p.mp4", "size": 2 * GIB}],
                "Sintel (2010) 1080p MKV",
                policy,
                "Sintel 2010 720p",
            )

    def test_disallowed_extensions(self):
        for ext in ("exe", "zip", "rar", "iso", "srt", "jpg"):
            with self.subTest(ext=ext):
                with self.assertRaises(SearchFailure):
                    select_single_file([{"index": 0, "name": f"Movie 2026.{ext}", "size": 2 * GIB}], "Movie 2026", self.policy)

    def test_double_extension_uses_last_extension(self):
        with self.assertRaises(SearchFailure):
            select_single_file([{"index": 0, "name": "Movie 2026.mkv.exe", "size": 2 * GIB}], "Movie 2026", self.policy)

    def test_zero_bytes_rejected(self):
        with self.assertRaises(SearchFailure):
            select_single_file([{"index": 0, "name": "Movie 2026.mkv", "size": 0}], "Movie 2026", self.policy)

    def test_500_mib_rejected(self):
        with self.assertRaises(SearchFailure):
            select_single_file([{"index": 0, "name": "Movie 2026.mkv", "size": 500 * 1024 * 1024}], "Movie 2026", self.policy)

    def test_exact_one_gib_accepted(self):
        self.assertEqual(select_single_file([{"index": 0, "name": "Movie 2026.mkv", "size": GIB}], "Movie 2026", self.policy).file.size, GIB)

    def test_exact_five_gib_accepted(self):
        self.assertEqual(select_single_file([{"index": 0, "name": "Movie 2026.mkv", "size": 5 * GIB}], "Movie 2026", self.policy).file.size, 5 * GIB)

    def test_above_five_gib_rejected(self):
        with self.assertRaises(SearchFailure):
            select_single_file([{"index": 0, "name": "Movie 2026.mkv", "size": 5 * GIB + 1}], "Movie 2026", self.policy)

    def test_four_file_torrent_selects_only_main_video(self):
        files = [
            {"index": 0, "name": "Movie 2026.mkv", "size": 3 * GIB},
            {"index": 1, "name": "sample.mkv", "size": 150 * 1024 * 1024},
            {"index": 2, "name": "subtitles.srt", "size": 2 * 1024 * 1024},
            {"index": 3, "name": "poster.jpg", "size": 4 * 1024 * 1024},
        ]
        self.assertEqual(select_single_file(files, "Movie 2026", self.policy).file.index, 0)

    def test_two_equivalent_videos_are_ambiguous(self):
        files = [{"index": 0, "name": "Movie 2026-part1.mkv", "size": 2 * GIB}, {"index": 1, "name": "Movie 2026-part2.mkv", "size": 2 * GIB}]
        with self.assertRaises(SearchFailure) as context:
            select_single_file(files, "Movie 2026", self.policy)
        self.assertEqual(context.exception.code, "AMBIGUOUS_CONTENT")

    def test_sample_never_wins(self):
        files = [{"index": 0, "name": "Movie 2026 sample.mkv", "size": 2 * GIB}, {"index": 1, "name": "Movie 2026.mkv", "size": 3 * GIB}]
        self.assertEqual(select_single_file(files, "Movie 2026", self.policy).file.index, 1)

    def test_trailer_never_wins(self):
        with self.assertRaises(SearchFailure):
            select_single_file([{"index": 0, "name": "Movie 2026 trailer.mp4", "size": 2 * GIB}], "Movie 2026", self.policy)

    def test_path_traversal_rejected(self):
        with self.assertRaises(SearchFailure):
            select_single_file([{"index": 0, "name": "../Movie 2026.mkv", "size": 2 * GIB}], "Movie 2026", self.policy)

    def test_title_mismatch_rejected(self):
        self.assertFalse(title_matches("The Matrix 1999", "Matrix Resurrections 2021.mkv"))

    def test_requested_resolution_and_extension_are_not_title_terms(self):
        self.assertTrue(title_matches(
            "Sintel (2010) 1080p MKV",
            "Sintel.2010.1080p.NF.WEB-DL.DDP5.1.H.264-DELUNO",
        ))

    def test_btdig_search_uses_content_terms_without_format_preferences(self):
        fake = FakeQBit([])
        with self.assertRaises(SearchFailure):
            search_btdig(
                fake.request,
                "Sintel (2010) 1080p MKV",
                self.policy,
                sleep_fn=lambda _: None,
            )
        starts = [fields for method, path, fields in fake.calls if path == "/api/v2/search/start"]
        self.assertEqual(starts[0]["pattern"], "sintel 2010 LAT")


class MagnetAndFlowTests(unittest.TestCase):
    def test_magnet_validation(self):
        self.assertEqual(parse_magnet(MAGNET_A)[1], "a" * 40)

    def test_malformed_magnet_rejected(self):
        with self.assertRaises(SearchFailure) as context:
            parse_magnet("magnet:?dn=no-hash")
        self.assertEqual(context.exception.code, "NO_VALID_MAGNET")

    def test_duplicate_infohash_is_deterministically_deduplicated(self):
        values = [candidate("Movie 2026 WEB-DL LAT", MAGNET_A), candidate("Movie 2026 WEB-DL LAT", MAGNET_A, 1)]
        self.assertEqual(len(rank_candidates(values, self.policy())), 1)

    def policy(self):
        return SelectionPolicy()

    def test_request_fingerprint_is_stable(self):
        args = {"query": "Movie 2026", "min_size_gib": 1, "max_size_gib": 5}
        self.assertEqual(request_fingerprint(args, SelectionPolicy()), request_fingerprint(dict(args), SelectionPolicy()))

    def test_429_does_not_retry(self):
        class HTTP429(Exception):
            code = 429
        calls = []
        def request(*_args):
            calls.append(1)
            raise HTTP429()
        with self.assertRaises(RateLimited):
            _request(request, "GET", "/api/v2/search/status", {})
        self.assertEqual(len(calls), 1)

    def test_dry_run_never_adds_torrent(self):
        fake = FakeQBit([{"fileName": "Movie 2026 WEB-DL LAT", "fileUrl": MAGNET_A}])
        result = execute(fake.request, {"query": "Movie 2026", "dry_run": True}, sleep_fn=lambda _: None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "dry_run")
        self.assertFalse(any(path == "/api/v2/torrents/add" for _, path, _ in fake.calls))

    def test_success_starts_one_torrent_and_one_file(self):
        fake = FakeQBit([{"fileName": "Movie 2026 WEB-DL LAT", "fileUrl": MAGNET_A}])
        fake.files = [{"index": 0, "name": "Movie 2026.mkv", "size": 3 * GIB, "priority": 0, "progress": 0}, {"index": 1, "name": "sample.mkv", "size": 100, "priority": 0, "progress": 0}]
        result = execute(fake.request, {"query": "Movie 2026"}, sleep_fn=lambda _: None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["selected"]["magnet_uri"], MAGNET_A)
        self.assertEqual(result["download"], {"torrent_count_started": 1, "payload_files_selected": 1})
        starts = [call for call in fake.calls if call[1] == "/api/v2/torrents/start"]
        self.assertEqual(len(starts), 1)
        self.assertEqual([item["priority"] for item in fake.files], [1, 0])

    def test_existing_infohash_is_not_added(self):
        fake = FakeQBit([{"fileName": "Movie 2026 WEB-DL LAT", "fileUrl": MAGNET_A}], existing={"a" * 40})
        result = execute(fake.request, {"query": "Movie 2026"}, sleep_fn=lambda _: None)
        self.assertFalse(result["ok"])
        self.assertFalse(any(path == "/api/v2/torrents/add" for _, path, _ in fake.calls))

    def test_two_priorities_fail_closed(self):
        fake = FakeQBit([{"fileName": "Movie 2026 WEB-DL LAT", "fileUrl": MAGNET_B}])
        fake.files = [{"index": 0, "name": "Movie 2026.mkv", "size": 3 * GIB, "priority": 0}, {"index": 1, "name": "Movie 2026-alt.mkv", "size": 3 * GIB, "priority": 0}]
        result = execute(fake.request, {"query": "Movie 2026"}, sleep_fn=lambda _: None)
        self.assertFalse(result["ok"])
        self.assertFalse(any(path == "/api/v2/torrents/start" for _, path, _ in fake.calls))

    def test_schema_boundary_is_single_file_by_constant(self):
        from torrent_search import MAX_PAYLOAD_FILES, MAX_TORRENTS_STARTED_PER_REQUEST
        self.assertEqual((MAX_PAYLOAD_FILES, MAX_TORRENTS_STARTED_PER_REQUEST), (1, 1))

    def test_search_429_is_not_no_results(self):
        class HTTP429(Exception):
            code = 429
        def request(*_args):
            raise HTTP429()
        with self.assertRaises(RateLimited) as context:
            search_btdig(request, "Movie 2026", SelectionPolicy(), sleep_fn=lambda _: None)
        self.assertEqual(context.exception.code, "BTDIG_RATE_LIMIT")

    def test_unexpected_search_payload_is_parser_error(self):
        with self.assertRaises(SearchFailure) as context:
            _validate_search_payload({"status": "Stopped", "total": 1, "results": [{"unexpected": "html"}]})
        self.assertEqual(context.exception.code, "BTDIG_PARSER_ERROR")

    def test_running_search_is_search_timeout(self):
        calls = []
        original = search_module.SEARCH_TIMEOUT_SECONDS
        search_module.SEARCH_TIMEOUT_SECONDS = 0
        def request(method, path, fields=None):
            calls.append((method, path))
            if path == "/api/v2/search/start":
                return json.dumps({"id": 501})
            if path == "/api/v2/search/status":
                return json.dumps([{"id": 501, "status": "Running"}])
            return "Ok."
        try:
            with self.assertRaises(SearchFailure) as context:
                search_btdig(request, "Movie 2026", SelectionPolicy(), sleep_fn=lambda _: None)
            self.assertEqual(context.exception.code, "SEARCH_TIMEOUT")
        finally:
            search_module.SEARCH_TIMEOUT_SECONDS = original
        self.assertIn(("POST", "/api/v2/search/delete"), calls)

    def test_results_rejected_by_filters_are_no_valid_candidates(self):
        row = {"fileName": "Unrelated 2030.iso", "fileUrl": MAGNET_A}
        def request(method, path, fields=None):
            if path == "/api/v2/search/start":
                return json.dumps({"id": 502})
            if path == "/api/v2/search/status":
                return json.dumps([{"id": 502, "status": "Stopped", "total": 1}])
            if path == "/api/v2/search/results":
                return json.dumps({"status": "Stopped", "total": 1, "results": [row]})
            return "Ok."
        diagnostics = {}
        with self.assertRaises(SearchFailure) as context:
            search_btdig(request, "Movie 2026", SelectionPolicy(), sleep_fn=lambda _: None, diagnostics=diagnostics)
        self.assertEqual(context.exception.code, "NO_VALID_CANDIDATES")
        self.assertEqual(diagnostics["parser_accepted"], 0)
        self.assertGreater(diagnostics["results_returned"], 0)

    def test_search_positions_reset_for_non_comparable_queries(self):
        counter = {"id": 600}
        def request(method, path, fields=None):
            if path == "/api/v2/search/start":
                counter["id"] += 1
                return json.dumps({"id": counter["id"]})
            if path == "/api/v2/search/status":
                return json.dumps([{"id": int(fields["search_id"]), "status": "Stopped", "total": 1}])
            if path == "/api/v2/search/results":
                return json.dumps({"status": "Stopped", "total": 1, "results": [{"fileName": "Movie 2026 WEB-DL LAT", "fileUrl": MAGNET_A}]})
            return "Ok."
        values = search_btdig(request, "Movie 2026", SelectionPolicy(), sleep_fn=lambda _: None)
        self.assertEqual(values[0].source_rank, 0)

    def test_ffprobe_requires_a_video_stream(self):
        def runner_video(*_args, **_kwargs):
            return SimpleNamespace(returncode=0, stdout="video\n")
        def runner_audio(*_args, **_kwargs):
            return SimpleNamespace(returncode=0, stdout="audio\n")
        self.assertTrue(validate_completed_video_file("/tmp/finished.mkv", runner_video))
        self.assertFalse(validate_completed_video_file("/tmp/invalid.mkv", runner_audio))

    def test_preflight_survives_process_state_reload_without_second_add(self):
        fake = FakeQBit()
        fake.files = [{"index": 0, "name": "Movie 2026.mkv", "size": 3 * GIB, "priority": 0}]
        original = fake.request
        reloaded = {"done": False}
        def request(method, path, fields=None):
            result = original(method, path, fields)
            if path == "/api/v2/torrents/info" and fake.torrents and not reloaded["done"]:
                fake.torrents = dict(fake.torrents)
                reloaded["done"] = True
            return result
        result = _preflight(request, candidate("Movie 2026 WEB-DL LAT"), "hermes-task-restart", lambda _: None)
        self.assertEqual(len(result), 1)
        self.assertEqual(len([call for call in fake.calls if call[1] == "/api/v2/torrents/add"]), 1)

    def test_lost_add_response_is_verified_by_infohash(self):
        fake = FakeQBit()
        fake.files = [{"index": 0, "name": "Movie 2026.mkv", "size": 3 * GIB, "priority": 0}]
        original = fake.request
        lost = {"raised": False}
        def request(method, path, fields=None):
            if path == "/api/v2/torrents/add" and not lost["raised"]:
                lost["raised"] = True
                original(method, path, fields)
                raise ConnectionError("response lost after write")
            return original(method, path, fields)
        result = _preflight(request, candidate("Movie 2026 WEB-DL LAT", MAGNET_B), "hermes-task-lost", lambda _: None)
        self.assertEqual(len(result), 1)
        self.assertEqual(len([call for call in fake.calls if call[1] == "/api/v2/torrents/add"]), 1)

    def test_retry_after_started_request_is_idempotent(self):
        query = "Retry Movie 2026"
        fake = FakeQBit([{"fileName": query + " WEB-DL LAT", "fileUrl": MAGNET_C}])
        fake.files = [{"index": 0, "name": query + ".mkv", "size": 3 * GIB, "priority": 0}]
        first = execute(fake.request, {"query": query}, sleep_fn=lambda _: None)
        second = execute(fake.request, {"query": query}, sleep_fn=lambda _: None)
        self.assertTrue(first["ok"])
        self.assertEqual(second["error"], "DUPLICATE_TORRENT")
        self.assertEqual(len([call for call in fake.calls if call[1] == "/api/v2/torrents/add"]), 1)

    def test_preexisting_user_torrent_is_not_deleted(self):
        fake = FakeQBit([{"fileName": "Movie 2026 WEB-DL LAT", "fileUrl": MAGNET_A}], existing={"a" * 40})
        result = execute(fake.request, {"query": "Movie 2026"}, sleep_fn=lambda _: None)
        self.assertFalse(result["ok"])
        self.assertFalse(any(path == "/api/v2/torrents/delete" for _, path, _ in fake.calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
