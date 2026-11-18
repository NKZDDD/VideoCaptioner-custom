"""Conservative presentation fixes; no generated words or inferred word times."""
import copy
import re


def improve_latin_readability(data, style=''):
    report = {'merges': [], 'display_holds': [], 'short_cues': []}
    def fits(text):
        from app.core.single_language_layout import wrapped
        return wrapped(text,style).count(r'\N') <= 1
    cues = []
    for original in data.segments:
        cue = copy.deepcopy(original)
        if cues:
            prior = cues[-1]
            speaker = getattr(prior, 'script_speaker', '')
            same_row = (getattr(prior, 'script_row_id', None) is not None
                        and getattr(prior, 'script_row_id', None) == getattr(cue, 'script_row_id', None))
            same_speaker = speaker and speaker == getattr(cue, 'script_speaker', '')
            same_voice = (getattr(prior, 'voice_source_id', None) is not None
                          and getattr(prior, 'voice_source_id', None) == getattr(cue, 'voice_source_id', None))
            joined = prior.text.rstrip() + ' ' + cue.text.lstrip()
            short = min(prior.end_time-prior.start_time, cue.end_time-cue.start_time) < 600
            if (short and (same_row or same_speaker or same_voice)
                    and 0 <= cue.start_time-prior.end_time <= 250
                    and not re.search(r'[.!?][\"\u201d\u2019]*$', prior.text.rstrip())
                    and len(joined) <= 64 and len(joined.split()) <= 14
                    and fits(joined)
                    and cue.end_time-prior.start_time <= 6000
                    and not getattr(prior, 'translated_text', '')
                    and not getattr(cue, 'translated_text', '')):
                report['merges'].append({'start_ms': prior.start_time, 'end_ms': cue.end_time,
                                         'parts': [prior.text, cue.text]})
                prior.text = joined
                prior.end_time = cue.end_time
                continue
        cues.append(cue)
    # Display holds are bounded by neighbouring recognized speech and source end.
    # Keep the untouched source times in the report; never call these word times.
    native = [(c.start_time, c.end_time) for c in cues]
    source_end = getattr(data, 'source_duration_ms', native[-1][1] if native else 0)
    for i, cue in enumerate(cues):
        a, b = native[i]
        deficit = max(0, 600-(b-a))
        right = native[i+1][0] if i+1 < len(native) else source_end
        cue.end_time += min(deficit, 250, max(0, right-b))
        deficit = max(0, 600-(cue.end_time-a))
        left = cues[i-1].end_time if i else 0
        cue.start_time -= min(deficit, 150, max(0, a-left))
        if (a,b) != (cue.start_time,cue.end_time):
            report['display_holds'].append({'native_start_ms': a, 'native_end_ms': b,
                'start_ms': cue.start_time, 'end_ms': cue.end_time, 'text': cue.text})
        if cue.end_time-cue.start_time < 500:
            report['short_cues'].append({'start_ms': cue.start_time, 'end_ms': cue.end_time, 'text': cue.text})
    data.segments = cues
    return data, report
