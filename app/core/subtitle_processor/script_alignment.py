"""Episode-script alignment for word-timestamp subtitles.

ASR owns timing.  Once an episode script is strongly matched, the script owns
the wording inside anchored spans (including names and 他/她/它).  Unmatched
audio is kept so ad-libs or a wrong episode reference cannot be forced in.
"""

from __future__ import annotations

import difflib
import re
from itertools import combinations
from typing import Any, Dict, Iterable, List, Sequence, Tuple


MIN_EPISODE_MATCH_RATIO = 0.82
AUTHORITATIVE_MATCH_RATIO = 0.88
MAX_REPLACEMENT_CHARS = 4
MAX_AUTHORITATIVE_REPLACEMENT_CHARS = 12
MAX_INSERTION_CHARS = 3
MAX_ALIGNMENT_EDIT_INTERNAL_GAP_MS = 600
PERSPECTIVE_PRONOUNS = set("我你您他她它咱")
GENDER_PRONOUN_FORMS = {"他", "她", "它", "他们", "她们", "它们"}
FILLER_CHARS = set("嗯啊呀哦噢呃额唉哎诶呵哈")
TERMINAL_PUNCTUATION = set("。！？!?…")
CLAUSE_PUNCTUATION = set("，,；;、")
SINGLE_CHAR_VOCATIVES = set("哥姐爸妈爷奶叔婶姨舅")
NUMBER_CHARS = set("0123456789零〇一二三四五六七八九十百千万两")
GRAMMAR_HOMOPHONES = set("的地得")


def _is_spoken_char(char: str) -> bool:
    return bool(char) and (char.isalnum() or "\u4e00" <= char <= "\u9fff")


def _spoken_chars(text: str) -> str:
    return "".join(char for char in (text or "") if _is_spoken_char(char))


def _dialogue_only(line: str) -> str:
    line = (line or "").strip()
    if not line:
        return ""
    for delimiter in ("：", ":"):
        if delimiter in line:
            prefix, content = line.split(delimiter, 1)
            if prefix.strip() and content.strip():
                return content.strip()
    return line


def extract_script_dialogue(script_reference: str) -> str:
    """Remove speaker labels while keeping dialogue punctuation and order."""
    return "".join(
        _dialogue_only(line)
        for line in (script_reference or "").splitlines()
        if _dialogue_only(line)
    )


def extract_script_speakers(script_reference: str) -> List[str]:
    """Return canonical speaker labels from the per-episode reference."""
    speakers = []
    for raw_line in (script_reference or "").splitlines():
        line = raw_line.strip()
        for delimiter in ("：", ":"):
            if delimiter in line:
                speaker = line.split(delimiter, 1)[0].strip()
                if 1 < len(speaker) <= 12 and speaker not in speakers:
                    speakers.append(speaker)
                break
    return speakers


def _expand_segments(segments: Sequence[Any]) -> List[Any]:
    """Expand mixed word/character segments into timestamped spoken chars."""
    expanded: List[Any] = []
    for segment in segments:
        chars = _spoken_chars(getattr(segment, "text", ""))
        if not chars:
            continue
        start = int(getattr(segment, "start_time"))
        end = int(getattr(segment, "end_time"))
        duration = max(0, end - start)
        segment_type = type(segment)
        for index, char in enumerate(chars):
            char_start = start + round(duration * index / len(chars))
            char_end = start + round(duration * (index + 1) / len(chars))
            expanded.append(segment_type(char, char_start, char_end))
    return expanded


def _anchor_sizes(opcodes: Sequence[Tuple[str, int, int, int, int]], index: int) -> Tuple[int, int]:
    left = 0
    right = 0
    if index > 0 and opcodes[index - 1][0] == "equal":
        left = opcodes[index - 1][2] - opcodes[index - 1][1]
    if index + 1 < len(opcodes) and opcodes[index + 1][0] == "equal":
        right = opcodes[index + 1][2] - opcodes[index + 1][1]
    return left, right


