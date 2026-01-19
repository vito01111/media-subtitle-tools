#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于懒猫算力舱的视频语音字幕识别工具
使用 faster-whisper-large-v2 API 进行语音识别，生成 SRT 字幕文件
使用前需要修改脚本中的服务接口地址：在脚本中搜索 {这里填你的用户名}，替换为你的懒猫用户名。
"""

import os
import sys
import time
import glob
import subprocess
import argparse
import re
from pathlib import Path
from typing import List, Tuple, Optional
import requests

# Windows 系统设置 UTF-8 编码
if sys.platform == "win32":
    import locale
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8')

# 清除代理设置，避免连接问题
os.environ.pop('HTTP_PROXY', None)
os.environ.pop('HTTPS_PROXY', None)
os.environ.pop('http_proxy', None)
os.environ.pop('https_proxy', None)

# 配置参数
SEGMENT_DURATION = int(os.environ.get('SEGMENT_DURATION', '900'))  # 默认15分钟
# 将 {这里填你的用户名} 替换为你的实际用户名
ASR_API_URL = os.environ.get('ASR_API_URL', 'https://asr-ai.{这里填你的用户名}.heiyu.space/v1/audio/transcriptions')
ASR_MODEL = os.environ.get('ASR_MODEL', 'Systran/faster-whisper-large-v2')
MAX_RETRIES = 3
TIMEOUT = 300  # 5分钟超时

# 状态图标
ICON_SUCCESS = "✅"
ICON_WARNING = "⚠️"
ICON_ERROR = "❌"
ICON_PROCESSING = "🔄"


def print_status(message: str, status: str = "info"):
    """打印带状态图标的消息"""
    icons = {
        "success": ICON_SUCCESS,
        "warning": ICON_WARNING,
        "error": ICON_ERROR,
        "processing": ICON_PROCESSING,
        "info": "ℹ️"
    }
    icon = icons.get(status, "")
    print(f"{icon} {message}")


def check_ffmpeg():
    """检查 FFmpeg 是否安装"""
    try:
        subprocess.run(['ffmpeg', '-version'],
                      stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE,
                      check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print_status("FFmpeg 未安装或不在 PATH 中", "error")
        print("请安装 FFmpeg: https://ffmpeg.org/download.html")
        return False


def select_language(default_language: Optional[str] = None) -> Optional[str]:
    """选择识别语言"""
    # 常用语言列表
    languages = [
        ("zh", "中文 (Chinese)"),
        ("ja", "日语 (Japanese)"),
        ("en", "英语 (English)"),
        ("ko", "韩语 (Korean)"),
        ("es", "西班牙语 (Spanish)"),
        ("fr", "法语 (French)"),
        ("de", "德语 (German)"),
        ("ru", "俄语 (Russian)"),
    ]

    # 如果指定了默认语言且有效，直接返回
    if default_language:
        valid_codes = [code for code, _ in languages]
        if default_language in valid_codes:
            lang_name = next(name for code, name in languages if code == default_language)
            print_status(f"使用指定语言: {lang_name}", "success")
            return default_language

    # 显示语言选择菜单
    print("\n请选择识别语言:")
    for i, (code, name) in enumerate(languages, 1):
        print(f"  {i}. {name}")

    while True:
        try:
            choice = input(f"\n请选择语言 (1-{len(languages)}): ").strip()
            index = int(choice) - 1
            if 0 <= index < len(languages):
                selected_code, selected_name = languages[index]
                print_status(f"已选择: {selected_name}", "success")
                return selected_code
            else:
                print_status(f"请输入 1 到 {len(languages)} 之间的数字", "warning")
        except (ValueError, KeyboardInterrupt):
            print_status("\n已取消", "warning")
            return None


def select_video_file() -> Optional[str]:
    """选择视频文件"""
    # 查找当前目录的所有 MP4 文件（不区分大小写，避免重复）
    all_files = glob.glob("*.mp4") + glob.glob("*.MP4")
    # 去重（Windows 文件系统不区分大小写）
    video_files = []
    seen_lower = set()
    for f in all_files:
        lower_name = f.lower()
        if lower_name not in seen_lower:
            seen_lower.add(lower_name)
            video_files.append(f)

    if not video_files:
        print_status("当前目录没有找到 MP4 视频文件", "error")
        return None

    if len(video_files) == 1:
        print_status(f"找到视频文件: {video_files[0]}", "success")
        return video_files[0]

    # 多个文件时提供选择
    print("\n找到多个视频文件:")
    for i, file in enumerate(video_files, 1):
        file_size = os.path.getsize(file) / (1024 * 1024)  # MB
        print(f"  {i}. {file} ({file_size:.1f} MB)")

    while True:
        try:
            choice = input(f"\n请选择视频文件 (1-{len(video_files)}): ").strip()
            index = int(choice) - 1
            if 0 <= index < len(video_files):
                return video_files[index]
            else:
                print_status(f"请输入 1 到 {len(video_files)} 之间的数字", "warning")
        except (ValueError, KeyboardInterrupt):
            print_status("\n已取消", "warning")
            return None


def extract_audio(video_path: str, output_path: str) -> bool:
    """从视频中提取音频"""
    print_status(f"正在从视频中提取音频...", "processing")
    start_time = time.time()

    cmd = [
        'ffmpeg', '-i', video_path,
        '-vn',  # 不包含视频
        '-acodec', 'libmp3lame',  # MP3 编码
        '-ab', '64k',  # 64k 比特率
        '-ar', '32000',  # 32kHz 采样率
        '-y',  # 覆盖已存在的文件
        output_path
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            encoding='utf-8',
            errors='ignore'
        )
        elapsed = time.time() - start_time
        print_status(f"音频提取完成 (耗时: {elapsed:.1f}秒)", "success")
        return True
    except subprocess.CalledProcessError as e:
        print_status(f"音频提取失败: {e.stderr}", "error")
        return False


def split_audio(audio_path: str, output_pattern: str, segment_duration: int) -> List[str]:
    """分割音频文件"""
    print_status(f"正在分割音频 (每段 {segment_duration//60} 分钟)...", "processing")
    start_time = time.time()

    cmd = [
        'ffmpeg', '-i', audio_path,
        '-f', 'segment',  # 分割模式
        '-segment_time', str(segment_duration),  # 每段时长
        '-c', 'copy',  # 直接复制，不重新编码
        '-y',
        output_pattern
    ]

    try:
        subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            encoding='utf-8',
            errors='ignore'
        )

        # 查找生成的音频片段
        pattern = output_pattern.replace('%03d', '*')
        segments = sorted(glob.glob(pattern))

        elapsed = time.time() - start_time
        print_status(f"音频分割完成，共 {len(segments)} 个片段 (耗时: {elapsed:.1f}秒)", "success")
        return segments
    except subprocess.CalledProcessError as e:
        print_status(f"音频分割失败: {e.stderr}", "error")
        return []


def call_asr_api(audio_path: str, language: str, retry_count: int = 0) -> Optional[str]:
    """调用 ASR API 进行语音识别"""
    try:
        with open(audio_path, 'rb') as f:
            files = {'file': (os.path.basename(audio_path), f, 'audio/mpeg')}
            data = {
                'model': ASR_MODEL,
                'language': language,
                'response_format': 'srt'
            }

            timeout = TIMEOUT * (retry_count + 1)  # 重试时增加超时时间
            response = requests.post(
                ASR_API_URL,
                files=files,
                data=data,
                timeout=timeout
            )

            if response.status_code == 200:
                return response.text
            else:
                print_status(
                    f"API 返回错误 (状态码: {response.status_code}): {response.text[:200]}",
                    "error"
                )
                return None

    except requests.exceptions.Timeout:
        print_status(f"请求超时 (超时时间: {timeout}秒)", "warning")
        return None
    except requests.exceptions.RequestException as e:
        print_status(f"网络请求失败: {str(e)}", "error")
        return None
    except Exception as e:
        print_status(f"未知错误: {str(e)}", "error")
        return None


def process_audio_segment(segment_path: str, language: str, segment_index: int, total_segments: int) -> Optional[str]:
    """处理单个音频片段"""
    segment_name = os.path.basename(segment_path)
    print_status(f"正在识别片段 [{segment_index}/{total_segments}]: {segment_name}", "processing")

    start_time = time.time()

    # 重试机制
    for attempt in range(MAX_RETRIES):
        if attempt > 0:
            print_status(f"第 {attempt + 1} 次重试...", "warning")
            time.sleep(2 ** attempt)  # 指数退避

        result = call_asr_api(segment_path, language, attempt)

        if result:
            elapsed = time.time() - start_time
            print_status(f"片段 [{segment_index}/{total_segments}] 识别完成 (耗时: {elapsed:.1f}秒)", "success")
            return result

    print_status(f"片段 [{segment_index}/{total_segments}] 识别失败，已达到最大重试次数", "error")
    return None


def parse_srt_time(time_str: str) -> float:
    """解析 SRT 时间格式为秒数"""
    # 格式: hh:mm:ss,ms
    match = re.match(r'(\d+):(\d+):(\d+),(\d+)', time_str)
    if match:
        hours, minutes, seconds, milliseconds = map(int, match.groups())
        return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000
    return 0.0


def format_srt_time(seconds: float) -> str:
    """将秒数格式化为 SRT 时间格式"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    milliseconds = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def merge_srt_files(srt_contents: List[Tuple[int, str]], segment_duration: int) -> str:
    """合并多个 SRT 内容，调整时间偏移"""
    print_status("正在合并字幕文件...", "processing")
    start_time = time.time()

    merged_subtitles = []
    subtitle_counter = 1

    for segment_index, srt_content in srt_contents:
        if not srt_content or not srt_content.strip():
            continue

        # 计算时间偏移
        time_offset = segment_index * segment_duration

        # 解析 SRT 内容
        blocks = srt_content.strip().split('\n\n')

        for block in blocks:
            lines = block.strip().split('\n')
            if len(lines) < 3:
                continue

            # 解析时间轴
            time_line = lines[1]
            match = re.match(r'(\S+)\s+-->\s+(\S+)', time_line)
            if not match:
                continue

            start_time_str, end_time_str = match.groups()

            # 调整时间
            start_seconds = parse_srt_time(start_time_str) + time_offset
            end_seconds = parse_srt_time(end_time_str) + time_offset

            # 重新格式化
            new_start = format_srt_time(start_seconds)
            new_end = format_srt_time(end_seconds)

            # 字幕文本
            subtitle_text = '\n'.join(lines[2:])

            # 添加到合并列表
            merged_subtitles.append(f"{subtitle_counter}\n{new_start} --> {new_end}\n{subtitle_text}")
            subtitle_counter += 1

    elapsed = time.time() - start_time
    print_status(f"字幕合并完成，共 {len(merged_subtitles)} 条字幕 (耗时: {elapsed:.1f}秒)", "success")

    return '\n\n'.join(merged_subtitles) + '\n'


