# -*- coding: utf-8 -*-
"""Portable storage for per-video script references."""

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Dict, Optional
from xml.etree import ElementTree

from app.config import APPDATA_PATH
from app.core.concurrency import atomic_text, locked_store


STORE_DIR = APPDATA_PATH / "script_references"
INDEX_PATH = STORE_DIR / "index.json"
LIBRARY_PATH = STORE_DIR / "active_script.txt"
LIBRARY_META_PATH = STORE_DIR / "active_script.json"
SCRIPT_SUFFIXES = {".txt", ".docx", ".srt", ".json"}


def _positive_int(value) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _chinese_number_to_int(text: str) -> Optional[int]:
    digits = {
        "零": 0,
        "〇": 0,
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000}
    if not text or any(char not in digits and char not in units for char in text):
        return None
    if not any(char in units for char in text):
        try:
            return _positive_int("".join(str(digits[char]) for char in text))
        except ValueError:
            return None

    total = 0
    current = 0
    for char in text:
        if char in digits:
            current = digits[char]
        else:
            total += (current or 1) * units[char]
            current = 0
    return _positive_int(total + current)


def _episode_from_value(value) -> Optional[int]:
    number = _positive_int(value)
    if number is not None:
        return number
    text = str(value or "").strip()
    match = re.search(r"(?i)(?:EP?|第)\s*0*(\d+)", text)
    if match:
        return _positive_int(match.group(1))
    chinese_match = re.search(r"第?([零〇一二两三四五六七八九十百千]+)(?:集)?", text)
    if chinese_match:
        return _chinese_number_to_int(chinese_match.group(1))
    return None


def extract_episode_number(file_path: str) -> Optional[int]:
    """Read an episode number from extracted JSON metadata or a filename.

    Supported examples include ``第4集``, ``EP04``, ``E004`` and ``004``.
    JSON exported by the dialogue-extractor uses ``current_episode`` and takes
    priority over the source/original episode number.
    """
    path = Path(file_path)
    if path.suffix.lower() == ".json" and path.is_file():
        data = _read_json(path, {})
        if isinstance(data, dict):
            for key in ("current_episode", "episode_number", "episode"):
                number = _episode_from_value(data.get(key))
                if number is not None:
                    return number
            for key in ("current_episode_id", "episode_id"):
                number = _episode_from_value(data.get(key))
                if number is not None:
                    return number

    stem = path.stem.strip()
    patterns = (
        r"第\s*0*(\d+)\s*集",
        r"(?i)(?:^|[^A-Z0-9])EP0*(\d+)(?:$|[^0-9])",
        r"(?i)(?:^|[^A-Z0-9])E0*(\d+)(?:$|[^0-9])",
    )
    for pattern in patterns:
        match = re.search(pattern, stem)
        if match:
            return _positive_int(match.group(1))
    chinese_match = re.search(r"第([零〇一二两三四五六七八九十百千]+)集", stem)
    if chinese_match:
        return _chinese_number_to_int(chinese_match.group(1))
    if re.fullmatch(r"0*\d+", stem):
        return _positive_int(stem)
    if re.fullmatch(r"[零〇一二两三四五六七八九十百千]+", stem):
        return _chinese_number_to_int(stem)
    # Common delivery names: 指挥官回归1.mp4 / 剧名_02.mp4.
    # Require a Chinese title and a short terminal numeric group so dates,
    # MASTER timestamps and English version numbers are not treated as episodes.
    title_number = re.search(r"(?<=[\u4e00-\u9fff])\s*[_\- ]?\s*([0-9]{1,3})$", stem)
    if title_number:
        return _positive_int(title_number.group(1))
    return None


def is_script_file(file_path: str) -> bool:
    return Path(file_path).is_file() and Path(file_path).suffix.lower() in SCRIPT_SUFFIXES


def _script_rank(path: Path) -> int:
    """Prefer the extractor's current-episode artifacts over archives."""
    parts = {part.lower() for part in path.parts}
    rank = 0
    if "current_episodes" in parts:
        rank += 100
    if path.suffix.lower() == ".json":
        rank += 20
    if re.fullmatch(r"(?i)EP0*\d+", path.stem):
        rank += 10
    return rank


