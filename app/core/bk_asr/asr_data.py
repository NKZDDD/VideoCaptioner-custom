import json
import math
import re
from pathlib import Path
from typing import List, Tuple
import os
import platform

from app.core.utils.ass_effects import effect_text_layers, normalize_capcut_style_bold
from app.core.utils.ass_auto_wrap import auto_wrap_text, parse_ass_info, is_mainly_cjk, english_safe_width


def handle_long_path(path: str) -> str:
    """处理Windows系统中的长路径问题

    Args:
        path: 原始路径

    Returns:
        处理后的路径
    """
    # 检查是否是Windows系统
    if platform.system() == "Windows":
        # 如果路径长度超过260个字符，添加\\?\前缀
        if len(path) > 260 and not path.startswith("\\\\?\\"):
            # 转换为绝对路径
            abs_path = os.path.abspath(path)
            return f"\\\\?\\{abs_path}"
    return path


class ASRDataSeg:
    def __init__(
        self, text: str, start_time: int, end_time: int, translated_text: str = ""
    ):
        self.text = text
        self.translated_text = translated_text
        self.start_time = start_time
        self.end_time = end_time

    def to_srt_ts(self) -> str:
        """Convert to SRT timestamp format"""
        return f"{self._ms_to_srt_time(self.start_time)} --> {self._ms_to_srt_time(self.end_time)}"

    def to_lrc_ts(self) -> str:
        """Convert to LRC timestamp format"""
        return f"[{self._ms_to_lrc_time(self.start_time)}]"

    def to_ass_ts(self) -> Tuple[str, str]:
        """Convert to ASS timestamp format"""
        return self._ms_to_ass_ts(self.start_time), self._ms_to_ass_ts(self.end_time)

    def _ms_to_lrc_time(self, ms: int) -> str:
        seconds = ms / 1000
        minutes, seconds = divmod(seconds, 60)
        return f"{int(minutes):02}:{seconds:.2f}"

    @staticmethod
    def _ms_to_srt_time(ms: int) -> str:
        """Convert milliseconds to SRT time format (HH:MM:SS,mmm)"""
        total_seconds, milliseconds = divmod(ms, 1000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{int(hours):02}:{int(minutes):02}:{int(seconds):02},{int(milliseconds):03}"

    @staticmethod
    def _ms_to_ass_ts(ms: int) -> str:
        """Convert milliseconds to ASS timestamp format (H:MM:SS.cc)"""
        total_seconds, milliseconds = divmod(ms, 1000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        centiseconds = int(milliseconds / 10)
        return f"{int(hours):01}:{int(minutes):02}:{int(seconds):02}.{centiseconds:02}"

    @property
    def transcript(self) -> str:
        """Return segment text"""
        return self.text

    def __str__(self) -> str:
        return f"ASRDataSeg({self.text}, {self.start_time}, {self.end_time})"


class ASRData:
    def __init__(self, segments: List[ASRDataSeg]):
        # 去除 segments.text 为空的
        filtered_segments = [seg for seg in segments if seg.text and seg.text.strip()]
        filtered_segments.sort(key=lambda x: x.start_time)
        self.segments = filtered_segments

    def __iter__(self):
        return iter(self.segments)

    def __len__(self) -> int:
        return len(self.segments)

    def has_data(self) -> bool:
        """Check if there are any utterances"""
        return len(self.segments) > 0

    def is_word_timestamp(self) -> bool:
        """
        判断是否是字级时间戳
        规则：
        1. 对于英文，每个segment应该只包含一个单词
        2. 对于中文，每个segment应该只包含一个汉字
        3. 允许20%的误差率
        """
        if not self.segments:
            return False

        valid_segments = 0
        total_segments = len(self.segments)

        for seg in self.segments:
            text = seg.text.strip()
            # 检查是否只包含一个英文单词或一个汉字
            if (len(text.split()) == 1 and text.isascii()) or len(text.strip()) <= 2:
                valid_segments += 1
        return (valid_segments / total_segments) >= 0.8

    def split_to_word_segments(self) -> "ASRData":
        """
        将当前ASRData中的每个segment按字词分割，并按音素计算时间戳
        每4个字符视为一个音素单位进行时间分配

        Returns:
            ASRData: 包含分割后字词级别segments的新ASRData实例
        """
        CHARS_PER_PHONEME = 4  # 每个音素包含的字符数
        new_segments = []

        for seg in self.segments:
            text = seg.text
            duration = seg.end_time - seg.start_time

            # 匹配所有有效字符（包括数字和各种语言）
            pattern = (
                # 以单词形式出现的语言(连续提取)
                r"[a-zA-Z\u00c0-\u00ff\u0100-\u017f']+"  # 拉丁字母及其变体(英语、德语、法语等)
                r"|[\u0400-\u04ff]+"  # 西里尔字母(俄语等)
                r"|[\u0370-\u03ff]+"  # 希腊语
                r"|[\u0600-\u06ff]+"  # 阿拉伯语
                r"|[\u0590-\u05ff]+"  # 希伯来语
                r"|\d+"  # 数字
                # 以单字形式出现的语言(单字提取)
                r"|[\u4e00-\u9fff]"  # 中文
                r"|[\u3040-\u309f]"  # 日文平假名
                r"|[\u30a0-\u30ff]"  # 日文片假名
                r"|[\uac00-\ud7af]"  # 韩文
                r"|[\u0e00-\u0e7f][\u0e30-\u0e3a\u0e47-\u0e4e]*"  # 泰文基字符及其音标组合
                r"|[\u0900-\u097f]"  # 天城文(印地语等)
                r"|[\u0980-\u09ff]"  # 孟加拉语
                r"|[\u0e80-\u0eff]"  # 老挝文
                r"|[\u1000-\u109f]"  # 缅甸文
            )
            words = re.finditer(pattern, text)
            words_list = list(words)

            if not words_list:
                continue

            # 计算总音素数
            total_phonemes = sum(
                math.ceil(len(w.group()) / CHARS_PER_PHONEME) for w in words_list
            )
            time_per_phoneme = duration / max(total_phonemes, 1)  # 防止除零

            current_time = seg.start_time
            for word_match in words_list:
                word = word_match.group()
                # 计算当前词的音素数
                word_phonemes = math.ceil(len(word) / CHARS_PER_PHONEME)
                word_duration = int(time_per_phoneme * word_phonemes)

                # 创建新的字词级segment
                word_end_time = min(current_time + word_duration, seg.end_time)
                new_segments.append(
                    ASRDataSeg(
                        text=word, start_time=current_time, end_time=word_end_time
                    )
                )

                current_time = word_end_time

        self.segments = new_segments
        return self

    def remove_punctuation(self) -> "ASRData":
        """
        移除字幕正文中的常见中英文标点符号。

        旧实现只处理行尾的中文逗号和句号，导致问号、英文逗号以及
        行内标点残留。这里统一处理整条正文，但不改变文字和时间轴。
        """
        punctuation = re.compile(
            r"[，。！？!?、；;：:,.．…—～~·“”‘’\"'（）()\[\]【】《》〈〉{}]"
        )
        # Numeric separators carry meaning. In CJK output, a decimal separator
        # is spoken as 点; never concatenate its two sides when punctuation goes.
        cjk_source=any(re.search(r'[\u3400-\u9fff]',seg.text) for seg in self.segments)
        cjk_translation=any(re.search(r'[\u3400-\u9fff]',seg.translated_text) for seg in self.segments)
        def remove(text,cjk):
            if cjk:text=re.sub(r'(?<=\d)[.．](?=\d)', '点', text)
            return punctuation.sub("",text.strip())
        for seg in self.segments:
            old=seg.text
            seg.text=remove(seg.text,cjk_source)
            seg.translated_text=remove(seg.translated_text,cjk_translation)
            # A numeric display expansion invalidates cached word-text slices,
            # not its source interval. Repage using the existing bounded route.
            if re.sub(r'[^\w]','',old)!=re.sub(r'[^\w]','',seg.text):
                for attr in ('_delivery_native_units','alignment_units'):
                    if hasattr(seg,attr):delattr(seg,attr)
        return self

    def save(
        self, save_path: str, ass_style: str = None, layout: str = "原文在上"
    ) -> None:
        """
        Save the ASRData to a file

        Args:
            save_path: 保存路径
            ass_style: ASS样式字符串,为空则使用默认样式
            layout: 字幕布局,可选值["原文在上", "译文在上", "仅原文", "仅译文"]
        """
        # 处理Windows长路径问题
        save_path = handle_long_path(save_path)

        # 创建目录
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)

        if save_path.endswith(".srt"):
            self.to_srt(save_path=save_path, layout=layout)
        elif save_path.endswith(".txt"):
            self.to_txt(save_path=save_path, layout=layout)
        elif save_path.endswith(".json"):
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(self.to_json(), f, ensure_ascii=False)
        elif save_path.endswith(".ass"):
            self.to_ass(save_path=save_path, style_str=ass_style, layout=layout)
        else:
            raise ValueError(f"Unsupported file extension: {save_path}")

    def to_txt(self, save_path=None, layout: str = "原文在上") -> str:
        """Convert to plain text subtitle format (without timestamps)"""
        result = []
        for seg in self.segments:
            # 检查是否有换行符
            original = seg.text
            translated = seg.translated_text

            # 根据字幕类型组织文本
            if layout == "原文在上":
                text = f"{original}\n{translated}" if translated else original
            elif layout == "译文在上":
                text = f"{translated}\n{original}" if translated else original
            elif layout == "仅原文":
                text = original
            elif layout == "仅译文":
                text = translated if translated else original
            else:
                text = seg.transcript
            result.append(text)
        text = "\n".join(result)
        if save_path:
            # 处理Windows长路径问题
            save_path = handle_long_path(save_path)

            with open(save_path, "w", encoding="utf-8") as f:
                f.write("\n".join(result))
        return text

    def to_srt(self, layout: str = "原文在上", save_path=None) -> str:
        """Convert to SRT subtitle format"""
        from app.core.utils.punctuation_boundaries import repair
        output,_=repair(self)
        srt_lines = []
        for n, seg in enumerate(output.segments, 1):
            # 检查是否有换行符
            original = seg.text
            translated = seg.translated_text

            # 根据字幕类型组织文本
            if layout == "原文在上":
                text = f"{original}\n{translated}" if translated else original
            elif layout == "译文在上":
                text = f"{translated}\n{original}" if translated else original
            elif layout == "仅原文":
                text = original
            elif layout == "仅译文":
                text = translated if translated else original
            else:
                text = seg.transcript

            srt_lines.append(f"{n}\n{seg.to_srt_ts()}\n{text}\n")

        srt_text = "\n".join(srt_lines)
        if save_path:
            # 处理Windows长路径问题
            save_path = handle_long_path(save_path)

            with open(save_path, "w", encoding="utf-8") as f:
                f.write(srt_text)
        return srt_text

    def to_lrc(self, save_path=None) -> str:
        """Convert to LRC subtitle format"""
        raise NotImplementedError("LRC format is not supported")

    def to_json(self) -> dict:
        result_json = {}
        for i, segment in enumerate(self.segments, 1):
            # 检查是否有换行符
            original = segment.text
            translated = segment.translated_text

            result_json[str(i)] = {
                "start_time": segment.start_time,
                "end_time": segment.end_time,
                "original_subtitle": original,
                "translated_subtitle": translated,
            }
            for key in ('asr_segment_id','asr_origin','speaker_id'):
                value=getattr(segment,key,None)
                if isinstance(value,(str,int)) and not isinstance(value,bool):result_json[str(i)][key]=value
        return result_json

    def to_ass(
        self, style_str: str = None, layout: str = "原文在上", save_path: str = None
    ) -> str:
        """转换为ASS字幕格式

        Args:
            style_str: ASS样式字符串,为空则使用默认样式
            layout: 字幕布局,可选值["译文在上", "原文在上", "仅原文", "仅译文"]

        Returns:
            ASS格式字幕内容
        """
        if not style_str:
            style_str = (
                "[V4+ Styles]\n"
                "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,"
                "Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,"
                "Alignment,MarginL,MarginR,MarginV,Encoding\n"
                "Style: Default,MicrosoftYaHei-Bold,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,"
                "0,0,1,2,0,2,10,10,15,1\n"
                "Style: Secondary,MicrosoftYaHei-Bold,30,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,"
                "0,0,1,2,0,2,10,10,15,1"
            )

        # Also correct style files saved by older patch versions.  This keeps
        # the user's presets untouched while making every new ASS/video use the
        # V9 Jianying-calibrated supersampled translucent outline.
        style_str = normalize_capcut_style_bold(style_str)
        from app.core.utils.delivery_policy import read_policy
        delivery_policy=read_policy(style_str)
        temporary_delivery='; VideoCaptionerTemporaryDelivery: 1' in style_str.splitlines()
        if delivery_policy and layout in ('原文在上','译文在上'):
            # Default is the upper language in both bilingual output branches.
            style_lines=style_str.splitlines();secondary=None
            for row in style_lines:
                if re.match(r'^Style:\s*Secondary,',row):secondary=row.split(',')
            if secondary and len(secondary)>21:
                for index,row in enumerate(style_lines):
                    if re.match(r'^Style:\s*Default,',row):
                        fields=row.split(',')
                        fields[21]=str(round(float(secondary[21])+float(secondary[2])*1.35+4))
                        style_lines[index]=','.join(fields)
            style_str='\n'.join(style_lines)


        # Explicit per-language anchors opt out of the legacy inferred gap.
        positions={}
        for line in style_str.splitlines():
            if line.startswith('; VideoCaptionerPosition:'):
                match=re.fullmatch(r'; VideoCaptionerPosition: (Default|Secondary),margin=(\d+)',line)
                if not match or int(match.group(2))>10000:raise ValueError('独立字幕位置格式无效')
                role=match.group(1)
                if role in positions:raise ValueError('独立字幕位置重复')
                positions[role]=match.group(2)
        if positions:
            rows=style_str.splitlines()
            for index,line in enumerate(rows):
                if line.startswith('Style: '):
                    fields=line.split(',');role=fields[0].split(':',1)[1].strip()
                    if role in positions:fields[21]=positions[role];rows[index]=','.join(fields)
            style_str='\n'.join(rows)

        # The Jianying conversion (size * 46/14 and MarginV=217) is measured
        # in 1280x720 ASS units. CalibrationCanvas describes the preview image,
        # not ASS units. Only an explicit ScriptCanvas may override them.
        canvas = re.search(r'^; VideoCaptionerScriptCanvas:\s*(\d+),\s*(\d+)\s*$', style_str, re.MULTILINE)
        play_res_x, play_res_y = (int(canvas.group(1)), int(canvas.group(2))) if canvas else (1280, 720)
        if play_res_x <= 0 or play_res_y <= 0:
            raise ValueError('字幕模板校准画布尺寸无效')
        from app.core.utils.line_geometry import parse as parse_line_geometry, fragments as geometry_fragments
        line_geometry = parse_line_geometry(style_str, play_res_y)

        ass_content = (
            "[Script Info]\n"
            "; Script generated by VideoCaptioner\n"
            f"; VideoCaptionerLayout: {layout}\n"
            "; https://github.com/weifeng2333\n"
            "ScriptType: v4.00+\n"
            "WrapStyle: 2\n"
            f"PlayResX: {play_res_x}\n"
            f"PlayResY: {play_res_y}\n\n"
            f"{style_str}\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

        dialogue_template = "Dialogue: {},{},{},{},,0,0,0,,{}\n"
        _, wrap_font_sizes = parse_ass_info(ass_content)
        line_limit_match = re.search(r'^; VideoCaptionerEnglishLineWords:\s*(\d+)', style_str, re.MULTILINE)
        line_words = int(line_limit_match.group(1)) if line_limit_match else 6

        def add_dialogue(start_time, end_time, style_name, text):
            nonlocal ass_content
            explicit_geometry_lines = style_name in line_geometry and ('\n' in text or r'\N' in text)
            if explicit_geometry_lines:
                text = text.replace('\n', r'\N')
            if not explicit_geometry_lines and not delivery_policy and not is_mainly_cjk(re.sub(r"\{[^}]*\}", "", text)):
                text = auto_wrap_text(text, english_safe_width(style_str, play_res_x), wrap_font_sizes.get(style_name, 40), line_words,
                    '; VideoCaptionerEnglishSemanticWrap: 1' in style_str,
                    '; VideoCaptionerEnglishNoSqueeze: 1' in style_str or
                    '; VideoCaptionerOriginalLanguage: hi' in style_str or
                    '; VideoCaptionerOriginalLanguage: ar' in style_str)
            if delivery_policy:text=text.replace('\n',r'\N')
            if delivery_policy and delivery_policy.get('delivery_separate_languages')==1:
                # Pin every effect layer to the same anchor; prevent collision stacking.
                row=next(r.split(',') for r in style_str.splitlines() if r.startswith('Style: '+style_name+','))
                anchor_y=play_res_y-float(row[21])
                if temporary_delivery and style_name=='Default':
                    lower=seg.translated_text if layout=='原文在上' else seg.text
                    lower_lines=max(1,len(lower.replace(r'\N','\n').splitlines()))
                    lower_row=next(r.split(',') for r in style_str.splitlines() if r.startswith('Style: Secondary,'))
                    anchor_y-=max(0,lower_lines-1)*float(lower_row[2])*1.2
                if style_name=='Secondary' and is_mainly_cjk(text):
                    # Keep the confirmed Chinese line inside the 87 px horizontal safe margins.
                    clean=re.sub(r"\{[^}]*\}", "", text)
                    available=(1080-174)*play_res_y/1920
                    display_chars=max((len(line) for line in clean.replace(r'\N','\n').splitlines()),default=1) if temporary_delivery else len(clean)
                    size=min(float(row[2]),available/max(1,display_chars))
                    if delivery_policy.get('delivery_keep_font_size')==1:
                        if size<float(row[2])-0.01:raise ValueError('保持字号：译文显示宽度超限，请使用原生词时间拆条后重新翻译；未缩小字号')
                        size=float(row[2])
                    text=r"{\fs"+str(round(size,2))+"}"+text
                text=r"{\an2\pos("+str(play_res_x/2)+","+str(anchor_y)+")}"+text
            if style_name in line_geometry:
                for index, count, fragment in geometry_fragments(text, line_geometry[style_name], play_res_x):
                    for layer, effect_text in effect_text_layers(style_str, style_name, fragment):
                        event = dialogue_template.format(layer, start_time, end_time, style_name, effect_text)
                        fields = event.split(',', 9)
                        fields[8] = f'VCLineV1:{index}:{count}'
                        ass_content += ','.join(fields)
            else:
                for layer, effect_text in effect_text_layers(style_str, style_name, text):
                    ass_content += dialogue_template.format(layer, start_time, end_time, style_name, effect_text)

        from app.core.utils.punctuation_boundaries import repair
        output,_=repair(self)
        for seg in output.segments:
            start_time, end_time = seg.to_ass_ts()
            original = seg.text
            translated = seg.translated_text
            if delivery_policy and layout in ('原文在上','译文在上') and delivery_policy.get('delivery_separate_languages')==1:
                if len(original.replace(r'\N','\n').splitlines())>delivery_policy.get('delivery_original_max_lines',2) or len(translated.replace(r'\N','\n').splitlines())>delivery_policy.get('delivery_translation_max_lines',1):
                    raise ValueError('字幕导出行数与客户模板不一致，请拆成多条字幕；未增加显示行数')

            # 检查是否有译文
            has_translation = bool(translated and translated.strip())

            if layout == "译文在上":
                if has_translation:
                    add_dialogue(start_time, end_time, "Secondary", original)
                    add_dialogue(start_time, end_time, "Default", translated)
                else:
                    add_dialogue(start_time, end_time, "Default", original)
            elif layout == "原文在上":
                if has_translation:
                    add_dialogue(start_time, end_time, "Secondary", translated)
                    add_dialogue(start_time, end_time, "Default", original)
                else:
                    add_dialogue(start_time, end_time, "Default", original)
            elif layout == "仅原文":
                add_dialogue(start_time, end_time, "Default", original)
            elif layout == "仅译文":
                text = translated if has_translation else original
                add_dialogue(start_time, end_time, "Default", text)

        if save_path:
            # 处理Windows长路径问题
            save_path = handle_long_path(save_path)

            with open(save_path, "w", encoding="utf-8") as f:
                f.write(ass_content)
        return ass_content

    def to_vtt(self, save_path=None) -> str:
        """转换为WebVTT字幕格式

        Args:
            save_path: 可选的保存路径

        Returns:
            str: WebVTT格式的字幕内容
        """
        raise NotImplementedError("WebVTT format is not supported")
        # # WebVTT头部
        # vtt_lines = ["WEBVTT\n"]

        # for n, seg in enumerate(self.segments, 1):
        #     # 转换时间戳格式从毫秒到 HH:MM:SS.mmm
        #     start_time = seg._ms_to_srt_time(seg.start_time).replace(",", ".")
        #     end_time = seg._ms_to_srt_time(seg.end_time).replace(",", ".")

        #     # 添加序号（可选）和时间戳
        #     vtt_lines.append(f"{n}\n{start_time} --> {end_time}\n{seg.transcript}\n")

        # vtt_text = "\n".join(vtt_lines)

        # if save_path:
        #     with open(save_path, "w", encoding="utf-8") as f:
        #         f.write(vtt_text)

        # return vtt_text

    def merge_segments(self, start_index: int, end_index: int, merged_text: str = None):
        """合并从 start_index 到 end_index 的段（包含）。"""
        if (
            start_index < 0
            or end_index >= len(self.segments)
            or start_index > end_index
        ):
            raise IndexError("无效的段索引。")
        merged_start_time = self.segments[start_index].start_time
        merged_end_time = self.segments[end_index].end_time
        if merged_text is None:
            merged_text = "".join(
                seg.text for seg in self.segments[start_index : end_index + 1]
            )
        merged_seg = ASRDataSeg(merged_text, merged_start_time, merged_end_time)
        # 替换 segments[start_index:end_index+1] 为 merged_seg
        self.segments[start_index : end_index + 1] = [merged_seg]

    def merge_with_next_segment(self, index: int) -> None:
        """合并指定索引的段与下一个段。"""
        if index < 0 or index >= len(self.segments) - 1:
            raise IndexError("索引超出范围或没有下一个段可合并。")
        current_seg = self.segments[index]
        next_seg = self.segments[index + 1]
        merged_text = f"{current_seg.text} {next_seg.text}"
        merged_seg = ASRDataSeg(merged_text, current_seg.start_time, next_seg.end_time)
        self.segments[index] = merged_seg
        # 删除下一个段
        del self.segments[index + 1]

    def optimize_timing(self, threshold_ms: int = 1000) -> "ASRData":
        """优化字幕显示时间，如果相邻字幕段之间的时间间隔小于阈值，
        则将交界点设置为两段字幕的中间时间点

        Args:
            threshold_ms: 时间间隔阈值(毫秒)，默认800ms

        Returns:
            返回自身以支持链式调用
        """
        if self.is_word_timestamp():
            return self

        if not self.segments:
            return self

        for i in range(len(self.segments) - 1):
            current_seg = self.segments[i]
            next_seg = self.segments[i + 1]

            # 计算时间间隔
            time_gap = next_seg.start_time - current_seg.end_time

            # 如果间隔小于阈值，将交界点设置为 3/4 时间点
            if time_gap < threshold_ms:
                mid_time = (
                    current_seg.end_time + next_seg.start_time
                ) // 2 + time_gap // 4
                current_seg.end_time = mid_time
                next_seg.start_time = mid_time

        return self

    def __str__(self):
        return self.to_txt()

    @staticmethod
    def from_subtitle_file(file_path: str, ass_layout_hint: str = None) -> "ASRData":
        """从文件路径加载ASRData实例

        Args:
            file_path: 字幕文件路径，支持.srt、.vtt、.ass、.json格式

        Returns:
            ASRData: 解析后的ASRData实例

        Raises:
            ValueError: 不支持的文件格式或文件读取错误
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = file_path.read_text(encoding="gbk")

        suffix = file_path.suffix.lower()

        if suffix == ".srt":
            return ASRData.from_srt(content)
        elif suffix == ".vtt":
            if "<c>" in content:  # YouTube VTT格式包含字级时间戳
                return ASRData.from_youtube_vtt(content)
            return ASRData.from_vtt(content)
        elif suffix == ".ass":
            return ASRData.from_ass(content, layout_hint=ass_layout_hint)
        elif suffix == ".json":
            return ASRData.from_json(json.loads(content))
        else:
            raise ValueError(f"不支持的文件格式: {suffix}")

    @staticmethod
    def from_json(json_data: dict) -> "ASRData":
        """从JSON数据创建ASRData实例"""
        segments = []
        for i in sorted(json_data.keys(), key=int):
            segment_data = json_data[i]
            segment = ASRDataSeg(
                text=segment_data["original_subtitle"],
                translated_text=segment_data["translated_subtitle"],
                start_time=segment_data["start_time"],
                end_time=segment_data["end_time"],
            )
            for key in ('asr_segment_id','asr_origin','speaker_id'):
                value=segment_data.get(key)
                if isinstance(value,(str,int)) and not isinstance(value,bool):setattr(segment,key,value)
            segments.append(segment)
        return ASRData(segments)

    @staticmethod
    def from_srt(srt_str: str) -> "ASRData":
        """
        从SRT格式的字符串创建ASRData实例。

        :param srt_str: 包含SRT格式字幕的字符串。
        :return: 解析后的ASRData实例。
        """
        segments = []
        srt_time_pattern = re.compile(
            r"(\d{2}):(\d{2}):(\d{1,2})[.,](\d{3})\s-->\s(\d{2}):(\d{2}):(\d{1,2})[.,](\d{3})"
        )
        blocks = re.split(r"\n\s*\n", srt_str.strip())

        # 如果超过96%的块都超过4行，说明可能包含翻译文本
        blocks_lines_count = [len(block.splitlines()) for block in blocks]
        if (
            len(blocks_lines_count) > 0
            and all(count <= 4 for count in blocks_lines_count)
            and sum(count == 4 for count in blocks_lines_count)
            / len(blocks_lines_count)
            >= 0.98
        ):
            has_translated_subtitle = True
        else:
            has_translated_subtitle = False

        for block in blocks:
            lines = block.splitlines()
            if len(lines) < 3:  # 至少需要3行：序号、时间戳和文本
                continue

            match = srt_time_pattern.match(lines[1])
            if not match:
                continue

            time_parts = list(map(int, match.groups()))
            start_time = sum(
                [
                    time_parts[0] * 3600000,
                    time_parts[1] * 60000,
                    time_parts[2] * 1000,
                    time_parts[3],
                ]
            )
            end_time = sum(
                [
                    time_parts[4] * 3600000,
                    time_parts[5] * 60000,
                    time_parts[6] * 1000,
                    time_parts[7],
                ]
            )

            if has_translated_subtitle and len(lines) >= 4:
                text = lines[2]
                translated_text = lines[3]
                segments.append(
                    ASRDataSeg(
                        text, start_time, end_time, translated_text=translated_text
                    )
                )
            else:
                text = lines[2]
                segments.append(ASRDataSeg(text, start_time, end_time))

        return ASRData(segments)

    @staticmethod
    def from_vtt(vtt_str: str) -> "ASRData":
        """
        从 VTT 格式的字符串创建ASRData实例。

        :param vtt_str: VTT格式的字幕字符串
        :return: ASRData实例
        """
        segments = []
        # 跳过头部元数据
        content = vtt_str.split("\n\n")[2:]

        timestamp_pattern = re.compile(
            r"(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})"
        )

        for block in content:
            lines = block.strip().split("\n")
            if len(lines) < 2:
                continue

            # 解析时间戳行
            timestamp_line = lines[1]
            match = timestamp_pattern.match(timestamp_line)
            if not match:
                continue

            # 提取开始和结束时间
            time_parts = list(map(int, match.groups()))
            start_time = sum(
                [
                    time_parts[0] * 3600000,
                    time_parts[1] * 60000,
                    time_parts[2] * 1000,
                    time_parts[3],
                ]
            )
            end_time = sum(
                [
                    time_parts[4] * 3600000,
                    time_parts[5] * 60000,
                    time_parts[6] * 1000,
                    time_parts[7],
                ]
            )

            # 处理文本内容
            text_line = " ".join(lines[2:])
            cleaned_text = re.sub(r"<\d{2}:\d{2}:\d{2}\.\d{3}>", "", text_line)
            cleaned_text = re.sub(r"</?c>", "", cleaned_text)
            cleaned_text = cleaned_text.strip()

            if cleaned_text and cleaned_text != " ":
                segments.append(ASRDataSeg(cleaned_text, start_time, end_time))

        return ASRData(segments)

    @staticmethod
    def from_youtube_vtt(vtt_str: str) -> "ASRData":
        """
        从YouTube VTT格式的字符串创建ASRData实例，提取字级时间戳。

        :param vtt_str: 包含VTT格式字幕的字符串
        :return: 解析后的ASRData实例
        """

        def parse_timestamp(ts: str) -> int:
            """将时间戳字符串转换为毫秒"""
            h, m, s = ts.split(":")
            return int(float(h) * 3600000 + float(m) * 60000 + float(s) * 1000)

        def split_timestamped_text(text: str) -> List[ASRDataSeg]:
            """分离带时间戳的文本为单词段"""
            pattern = re.compile(r"<(\d{2}:\d{2}:\d{2}\.\d{3})>([^<]*)")
            matches = list(pattern.finditer(text))
            word_segments = []

            for i in range(len(matches) - 1):
                current_match = matches[i]
                next_match = matches[i + 1]

                start_time = parse_timestamp(current_match.group(1))
                end_time = parse_timestamp(next_match.group(1))
                word = current_match.group(2).strip()

                if word:
                    word_segments.append(ASRDataSeg(word, start_time, end_time))

            return word_segments

        segments = []
        blocks = re.split(r"\n\n+", vtt_str.strip())

        timestamp_pattern = re.compile(
            r"(\d{2}):(\d{2}):(\d{2}\.\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}\.\d{3})"
        )
        for block in blocks:
            lines = block.strip().split("\n")
            if not lines:
                continue

            match = timestamp_pattern.match(lines[0])
            if not match:
                continue

            block_start_time = (
                int(match.group(1)) * 3600000
                + int(match.group(2)) * 60000
                + float(match.group(3)) * 1000
            )
            block_end_time = (
                int(match.group(4)) * 3600000
                + int(match.group(5)) * 60000
                + float(match.group(6)) * 1000
            )

            # 获取文本内容
            text = "\n".join(lines)

            timestamp_row = re.search(r"\n(.*?<c>.*?</c>.*)", block)
            if timestamp_row:
                text = re.sub(r"<c>|</c>", "", timestamp_row.group(1))
                block_start_time_string = (
                    f"{match.group(1)}:{match.group(2)}:{match.group(3)}"
                )
                block_end_time_string = (
                    f"{match.group(4)}:{match.group(5)}:{match.group(6)}"
                )
                text = f"<{block_start_time_string}>{text}<{block_end_time_string}>"

                # 分离每个带时间戳的单词
                word_segments = split_timestamped_text(text)
                segments.extend(word_segments)

        return ASRData(segments)

    @staticmethod
    def from_ass(ass_str: str, layout_hint: str = None) -> "ASRData":
        """
        从ASS格式的字符串创建ASRData实例。

        :param ass_str: 包含ASS格式字幕的字符串
        :return: ASRData实例
        """
        from app.core.utils.line_geometry import restore as restore_line_geometry
        ass_str = restore_line_geometry(ass_str)
        segments = []
        ass_time_pattern = re.compile(
            r"Dialogue: \d+,(\d+:\d{2}:\d{2}\.\d{2}),(\d+:\d{2}:\d{2}\.\d{2}),(.*?),.*?,\d+,\d+,\d+,.*?,(.*?)$"
        )

        def parse_ass_time(time_str: str) -> int:
            """将ASS时间戳转换为毫秒"""
            hours, minutes, seconds = time_str.split(":")
            seconds, centiseconds = seconds.split(".")
            return (
                int(hours) * 3600000
                + int(minutes) * 60000
                + int(seconds) * 1000
                + int(centiseconds) * 10
            )

        # VideoCaptioner 为了实现柔边、描边和阴影，会为同一句字幕写入多条
        # 相同时间轴的 Dialogue。旧读取逻辑把这些渲染层误当成双语字幕，
        # 导致只有 Default 样式时 text 为空，重新打开批量工程便显示空表。
        is_videocaptioner_ass = "Script generated by VideoCaptioner" in ass_str
        layout_marks=re.findall(r'^; VideoCaptionerLayout: (.+)$',ass_str,re.MULTILINE)
        if len(layout_marks)>1:raise ValueError('ASS双语角色标记重复，未猜测原文和译文')
        source_layout=layout_marks[0].strip() if layout_marks else layout_hint
        if source_layout is not None and source_layout not in ('原文在上','译文在上','仅原文','仅译文'):
            raise ValueError('ASS双语角色标记无效')
        generated_groups = {}

        # 按行处理ASS文件
        for line in ass_str.splitlines():
            if line.startswith("Dialogue:"):
                match = ass_time_pattern.match(line)
                if match:
                    start_time = parse_ass_time(match.group(1))
                    end_time = parse_ass_time(match.group(2))
                    style = match.group(3).strip()
                    text = match.group(4)

                    text = re.sub(r"\{[^}]*\}", "", text)
                    text = text.replace("\\N", "\n")
                    text = text.strip()

                    if not text:
                        continue

                    if is_videocaptioner_ass:
                        time_key = (start_time, end_time)
                        style_items = generated_groups.setdefault(time_key, [])
                        item = (style, text)
                        # 多层特效的文本完全相同，只保留一个语义层。
                        if item not in style_items:
                            style_items.append(item)
                    else:
                        segments.append(ASRDataSeg(text, start_time, end_time))

        if is_videocaptioner_ass:
            for (start_time, end_time), style_items in generated_groups.items():
                default_texts = [
                    text for style, text in style_items if style == "Default"
                ]
                secondary_texts = [
                    text for style, text in style_items if style == "Secondary"
                ]
                fallback_texts = [text for _, text in style_items]

                if default_texts and secondary_texts:
                    # Styles describe screen positions; saved layout describes language roles.
                    original,translation=(default_texts[0],secondary_texts[0]) if source_layout=='原文在上' else (secondary_texts[0],default_texts[0])
                    segments.append(ASRDataSeg(original,start_time,end_time,translation))
                elif default_texts:
                    segments.append(
                        ASRDataSeg(default_texts[0], start_time, end_time)
                    )
                elif secondary_texts:
                    segments.append(
                        ASRDataSeg(secondary_texts[0], start_time, end_time)
                    )
                elif fallback_texts:
                    segments.append(
                        ASRDataSeg(fallback_texts[0], start_time, end_time)
                    )

        return ASRData(segments)


if __name__ == "__main__":
    from pathlib import Path

    # 示例：从SRT文件创建ASRData并转换为ASS格式
    srt_file_path = "示例路径/字幕文件.srt"
    asr_data = ASRData.from_srt(Path(srt_file_path).read_text(encoding="utf-8"))
    print(
        asr_data.to_ass(
            style_str="示例样式字符串", save_path=srt_file_path.replace(".srt", ".ass")
        )
    )