def _merged_edit_spans(
    opcodes: Sequence[Tuple[str, int, int, int, int]], max_equal_bridge: int = 2
) -> List[Tuple[int, int, int, int, int, int, int, int]]:
    """Merge edits separated by 1-2 equal chars (common in misheard names).

    For example 海诚 -> 镇海 is represented by SequenceMatcher as insertion
    镇 + equal 海 + deletion 诚. Treating that as one anchored replacement is
    necessary for deterministic name correction.
    """
    spans = []
    index = 0
    while index < len(opcodes):
        if opcodes[index][0] == "equal":
            index += 1
            continue
        start = index
        end = index
        while end + 2 < len(opcodes):
            bridge = opcodes[end + 1]
            following = opcodes[end + 2]
            bridge_size = bridge[2] - bridge[1]
            if (
                bridge[0] == "equal"
                and bridge_size <= max_equal_bridge
                and following[0] != "equal"
            ):
                end += 2
            else:
                break
        if end > start:
            left = opcodes[start - 1][2] - opcodes[start - 1][1] if start > 0 else 0
            right = (
                opcodes[end + 1][2] - opcodes[end + 1][1]
                if end + 1 < len(opcodes)
                else 0
            )
            spans.append(
                (
                    start,
                    end,
                    opcodes[start][1],
                    opcodes[end][2],
                    opcodes[start][3],
                    opcodes[end][4],
                    left,
                    right,
                )
            )
        index = end + 1
    return spans


def _safe_replacement(
    source: str,
    target: str,
    left: int,
    right: int,
    canonical_speakers: Sequence[str],
) -> bool:
    if not source or not target:
        return False
    # Written/spoken number forms may differ in length (36 -> 三十六), and a
    # number beside a sentence pause may have only two same-side anchor chars.
    number_change = all(char in NUMBER_CHARS for char in source + target)
    if (
        number_change
        and len(source) <= MAX_REPLACEMENT_CHARS
        and len(target) <= MAX_AUTHORITATIVE_REPLACEMENT_CHARS
        and (
            (left >= 3 and right >= 3)
            or (left >= 2 and right >= 6)
            or (left >= 6 and right >= 2)
        )
    ):
        return True
    if len(source) != len(target):
        return False
    if len(source) > MAX_REPLACEMENT_CHARS or left < 3 or right < 3:
        return False
    if not all(_is_spoken_char(char) for char in target):
        return False
    if any(char in PERSPECTIVE_PRONOUNS for char in source + target):
        return False
    # Audio stays authoritative.  Deterministic replacement is limited to
    # canonical cast names, number notation, grammar homophones, or a filler
    # occupying the exact script position of a strongly anchored spoken token.
    target_is_name = any(target in speaker for speaker in canonical_speakers)
    grammar_change = all(char in GRAMMAR_HOMOPHONES for char in source + target)
    filler_recovery = all(char in FILLER_CHARS for char in source)
    return target_is_name or number_change or grammar_change or filler_recovery


def _strongly_anchored(left: int, right: int) -> bool:
    """Allow an edit only when its position in this episode is unambiguous."""
    return (left >= 3 and right >= 3) or (left == 0 and right >= 8) or (right == 0 and left >= 8)


def _authoritative_replacement(
    source: str, target: str, left: int, right: int
) -> bool:
    """Accept script wording in a short, strongly anchored matched span."""
    return (
        bool(source)
        and bool(target)
        and len(source) <= MAX_AUTHORITATIVE_REPLACEMENT_CHARS
        and len(target) <= MAX_AUTHORITATIVE_REPLACEMENT_CHARS
        and _strongly_anchored(left, right)
        and all(_is_spoken_char(char) for char in source + target)
    )


def _span_crosses_real_pause(
    units: Sequence[Any], start_index: int, end_index: int
) -> bool:
    """Do not let one script edit consume words from two speaking turns."""
    for index in range(start_index + 1, end_index):
        gap = int(units[index].start_time) - int(units[index - 1].end_time)
        if gap >= MAX_ALIGNMENT_EDIT_INTERNAL_GAP_MS:
            return True
    return False