def _video_rank(path: Path) -> int:
    """Prefer source videos when an output copy is in the same folder."""
    name = path.name.lower()
    rank = 0
    if not name.startswith(("【卡卡】", "卡卡", "output", "export")):
        rank += 100
    return rank


def _unique_episode_files(paths, ranker=None):
    grouped = {}
    for path in paths:
        episode = extract_episode_number(str(path))
        if episode is not None:
            grouped.setdefault(episode, []).append(path)

    chosen = {}
    ambiguous = {}
    for episode, candidates in grouped.items():
        if ranker:
            best_rank = max(ranker(path) for path in candidates)
            candidates = [path for path in candidates if ranker(path) == best_rank]
        if len(candidates) == 1:
            chosen[episode] = candidates[0]
        else:
            ambiguous[episode] = [str(path) for path in candidates]
    return chosen, ambiguous


def _collect_input_files(inputs, allowed_suffixes):
    """Expand any mixture of files and folders into a unique file list."""
    collected = {}
    for raw_path in inputs:
        path = Path(str(raw_path).strip())
        if path.is_file() and path.suffix.lower() in allowed_suffixes:
            collected[str(path.resolve()).lower()] = path
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.suffix.lower() in allowed_suffixes and child.is_file():
                    collected[str(child.resolve()).lower()] = child
    return list(collected.values())


def batch_bind_script_inputs(script_inputs, video_inputs, video_suffixes, progress=None) -> Dict:
    """Bind mixed script/video files and folders by episode number."""
    if not script_inputs:
        raise FileNotFoundError("请添加一个或多个分集 JSON/剧本文件或文件夹")
    if not video_inputs:
        raise FileNotFoundError("请添加一个或多个视频文件或文件夹")
    normalized_video_suffixes = {
        suffix.lower() if str(suffix).startswith(".") else f".{str(suffix).lower()}"
        for suffix in video_suffixes
    }
    if progress:progress('正在扫描剧本和视频…')
    script_files = _collect_input_files(script_inputs, SCRIPT_SUFFIXES)
    video_files = _collect_input_files(video_inputs, normalized_video_suffixes)
    if not script_files:
        raise FileNotFoundError("所选内容中没有可识别的分集 JSON/剧本文件")
    if not video_files:
        raise FileNotFoundError("所选内容中没有可识别的视频文件")
    scripts, ambiguous_scripts = _unique_episode_files(script_files, _script_rank)
    videos, ambiguous_videos = _unique_episode_files(video_files, _video_rank)

    matched = []
    failed = []
    common_episodes = sorted(set(scripts) & set(videos))
    pairs = [(episode, scripts[episode], videos[episode], "episode") for episode in common_episodes]
    # A sole explicitly selected script and video can represent an unnumbered
    # standalone film. Never fabricate an episode number from archival E001 IDs.
    if not pairs and len(script_files) == 1 and len(video_files) == 1:
        script_path, video_path = script_files[0], video_files[0]
        script_ep = extract_episode_number(str(script_path))
        video_ep = extract_episode_number(str(video_path))
        data = _read_json(script_path, {}) if script_path.suffix.lower() == ".json" else {}
        episodes = data.get("episodes", []) if isinstance(data, dict) else []
        whole_series = isinstance(episodes, list) and len(episodes) > 1
        if (script_ep is None or video_ep is None) and not whole_series:
            pairs.append((script_ep or video_ep, script_path, video_path, "single"))
    pending=[]
    for number,(episode, script_path, video_path, binding_mode) in enumerate(pairs,1):
        info=dict(episode=episode,script_path=str(script_path),video_path=str(video_path),binding_mode=binding_mode)
        try:
            if progress:progress(f'正在读取第 {number}/{len(pairs)} 集剧本…')
            reference_text=load_script_file(str(script_path))
            prepared=_prepare_binding(str(video_path),reference_text,str(script_path))
            pending.append((info,prepared))
        except Exception as exc:failed.append(dict(info,error=str(exc)))
    if pending:
        try:
            if progress:progress(f'正在统一保存 {len(pending)} 集绑定…')
            saved,errors,bound_count=_commit_bindings([item for info,item in pending],progress)
            for number,(info,item) in enumerate(pending):
                if number in errors:failed.append(dict(info,error=errors[number]))
                else:matched.append(info)
        except Exception as exc:
            failed.extend(dict(info,error=str(exc)) for info,item in pending)
            bound_count=None
    else:bound_count=None

    handled_scripts = {row[1] for row in pairs}
    handled_videos = {row[2] for row in pairs}
    return {
        "unrecognized_script_files": [str(path) for path in script_files if path not in handled_scripts and extract_episode_number(str(path)) is None],
        "unrecognized_video_files": [str(path) for path in video_files if path not in handled_videos and extract_episode_number(str(path)) is None],
        "bound_count": bound_count,
        "matched": matched,
        "failed": failed,
        "unmatched_script_episodes": sorted(set(scripts) - set(videos)),
        "unmatched_video_episodes": sorted(set(videos) - set(scripts)),
        "ambiguous_script_episodes": ambiguous_scripts,
        "ambiguous_video_episodes": ambiguous_videos,
        "script_file_count": len(script_files),
        "video_file_count": len(video_files),
    }


