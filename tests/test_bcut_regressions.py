import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from app.core import concurrency
from app.core.bk_asr import bcut_recovery as recovery
from app.core.bk_asr.asr_data import ASRData
from app.core.bk_asr.bcut import API_QUERY_RESULT, BcutASR
from app.core.bk_asr.jianying import JianYingASR
from app.core.caption_engine_v1.origin import restore_origins


RESULT = {"utterances": [{
    "transcript": "hello, world", "start_time": 0, "end_time": 100,
    "words": [
        {"label": "hello", "start_time": 0, "end_time": 40},
        {"label": ",world", "start_time": 45, "end_time": 100},
    ],
}]}


def response(payload, status=200):
    reply = requests.Response()
    reply.status_code = status
    reply._content = json.dumps(payload).encode()
    return reply


class BcutRegressionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.patch(concurrency, "SHARED_APPDATA_PATH", root)
        self.patch(recovery, "ROOT", root / "recovery")
        self.assertNotEqual(BcutASR.RATE_LIMIT_STATE_FILE, JianYingASR.RATE_LIMIT_STATE_FILE)
        self.patch(BcutASR, "RATE_LIMIT_STATE_FILE", root / "b.json")
        self.patch(JianYingASR, "RATE_LIMIT_STATE_FILE", root / "j.json")
        self.patch(BcutASR, "RATE_LIMIT_MAX_CALLS", 1)
        self.http = self.patch(requests.Session, "request", Mock(side_effect=AssertionError("unexpected HTTP")))
        self.audio = b"test audio"

    def patch(self, target, name, value):
        handle = patch.object(target, name, value)
        self.addCleanup(handle.stop)
        return handle.start()

    def test_shared_cache_with_full_local_quota(self):
        recovery.save_result(self.audio, RESULT)
        BcutASR._record_rate_limit_usage(1)
        with patch("app.core.bk_asr.base.CacheManager") as cache:
            cache.return_value.get_asr_result.return_value = None
            result = BcutASR(self.audio, need_word_time_stamp=True).run()
        self.assertEqual([seg.text for seg in result], ["hello", ",world"])
        self.assertEqual(len(BcutASR._load_rate_limit_events(time.time())), 1)
        self.http.assert_not_called()

    def test_default_and_script_templates_reach_bcut_with_selected_cache_policy(self):
        from app.common.config import cfg
        from app.core.customer_templates import preset_defaults
        from app.core.entities import TranscribeModelEnum
        from app.core.task_factory import TaskFactory

        profile = {"settings": preset_defaults(), "style_template": ""}
        self.assertFalse(profile["settings"]["need_translate"])
        self.assertEqual(profile["settings"]["subtitle_layout"], "译文在上")
        with patch("app.core.utils.team_services.refresh_for_new_task"):
            for mode in (None, "strict", "hybrid", "script_correct"):
                for enabled in (False, True):
                    with self.subTest(mode=mode, cache=enabled), patch.object(cfg, "use_asr_cache", Mock(value=enabled)):
                        task = TaskFactory.create_transcribe_task(
                            "fixture.mp4", customer_profile=dict(profile, text_source_mode=mode)
                        )
                        self.assertEqual(task.transcribe_config.transcribe_model, TranscribeModelEnum.BIJIAN)
                        self.assertEqual(task.transcribe_config.use_asr_cache, enabled)
                        self.assertEqual(task.transcribe_config.need_word_time_stamp, mode is not None)
            profile["settings"]["subtitle_layout"] = "仅译文"
            with self.assertRaisesRegex(ValueError, "需要译文"):
                TaskFactory.create_transcribe_task("fixture.mp4", customer_profile=profile)

    def test_create_then_resume_after_412_counts_one_submission(self):
        JianYingASR._record_rate_limit_usage(5)
        self.http.side_effect = [
            response({"data": {"task_id": 123}}), response({}, 412),
            response({"code": "0", "data": {"state": "4", "result": json.dumps(RESULT)}}),
        ]
        with patch.object(BcutASR, "upload") as upload:
            with self.assertRaises(requests.HTTPError):
                BcutASR(self.audio, use_cache=False).run()
            self.assertEqual(recovery.load_job(self.audio)["task_id"], "123")
            self.assertGreater(recovery.remaining(), 0)
            with self.assertRaisesRegex(RuntimeError, "412"):
                BcutASR(self.audio, use_cache=False).run()
            # Model expiry without sleeping through the provider's cooldown.
            path = recovery.ROOT / "service.json"
            state = recovery.read(path)
            state.update(blocked_until=0, next_query_at=0)
            concurrency.atomic_text(path, json.dumps(state))
            result = BcutASR(self.audio, use_cache=False).run()
            self.assertEqual(result.segments[0].text, "hello, world")
            upload.assert_called_once()
            with self.assertRaisesRegex(RuntimeError, "本地ASR限制"):
                BcutASR(b"new audio", use_cache=False).run()
            upload.assert_called_once()
        self.assertEqual(self.http.call_count, 3)
        self.assertEqual(self.http.call_args.kwargs["params"]["task_id"], "123")
        self.assertEqual(len(BcutASR._load_rate_limit_events(time.time())), 1)

    def test_unrelated_processing_receipt_survives_recovery_probe(self):
        recovery.save_job(b"previous", "previous-id")
        concurrency.atomic_text(recovery.ROOT / "service.json", json.dumps({"task_id": "previous-id"}))
        self.http.side_effect = [
            response({"code": 0, "data": {"state": 1}}),
            response({"data": {"task_id": "current-id"}}),
            response({"data": {"state": 4, "result": RESULT}}),
        ]
        with patch.object(BcutASR, "upload"), patch.object(recovery, "pace_query"):
            BcutASR(self.audio, use_cache=False).run()
        ids = [call.kwargs["params"]["task_id"] for call in self.http.call_args_list
               if call.args[1] == API_QUERY_RESULT]
        self.assertEqual(ids, ["previous-id", "current-id"])
        self.assertEqual(recovery.load_job(b"previous")["task_id"], "previous-id")
        self.assertEqual(recovery.load_result(self.audio), RESULT)

    def test_cancel_while_queued_sends_no_requests(self):
        from app.core.bk_asr import transcribe
        from app.core.entities import TranscribeConfig, TranscribeModelEnum

        waiting = threading.Event()
        cancelled = threading.Event()
        errors = []
        config = TranscribeConfig(transcribe_model=TranscribeModelEnum.BIJIAN, use_asr_cache=False)

        def run():
            try:
                transcribe(self.audio, config, lambda *_: waiting.set(), cancel_requested=cancelled.is_set)
            except Exception as exc:
                errors.append(exc)

        recovery.ROOT.mkdir()
        with concurrency.shared_lock(recovery.ROOT / "provider-queue"):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(waiting.wait(5))
            finally:
                cancelled.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn("取消", str(errors[0]))
        self.http.assert_not_called()

    def test_existing_origin_receipts_match_only_unedited_export(self):
        asr = BcutASR(self.audio, use_cache=False, need_word_time_stamp=True)
        raw = ASRData(asr._make_segments(RESULT))
        path = recovery.ROOT / "old.origin.json"
        rows = [dict(text=s.text, start_ms=s.start_time, end_ms=s.end_time,
                     asr_segment_id=s.asr_segment_id) for s in raw]
        concurrency.atomic_text(path, json.dumps(rows))
        from_disk = ASRData.from_srt(raw.to_srt())
        self.assertEqual([s.text for s in from_disk], ["hello,", "world"])
        self.assertTrue(restore_origins(from_disk, path))
        self.assertEqual([s.asr_segment_id for s in from_disk], [0, 0])
        from_disk.segments[0].text = "edited"
        self.assertFalse(restore_origins(from_disk, path))
        from_disk.segments[0].text = "hello,"
        from_disk.segments[0].end_time += 1
        self.assertFalse(restore_origins(from_disk, path))