def _pause_isolated_safe_replacement(
    units: Sequence[Any],
    start_index: int,
    end_index: int,
    source: str,
    target: str,
    left_anchor: int,
    right_anchor: int,
    canonical_speakers: Sequence[str],
) -> bool:
    """Re-evaluate one edit island beside a pause with one strong anchor.

    A composite SequenceMatcher span can contain independent edits on both
    sides of a pause. Rejecting the composite must not discard a script-proven
    numeral, canonical name, gender pronoun, or grammar correction.
    """
    if not source or not target:
        return False
    if (
        len(source) > MAX_AUTHORITATIVE_REPLACEMENT_CHARS
        or len(target) > MAX_AUTHORITATIVE_REPLACEMENT_CHARS
        or not all(_is_spoken_char(char) for char in source + target)
    ):
        return False

    pause_before = (
        start_index > 0
        and int(units[start_index].start_time)
        - int(units[start_index - 1].end_time)
        >= MAX_ALIGNMENT_EDIT_INTERNAL_GAP_MS
    )
    pause_after = (
        end_index < len(units)
        and int(units[end_index].start_time)
        - int(units[end_index - 1].end_time)
        >= MAX_ALIGNMENT_EDIT_INTERNAL_GAP_MS
    )
    one_sided_anchor = (pause_before and right_anchor >= 6) or (
        pause_after and left_anchor >= 6
    )
    if not one_sided_anchor:
        return False

    number_change = all(char in NUMBER_CHARS for char in source + target)
    gender_change = source in GENDER_PRONOUN_FORMS and target in GENDER_PRONOUN_FORMS
    grammar_change = all(char in GRAMMAR_HOMOPHONES for char in source + target)
    target_is_name = any(target in speaker for speaker in canonical_speakers)
    return number_change or gender_change or grammar_change or target_is_name


def _make_replacement_units(
    units: Sequence[Any], start_index: int, end_index: int, text: str
) -> List[Any]:
    """Put script characters on ASR timings without flattening real pauses.

    When script correction removes characters (for example 哈天哪 -> 天呐),
    choose an ordered subset of the original character timestamps. Exact
    character matches are strongly preferred, so the one-second pause before
    天 remains visible to the downstream temporal splitter.
    """
    if start_index >= end_index or not text:
        return []
    source_units = list(units[start_index:end_index])
    source_text = "".join(str(unit.text) for unit in source_units)
    unit_type = type(units[start_index])

    if len(text) <= len(source_units):
        best_indices = None
        best_score = None
        for indices in combinations(range(len(source_units)), len(text)):
            mismatch_cost = sum(
                0 if source_text[source_index] == target_char else 10
                for source_index, target_char in zip(indices, text)
            )
            skipped_cost = len(source_units) - len(indices)
            edge_cost = indices[0] + (len(source_units) - 1 - indices[-1])
            score = mismatch_cost + skipped_cost + edge_cost * 0.1
            if best_score is None or score < best_score:
                best_score = score
                best_indices = indices
        if best_indices is not None:
            return [
                unit_type(
                    char,
                    int(source_units[source_index].start_time),
                    int(source_units[source_index].end_time),
                )
                for source_index, char in zip(best_indices, text)
            ]

    start = int(units[start_index].start_time)
    end = int(units[end_index - 1].end_time)
    if end <= start:
        return []
    duration = end - start
    result = []
    for offset, char in enumerate(text):
        char_start = start + round(duration * offset / len(text))
        char_end = start + round(duration * (offset + 1) / len(text))
        result.append(unit_type(char, char_start, max(char_start + 1, char_end)))
    return result


def _safe_insertion(target: str, left: int, right: int) -> bool:
    return (
        bool(target)
        and len(target) <= MAX_INSERTION_CHARS
        and left >= 3
        and right >= 3
        and all(_is_spoken_char(char) for char in target)
        and not any(char in PERSPECTIVE_PRONOUNS for char in target)
    )


def _gap_for_boundary(units: Sequence[Any], index: int) -> Tuple[int, int]:
    previous_end = int(units[index - 1].end_time) if index > 0 else -1
    next_start = int(units[index].start_time) if index < len(units) else -1
    return previous_end, next_start


