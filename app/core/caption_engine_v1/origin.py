"""Restore ASR grouping only when the raw or exported cues still match."""
import json
from pathlib import Path

from app.core.bk_asr.asr_data import ASRData, ASRDataSeg
from app.core.utils.punctuation_boundaries import repair


def restore_origins(data, path):
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(rows, list) or len(rows) != len(data.segments):
            return False
        for row, cue in zip(rows, data.segments):
            if (row["start_ms"], row["end_ms"]) != (cue.start_time, cue.end_time):
                return False
            if not isinstance(row["text"], str):
                return False
        ids = [row["asr_segment_id"] for row in rows]
        texts = [cue.text for cue in data.segments]
        if [row["text"] for row in rows] != texts:
            # Existing receipts keep native B text. Apply the same punctuation
            # repair as to_srt, then demand an exact match for the entire file.
            raw = ASRData([ASRDataSeg(r["text"], r["start_ms"], r["end_ms"]) for r in rows])
            exported, _ = repair(raw)
            if [cue.text for cue in exported.segments] != texts:
                return False
        for cue, segment_id in zip(data.segments, ids):
            cue.asr_segment_id = segment_id
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