def cleanup_temp_files(files: List[str]):
    """清理临时文件"""
    print_status("正在清理临时文件...", "processing")
    for file in files:
        try:
            if os.path.exists(file):
                os.remove(file)
        except Exception as e:
            print_status(f"删除文件失败 {file}: {str(e)}", "warning")


def main():
    parser = argparse.ArgumentParser(description='视频语音字幕识别工具')
    parser.add_argument('video', nargs='?', help='视频文件路径')
    parser.add_argument('-l', '--language', default=None,
                       help='语言代码 (zh/ja/en/ko/es/fr/de/ru)')
    parser.add_argument('-d', '--duration', type=int, default=SEGMENT_DURATION,
                       help=f'音频分割时长（秒），默认: {SEGMENT_DURATION}')

    args = parser.parse_args()

    print("=" * 60)
    print("视频语音字幕识别工具")
    print("=" * 60)

    # 检查 FFmpeg
    if not check_ffmpeg():
        return 1

    # 选择视频文件
    video_path = args.video
    if not video_path:
        video_path = select_video_file()
        if not video_path:
            return 1

    if not os.path.exists(video_path):
        print_status(f"视频文件不存在: {video_path}", "error")
        return 1

    # 选择语言
    language = select_language(args.language)
    if not language:
        return 1

    # 获取视频文件名（不含扩展名）
    video_name = Path(video_path).stem
    segment_duration = args.duration

    # 临时文件路径
    audio_path = f"{video_name}_audio.mp3"
    segment_pattern = f"{video_name}_segment_%03d.mp3"
    output_srt = f"{video_name}_{language}.srt"

    total_start_time = time.time()
    temp_files = [audio_path]

    try:
        # 第一步：提取音频
        if not extract_audio(video_path, audio_path):
            return 1

        # 第二步：分割音频
        segments = split_audio(audio_path, segment_pattern, segment_duration)
        if not segments:
            return 1

        temp_files.extend(segments)

        # 第三步：顺序识别
        print("\n" + "=" * 60)
        print(f"开始语音识别 (共 {len(segments)} 个片段)")
        print("=" * 60)

        srt_results = []
        failed_segments = []

        for i, segment in enumerate(segments):
            result = process_audio_segment(segment, language, i + 1, len(segments))
            if result:
                srt_results.append((i, result))
            else:
                failed_segments.append(i + 1)

        if not srt_results:
            print_status("所有片段识别失败", "error")
            return 1

        if failed_segments:
            print_status(f"部分片段识别失败: {failed_segments}", "warning")

        # 第四步：合并字幕
        merged_srt = merge_srt_files(srt_results, segment_duration)

        # 保存字幕文件
        with open(output_srt, 'w', encoding='utf-8') as f:
            f.write(merged_srt)

        # 统计信息
        total_elapsed = time.time() - total_start_time
        subtitle_count = len(merged_srt.strip().split('\n\n'))

        print("\n" + "=" * 60)
        print_status(f"字幕文件已生成: {output_srt}", "success")
        print(f"  - 总耗时: {total_elapsed:.1f} 秒")
        print(f"  - 字幕总条数: {subtitle_count}")
        print(f"  - 成功片段: {len(srt_results)}/{len(segments)}")
        if failed_segments:
            print(f"  - 失败片段: {failed_segments}")
        print("=" * 60)

        return 0

    except KeyboardInterrupt:
        print_status("\n\n用户中断操作", "warning")
        return 1
    except Exception as e:
        print_status(f"发生错误: {str(e)}", "error")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        # 清理临时文件
        cleanup_temp_files(temp_files)


if __name__ == '__main__':
    sys.exit(main())