def batch_bind_script_folder(
    script_folder: str, video_folder: str, video_suffixes
) -> Dict:
    """Backward-compatible folder-only entry point."""
    return batch_bind_script_inputs(
        [script_folder], [video_folder], video_suffixes
    )


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def _video_key(video_path: str) -> str:
    normalized = str(Path(video_path).expanduser().resolve()).lower()
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:20]


def _stored_video_matches(entry, identity):
    """Do not let an inaccessible historical path block a current binding."""
    if not isinstance(entry, dict):
        return False
    if entry.get('resource_identity') == identity:
        return True
    path = entry.get('video_path')
    if not path:
        return False
    from app.core.concurrency import resource_key
    try:
        return resource_key(path) == identity
    except OSError:
        # Keep the old record unchanged; this path cannot establish an alias.
        return False


def exact_video_entry(video_path):
    """Resolve only the same physical resource; filenames alone are never evidence."""
    if not video_path:return {}
    from app.core.concurrency import resource_key
    index=_read_json(INDEX_PATH,{})
    direct=index.get(_video_key(video_path),{})
    identity=resource_key(video_path);name=Path(video_path).name.casefold();matches=[direct] if direct else []
    for entry in index.values():
        if entry is direct:continue
        if not isinstance(entry,dict) or str(entry.get('video_name','')).casefold()!=name:continue
        stored=entry.get('resource_identity')
        if _stored_video_matches(entry, identity):matches.append(entry)
    variants={json.dumps([_load_entry_reference(e),e.get('bilingual_reference')],ensure_ascii=False,sort_keys=True) for e in matches}
    if len(variants)>1:raise ValueError('同一视频通过不同路径绑定了不同剧本，请重新绑定；未随意选择')
    return matches[0] if matches else {}


def _prepare_binding(video_path,reference_text,source_script=''):
    if not video_path or not Path(video_path).is_file():raise FileNotFoundError('请选择存在的原视频文件')
    reference_text=reference_text.strip()
    if not reference_text:raise ValueError('本次视频的剧本台词不能为空')
    from app.core.utils.bilingual_reference import reference_from_json
    from app.core.concurrency import resource_key
    bilingual=None
    if source_script and Path(source_script).suffix.lower()=='.json':
        bilingual=reference_from_json(_read_json(Path(source_script),{}))
        if bilingual and reference_text!=load_script_file(source_script).strip():raise ValueError('英文剧本已被手动修改，请同时更新双语参考后重新绑定')
    key=_video_key(video_path)
    entry={'resource_identity':resource_key(video_path),'video_path':str(Path(video_path).resolve()),'video_name':Path(video_path).name,'video_stem':Path(video_path).stem,'episode_number':extract_episode_number(video_path),'source_script':source_script,'source_episode_number':extract_episode_number(source_script) if source_script else None}
    if bilingual:entry['bilingual_reference']=bilingual
    return key,entry,reference_text