def _make_inserted_units(units: Sequence[Any], index: int, text: str) -> List[Any]:
    previous_end, next_start = _gap_for_boundary(units, index)
    unit_type = type(units[index] if index < len(units) else units[index - 1])
    gap = next_start - previous_end if previous_end >= 0 and next_start >= 0 else -1
    minimum_gap = max(120, 80 * len(text))
    if gap >= minimum_gap:
        per_char = min(220, max(80, (gap - 20) // max(1, len(text))))
        total = per_char * len(text)
        start = max(previous_end, next_start - total - 20)
        end = next_start
    elif index < len(units):
        # Word timestamps are often contiguous. Borrow the beginning of the
        # following character's span instead of dropping an aligned script
        # character merely because ASR gave it no independent timestamp.
        following = units[index]
        start = int(following.start_time)
        end = int(following.end_time)
        if end <= start:
            return []
        borrowed_end = start + round((end - start) * len(text) / (len(text) + 1))
        following.start_time = max(start + 1, borrowed_end)
        end = max(start + len(text), borrowed_end)
        per_char = max(1, (end - start) // len(text))
    elif index > 0:
        previous = units[index - 1]
        original_start = int(previous.start_time)
        end = int(previous.end_time)
        if end <= original_start:
            return []
        start = original_start + round((end - original_start) / (len(text) + 1))
        previous.end_time = max(original_start + 1, start)
        per_char = max(1, (end - start) // len(text))
    else:
        return []

    result = []
    for offset, char in enumerate(text):
        char_start = start + per_char * offset
        char_end = min(end, max(char_start + 1, char_start + per_char))
        result.append(unit_type(char, char_start, char_end))
    return result


def reconcile_segments_with_script(
    segments: Sequence[Any], script_reference: str
) -> Tuple[List[Any], Dict[str, Any]]:
    """Conservatively correct short ASR errors before semantic splitting."""
    units = _expand_segments(segments)
    script_text = _spoken_chars(extract_script_dialogue(script_reference))
    canonical_speakers = extract_script_speakers(script_reference)
    asr_text = "".join(unit.text for unit in units)
    report: Dict[str, Any] = {
        "ratio": 0.0,
        "authoritative": False,
        "replacements": [],
        "deletions": [],
        "insertions": [],
        "pause_protected": [],
        "pause_island_replacements": [],
        "applied": False,
    }
    if not units or not script_text or not asr_text:
        return list(segments), report
    cjk_count = sum("\u4e00" <= char <= "\u9fff" for char in script_text)
    if cjk_count / max(1, len(script_text)) < 0.5:
        return list(segments), report

    matcher = difflib.SequenceMatcher(None, asr_text, script_text, autojunk=False)
    report["ratio"] = matcher.ratio()
    if report["ratio"] < MIN_EPISODE_MATCH_RATIO:
        return list(segments), report
    authoritative = report["ratio"] >= AUTHORITATIVE_MATCH_RATIO
    report["authoritative"] = authoritative

    opcodes = matcher.get_opcodes()
    insertions: Dict[int, List[Any]] = {}
    changed = False
    consumed_opcodes = set()

    if authoritative:
        for (
            opcode_start,
            opcode_end,
            i1,
            i2,
            j1,
            j2,
            left_anchor,
            right_anchor,
        ) in _merged_edit_spans(opcodes):
            source = asr_text[i1:i2]
            target = script_text[j1:j2]
            # SequenceMatcher may bridge a tiny equal substring across a long
            # pause and accidentally consume two speaking turns as one edit.
            # Process those edit islands separately for every episode.
            if _span_crosses_real_pause(units, i1, i2):
                report["pause_protected"].append((source, target))
                continue
            if not _authoritative_replacement(
                source, target, left_anchor, right_anchor
            ):
                continue
            if len(source) == len(target):
                for offset, char in enumerate(target):
                    units[i1 + offset].text = char
            else:
                replacement = _make_replacement_units(units, i1, i2, target)
                if not replacement:
                    continue
                for unit in units[i1:i2]:
                    unit.text = ""
                insertions.setdefault(i1, []).extend(replacement)
            consumed_opcodes.update(range(opcode_start, opcode_end + 1))
            report["replacements"].append((source, target))
            changed = True

    for opcode_index, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal" or opcode_index in consumed_opcodes:
            continue
        source = asr_text[i1:i2]
        target = script_text[j1:j2]
        left_anchor, right_anchor = _anchor_sizes(opcodes, opcode_index)

        pause_island_replacement = authoritative and _pause_isolated_safe_replacement(
            units,
            i1,
            i2,
            source,
            target,
            left_anchor,
            right_anchor,
            canonical_speakers,
        )
        strong_replacement = authoritative and (
            _authoritative_replacement(source, target, left_anchor, right_anchor)
            or pause_island_replacement
        )
        conservative_replacement = _safe_replacement(
            source, target, left_anchor, right_anchor, canonical_speakers
        )
        if tag == "replace" and (strong_replacement or conservative_replacement):
            # A filler recognized at the end of one scene is often a missing
            # vocative in the next.  If a real pause follows, move the corrected
            # token to the end of that pause instead of displaying it early.
            if (
                all(char in FILLER_CHARS for char in source)
                and i2 < len(units)
                and int(units[i2].start_time) - int(units[i2 - 1].end_time) >= 300
            ):
                shifted = _make_inserted_units(units, i2, target)
                if shifted:
                    for unit in units[i1:i2]:
                        unit.text = ""
                    insertions.setdefault(i2, []).extend(shifted)
                elif len(source) == len(target):
                    for offset, char in enumerate(target):
                        units[i1 + offset].text = char
                else:
                    replacement = _make_replacement_units(units, i1, i2, target)
                    if not replacement:
                        continue
                    for unit in units[i1:i2]:
                        unit.text = ""
                    insertions.setdefault(i1, []).extend(replacement)
            elif len(source) == len(target):
                for offset, char in enumerate(target):
                    units[i1 + offset].text = char
            else:
                replacement = _make_replacement_units(units, i1, i2, target)
                if not replacement:
                    continue
                for unit in units[i1:i2]:
                    unit.text = ""
                insertions.setdefault(i1, []).extend(replacement)
            report["replacements"].append((source, target))
            if pause_island_replacement:
                report["pause_island_replacements"].append((source, target))
            changed = True
            continue

        if (
            tag == "delete"
            and authoritative
            and len(source) <= MAX_AUTHORITATIVE_REPLACEMENT_CHARS
            and _strongly_anchored(left_anchor, right_anchor)
            and all(char in FILLER_CHARS for char in source)
        ):
            for unit in units[i1:i2]:
                unit.text = ""
            report["deletions"].append(source)
            changed = True
            continue

        strong_insertion = (
            authoritative
            and len(target) <= 6
            and _strongly_anchored(left_anchor, right_anchor)
            and all(_is_spoken_char(char) for char in target)
        )
        if tag == "insert" and (
            strong_insertion or _safe_insertion(target, left_anchor, right_anchor)
        ):
            inserted = _make_inserted_units(units, i1, target)
            if inserted:
                insertions.setdefault(i1, []).extend(inserted)
                report["insertions"].append(target)
                changed = True

    reconciled: List[Any] = []
    for index, unit in enumerate(units):
        reconciled.extend(insertions.get(index, []))
        if unit.text:
            reconciled.append(unit)
    reconciled.extend(insertions.get(len(units), []))
    report["applied"] = changed
    return reconciled, report


def repair_script_confirmed_numeric_overlap(
    segments: Sequence[Any], script_reference: str
) -> int:
    """Remove adjacent-caption numeral duplication only with script proof."""
    script_text = _spoken_chars(extract_script_dialogue(script_reference))
    if not script_text:
        return 0

    repaired = 0
    for index in range(1, len(segments)):
        previous = segments[index - 1]
        current = segments[index]
        previous_text = str(getattr(previous, "text", ""))
        current_text = str(getattr(current, "text", ""))
        if not previous_text or not current_text:
            continue
        gap = int(current.start_time) - int(previous.end_time)
        if gap < MAX_ALIGNMENT_EDIT_INTERNAL_GAP_MS:
            continue

        max_overlap = min(4, len(previous_text), len(current_text))
        for size in range(max_overlap, 0, -1):
            overlap = previous_text[-size:]
            if overlap != current_text[:size]:
                continue
            if not all(char in NUMBER_CHARS for char in overlap):
                continue
            collapsed = previous_text[:-size] + current_text
            evidence = _spoken_chars(collapsed[-24:])
            if evidence and evidence in script_text:
                previous.text = previous_text[:-size]
                repaired += 1
                break
    return repaired


def script_chars_and_boundaries(script_reference: str) -> Tuple[str, List[int]]:
    """Return script dialogue plus readable hard/soft clause boundaries.

    Sentence punctuation and speaker-line changes are always boundaries.
    Commas and semicolons are accepted only when both neighbouring clauses
    remain readable, preventing mechanical two-character fragments.
    """
    chars: List[str] = []
    boundaries = set()
    for raw_line in (script_reference or "").splitlines():
        dialogue = _dialogue_only(raw_line)
        if not dialogue:
            continue
        line_start = len(chars)
        punctuation_positions = []
        for char in dialogue:
            if _is_spoken_char(char):
                chars.append(char)
            elif char in TERMINAL_PUNCTUATION | CLAUSE_PUNCTUATION and chars:
                # ``……`` and ``？！`` occupy one spoken boundary even though
                # they contain multiple punctuation code points.
                if not punctuation_positions or punctuation_positions[-1][0] != len(chars):
                    punctuation_positions.append((len(chars), char))

        line_end = len(chars)
        all_positions = [line_start] + [pos for pos, _ in punctuation_positions] + [line_end]
        for index, (position, punctuation) in enumerate(punctuation_positions, 1):
            if punctuation in TERMINAL_PUNCTUATION and punctuation != "…":
                boundaries.add(position)
                continue
            previous_position = all_positions[index - 1]
            next_position = all_positions[index + 1]
            previous_length = position - previous_position
            next_length = next_position - position
            single_vocative = (
                previous_length == 1
                and position > 0
                and chars[position - 1] in SINGLE_CHAR_VOCATIVES
            )
            minimum_left = 2 if punctuation in {"…", "、"} else 3
            if (
                (previous_length >= minimum_left or single_vocative)
                and next_length >= 2
            ):
                boundaries.add(position)
        if chars:
            boundaries.add(len(chars))
    return "".join(chars), sorted(boundaries)


def _script_chars_and_boundaries(script_reference: str) -> Tuple[str, List[int]]:
    """Backward-compatible private alias."""
    return script_chars_and_boundaries(script_reference)


def apply_script_boundary_hints(
    source: str,
    sentences: Sequence[str],
    script_reference: str,
    shift_radius: int = 2,
    replace_existing: bool = False,
) -> List[str]:
    """Shift/add sentence cuts to strongly aligned script sentence boundaries.

    Only boundaries are changed; joining the returned list always reproduces
    the exact source string.
    """
    if not source or not sentences or "".join(sentences) != source:
        return list(sentences)

    source_positions = [index for index, char in enumerate(source) if _is_spoken_char(char)]
    source_chars = "".join(source[index] for index in source_positions)
    script_chars, script_boundaries = _script_chars_and_boundaries(script_reference)
    if not source_chars or not script_chars or not script_boundaries:
        return list(sentences)

    matcher = difflib.SequenceMatcher(None, source_chars, script_chars, autojunk=False)
    if matcher.ratio() < 0.75:
        return list(sentences)

    script_to_source: Dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        if block.size < 2:
            continue
        for offset in range(block.size):
            script_to_source[block.b + offset] = block.a + offset

    strong_cuts = set()
    for boundary in script_boundaries:
        mapped_left = script_to_source.get(boundary - 1)
        mapped_right = script_to_source.get(boundary)
        if mapped_left is None and mapped_right is None:
            continue
        # When ASR contains a spoken suffix that is absent from the script,
        # anchor the cut to the first matched character on the right.  This
        # keeps the insertion with the spoken phrase: script ``事，以后`` and
        # ASR ``事儿以后`` must become ``事儿 / 以后``, never ``事 / 儿以后``.
        if mapped_right is not None and (
            mapped_left is None or mapped_right >= mapped_left + 1
        ):
            normalized_cut = mapped_right
        else:
            normalized_cut = mapped_left + 1
        if 0 < normalized_cut < len(source_positions):
            strong_cuts.add(source_positions[normalized_cut - 1] + 1)

    if not strong_cuts:
        return list(sentences)

    if replace_existing:
        # A strongly matched episode script is the authoritative source of
        # sentence/speaker boundaries.  Rebuilding the cut set removes valid-
        # length but linguistically broken LLM cuts such as ``心 / 高气傲``.
        # Long script clauses remain intact here so the timestamp-aware splitter
        # can choose an acoustic/semantic cut instead of chopping fixed widths.
        shifted_cuts = set(strong_cuts)
    else:
        existing_cuts = []
        cursor = 0
        for sentence in sentences[:-1]:
            cursor += len(sentence)
            existing_cuts.append(cursor)

        shifted_cuts = set()
        for cut in existing_cuts:
            nearby = [
                strong for strong in strong_cuts if abs(strong - cut) <= shift_radius
            ]
            shifted_cuts.add(
                min(nearby, key=lambda value: abs(value - cut)) if nearby else cut
            )
        shifted_cuts.update(strong_cuts)

    result = []
    start = 0
    for cut in sorted(cut for cut in shifted_cuts if 0 < cut < len(source)):
        chunk = source[start:cut]
        if chunk:
            result.append(chunk)
        start = cut
    if start < len(source):
        result.append(source[start:])
    return result or list(sentences)


def compact_script_guidance(script_reference: str, limit: int = 4000) -> str:
    """Return a bounded per-episode reference for the splitter prompt."""
    text = "\n".join(
        line.strip() for line in (script_reference or "").splitlines() if line.strip()
    )
    return text[:limit]