@locked_store
def _commit_bindings(prepared,progress=None):
    # Re-read under the shared lock, so another PC's completed saves are retained.
    import uuid
    from app.core.concurrency import resource_key
    STORE_DIR.mkdir(parents=True,exist_ok=True)
    index=json.loads(INDEX_PATH.read_text(encoding='utf-8-sig')) if INDEX_PATH.exists() else {}
    if not isinstance(index,dict):raise ValueError('绑定索引格式无效，未覆盖已有绑定')
    names={entry['video_name'].casefold() for key,entry,text in prepared};targets={entry['resource_identity'] for key,entry,text in prepared};aliases={};identities={}
    for old_key,old in index.items():
        if not isinstance(old,dict) or str(old.get('video_name','')).casefold() not in names:continue
        identity=old.get('resource_identity')
        # A stored identity is sufficient; consult historical paths only when missing.
        if identity not in targets and old.get('video_path'):
            path=old['video_path']
            try:
                if path not in identities:identities[path]=resource_key(path)
                identity=identities[path]
            except OSError:continue
        if identity:aliases.setdefault(identity,[]).append(old_key)
    saved={};errors={};created=[];seen={}
    try:
        for number,(key,entry,text) in enumerate(prepared):
            signature=json.dumps([text,entry.get('bilingual_reference')],ensure_ascii=False,sort_keys=True)
            identity=entry['resource_identity']
            if identity in seen and seen[identity]!=signature:raise ValueError('同一视频存在不同剧本，未提交本批绑定')
            seen[identity]=signature
            path=STORE_DIR/(key+'.'+uuid.uuid4().hex+'.txt')
            try:atomic_text(path,text)
            except Exception as exc:errors[number]=str(exc);continue
            created.append(path);entry=dict(entry,reference_path=str(path))
            for alias in aliases.get(identity,[]):index[alias]=entry
            index[key]=entry;saved[number]=entry
            if progress:progress(f'正在保存绑定 {number+1}/{len(prepared)}…')
        if saved:_write_json(INDEX_PATH,index)
        return saved,errors,len(index)
    except Exception:
        # New immutable reference files are invisible until the index commits.
        for path in created:
            try:path.unlink(missing_ok=True)
            except OSError:pass
        raise


def save_video_script_reference(video_path,reference_text,source_script='') -> Dict:
    prepared=_prepare_binding(video_path,reference_text,source_script)
    saved,errors,count=_commit_bindings([prepared])
    if errors:raise OSError(errors[0])
    return dict(saved[0],bound_count=count)


def _load_entry_reference(entry: Dict, fallback_key: str = "") -> str:
    """Read a stored reference after a portable folder has changed drives."""
    try:
        return Path(entry["reference_path"]).read_text(encoding="utf-8").strip()
    except (OSError, KeyError, TypeError):
        reference_name = Path(str(entry.get("reference_path", ""))).name
        fallback = STORE_DIR / (reference_name or f"{fallback_key}.txt")
        try:
            return fallback.read_text(encoding="utf-8").strip()
        except OSError:
            return ""


def _one_distinct_reference(entries) -> str:
    """Resolve duplicate bindings only when they contain identical dialogue."""
    distinct = {}
    for item in entries:
        text = _load_entry_reference(item)
        if text:
            normalized = re.sub(r"\s+", "", text)
            distinct.setdefault(hashlib.sha1(normalized.encode("utf-8")).hexdigest(), text)
    return next(iter(distinct.values())) if len(distinct) == 1 else ""


def load_exact_video_script_reference(video_path):
    if not video_path:
        return ''
    index = _read_json(INDEX_PATH, {})
    key = _video_key(video_path)
    entry = exact_video_entry(video_path)
    return _load_entry_reference(entry, key) if entry else ''


def load_video_script_reference(video_path: Optional[str]) -> str:
    if not video_path:
        return ""
    index = _read_json(INDEX_PATH, {})
    key = _video_key(video_path)
    entry = index.get(key)

    # When the portable software folder and media are copied to another PC,
    # the drive/path may change. Duplicate historical bindings are safe when
    # all copies contain the same episode dialogue.
    if not entry:
        name = Path(video_path).name.lower()
        matches = [
            item
            for item in index.values()
            if str(item.get("video_name", "")).lower() == name
        ]
        if len(matches) == 1:
            entry = matches[0]
        elif matches:
            duplicate_text = _one_distinct_reference(matches)
            if duplicate_text:
                return duplicate_text

    # A renamed video (EP09 -> 第9集) can still use the saved reference. Never
    # guess across different dramas: episode fallback succeeds only when every
    # candidate for that episode has identical dialogue.
    if not entry:
        episode = extract_episode_number(video_path)
        if episode is not None:
            matches = []
            for item in index.values():
                item_episode = _positive_int(item.get("episode_number"))
                if item_episode is None:
                    item_episode = extract_episode_number(
                        str(item.get("video_name") or item.get("video_path") or "")
                    )
                if item_episode == episode:
                    matches.append(item)
            episode_text = _one_distinct_reference(matches)
            if episode_text:
                return episode_text
    if not entry:
        return ""
    return _load_entry_reference(entry, key)


def count_video_references() -> int:
    return len(_read_json(INDEX_PATH, {}))


@locked_store
def save_active_script(text: str, source_path: str) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_text(LIBRARY_PATH, text)
    _write_json(LIBRARY_META_PATH, {"source_path": source_path})


def load_active_script() -> tuple[str, str]:
    try:
        text = LIBRARY_PATH.read_text(encoding="utf-8")
    except OSError:
        text = ""
    meta = _read_json(LIBRARY_META_PATH, {})
    return text, str(meta.get("source_path", ""))


def _read_text_with_fallback(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _read_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        xml_data = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml_data)
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    for paragraph in root.iter(namespace + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(namespace + "t"))
        if text.strip():
            paragraphs.append(text.strip())
    return "\n".join(paragraphs)


def _dialogue_rows(items) -> list[str]:
    rows = []
    for item in items:
        if isinstance(item, str):
            if item.strip():
                rows.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        speaker = str(
            item.get(
                "speaker",
                item.get("speaker_label", item.get("character", item.get("role", ""))),
            )
        ).strip()
        text = str(
            item.get(
                "text",
                item.get("dialogue", item.get("content", item.get("line", ""))),
            )
        ).strip()
        if text:
            rows.append(f"{speaker}：{text}" if speaker else text)
    return rows


def _read_script_json(path: Path) -> str:
    data = _read_json(path, {})
    if isinstance(data, dict):
        for key in ("dialogues", "lines", "script", "entries", "content"):
            value = data.get(key)
            if isinstance(value, list):
                rows = _dialogue_rows(value)
                if rows:
                    return "\n".join(rows)
            if isinstance(value, str) and value.strip():
                return value
        # Segment-based dialogue exports keep actual speech in segments[].lines.
        # Traverse only explicit dialogue containers, never arbitrary metadata
        # such as shot notes, voice_direction or production instructions.
        segments = data.get("segments")
        if isinstance(segments, list):
            rows = []
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                for key in ("dialogues", "lines", "script", "entries", "content"):
                    value = segment.get(key)
                    segment_rows = _dialogue_rows(value) if isinstance(value, list) else []
                    if segment_rows:
                        rows.extend(segment_rows)
                        break
                    if isinstance(value, str) and value.strip():
                        rows.append(value.strip())
                        break
            if rows:
                return "\n".join(rows)
    if isinstance(data, list):
        rows = _dialogue_rows(data)
        if rows:
            return "\n".join(rows)
    raise ValueError("无法识别该 JSON 剧本格式")


def load_script_file(file_path: str) -> str:
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        text = _read_docx(path)
    elif suffix == ".json":
        text = _read_script_json(path)
    else:
        text = _read_text_with_fallback(path)

    # Normalize only whitespace. The external Skill remains responsible for
    # deciding which content is dialogue and which metadata should be kept.
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def load_exact_bilingual_reference(video_path):
    if not video_path:return None
    from app.core.utils.bilingual_reference import validate_reference
    entry=exact_video_entry(video_path)
    return validate_reference(entry.get('bilingual_reference'))
