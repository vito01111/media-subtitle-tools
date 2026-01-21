#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基于懒猫算力舱的视频字幕OCR提取工具
使用FFmpeg抽帧 + PaddleOCR API识别字幕
使用前需要修改脚本中的服务接口地址：在脚本中搜索 {这里填你的用户名}，替换为你的懒猫用户名。
新增接口health检查机制，避免服务冷启动时，前几张图片识别失败
"""

import os
import sys
import re
import json
import time
import subprocess
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from difflib import SequenceMatcher
from typing import List, Tuple, Optional
import requests

# Windows系统设置UTF-8编码
if sys.platform == 'win32':
    import locale
    if locale.getpreferredencoding().upper() != 'UTF-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except:
            pass

# ==================== 配置参数 ====================
# 可通过环境变量覆盖
FRAME_INTERVAL = int(os.getenv('FRAME_INTERVAL', '3'))  # 抽帧间隔（秒）
MAX_WORKERS = int(os.getenv('MAX_WORKERS', '8'))  # 并发线程数
SIMILARITY_THRESHOLD = float(os.getenv('SIMILARITY_THRESHOLD', '0.8'))  # 字幕相似度阈值
NOISE_THRESHOLD = float(os.getenv('NOISE_THRESHOLD', '0.05'))  # 噪声过滤阈值（5%）
# 将 {这里填你的用户名} 替换为你的实际用户名
OCR_API_URL = os.getenv('OCR_API_URL', 'https://ocr-ai.{这里填你的用户名}.heiyu.space/ocr')
OCR_TIMEOUT = int(os.getenv('OCR_TIMEOUT', '30'))  # OCR请求超时（秒）
OCR_MAX_RETRIES = int(os.getenv('OCR_MAX_RETRIES', '3'))  # OCR最大重试次数

# 广告关键词黑名单
AD_KEYWORDS = [
    '关注', '点赞', '订阅', '转发', '分享', '评论',
    '公众号', '微信', 'VX', 'wx', 'WeChat',
    '抖音', 'TikTok', 'B站', 'bilibili',
    '官网', '官方', '下载', 'APP', 'app',
    '扫码', '二维码', 'QR', '链接',
]

# 网址正则
URL_PATTERN = re.compile(
    r'(https?://|www\.|[a-zA-Z0-9-]+\.(com|cn|net|org|tv|cc|me|io))',
    re.IGNORECASE
)

# 中文字符正则
CHINESE_PATTERN = re.compile(r'[\u4e00-\u9fff]+')


class SubtitleOCR:
    """视频字幕OCR提取器"""

    def __init__(self, video_path: str):
        self.video_path = Path(video_path)
        if not self.video_path.exists():
            raise FileNotFoundError(f"视频文件不存在: {video_path}")

        self.video_name = self.video_path.stem
        self.temp_dir = Path(f"temp_frames_{self.video_name}")
        self.output_srt = self.video_path.parent / f"{self.video_name}_ocr.srt"

        # 统计信息
        self.stats = {
            'extract_time': 0,
            'ocr_time': 0,
            'total_frames': 0,
            'processed_frames': 0,
            'valid_subtitles': 0,
        }

        # OCR结果缓存
        self.ocr_results = []

    def check_ffmpeg(self) -> bool:
        """检查FFmpeg是否可用"""
        try:
            subprocess.run(
                ['ffmpeg', '-version'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def get_video_duration(self) -> float:
        """获取视频时长（秒）"""
        cmd = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            str(self.video_path)
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return float(result.stdout.strip())
        except Exception as e:
            print(f"⚠️  无法获取视频时长: {e}")
            return 0

    def extract_frames(self) -> List[Path]:
        """使用FFmpeg抽帧（仅截取下方1/4区域）"""
        print(f"\n📹 开始抽帧: {self.video_path.name}")
        print(f"   间隔: {FRAME_INTERVAL}秒 | 区域: 下方1/4")

        # 创建临时目录
        self.temp_dir.mkdir(exist_ok=True)

        # FFmpeg命令
        output_pattern = str(self.temp_dir / "frame_%04d.png")
        cmd = [
            'ffmpeg',
            '-i', str(self.video_path),
            '-vf', f'fps=1/{FRAME_INTERVAL},crop=iw:ih/4:0:3*ih/4',
            '-q:v', '2',  # 高质量
            output_pattern,
            '-y'  # 覆盖已存在文件
        ]

        start_time = time.time()
        try:
            subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"FFmpeg抽帧失败: {e.stderr.decode('utf-8', errors='ignore')}")

        self.stats['extract_time'] = time.time() - start_time

        # 获取生成的帧文件
        frames = sorted(self.temp_dir.glob("frame_*.png"))
        self.stats['total_frames'] = len(frames)

        print(f"✅ 抽帧完成: {len(frames)}帧 | 耗时: {self.stats['extract_time']:.1f}秒")
        return frames

    def wait_for_service_ready(self, max_wait: int = 10) -> bool:
        """等待OCR服务就绪"""
        print(f"\n🔗 检测OCR服务...")

        # 构建健康检查URL（只替换路径部分，不影响域名）
        if OCR_API_URL.endswith('/ocr'):
            health_url = OCR_API_URL[:-4] + '/health'
        else:
            # 如果URL格式不是预期的，尝试通用方法
            from urllib.parse import urljoin, urlparse
            parsed = urlparse(OCR_API_URL)
            health_url = f"{parsed.scheme}://{parsed.netloc}/health"

        print(f"   健康检查URL: {health_url}")
        start = time.time()
        attempt_count = 0

        while time.time() - start < max_wait:
            attempt_count += 1
            try:
                response = requests.get(health_url, timeout=3)
                if response.status_code == 200:
                    elapsed = time.time() - start
                    print(f"✅ OCR服务已就绪 (耗时: {elapsed:.1f}秒)")
                    return True
                else:
                    print(f"   尝试 {attempt_count}: HTTP {response.status_code}")
            except Exception as e:
                print(f"   尝试 {attempt_count}: {type(e).__name__}: {str(e)[:50]}")

            time.sleep(2)

        print(f"⚠️  OCR服务未就绪（超时 {max_wait}秒），继续执行（依赖重试机制）")
        return False

    def call_ocr_api(self, image_path: Path) -> Optional[str]:
        """调用PaddleOCR API识别文字（带重试）"""
        last_error = None

        for attempt in range(OCR_MAX_RETRIES):
            try:
                with open(image_path, 'rb') as f:
                    files = {'file': (image_path.name, f, 'image/png')}
                    data = {
                        'request': json.dumps({
                            'max_size': 1920,
                            'det': True,
                            'rec': True,
                            'cls': True
                        })
                    }

                    response = requests.post(
                        OCR_API_URL,
                        files=files,
                        data=data,
                        timeout=OCR_TIMEOUT
                    )
                    response.raise_for_status()

                    result = response.json()
                    if 'results' in result and result['results']:
                        # 合并所有识别的文本
                        texts = [item.get('text', '').strip() for item in result['results']]
                        return ' '.join(filter(None, texts))

                    return None

            except requests.exceptions.Timeout as e:
                last_error = f"超时: {e}"
            except Exception as e:
                last_error = str(e)

            # 如果不是最后一次尝试，等待后重试
            if attempt < OCR_MAX_RETRIES - 1:
                wait_time = (attempt + 1) * 1  # 1s, 2s, 3s
                time.sleep(wait_time)

        # 所有重试都失败
        print(f"⚠️  OCR失败 {image_path.name}: {last_error}")
        return None

    def process_frame(self, frame_info: Tuple[int, Path]) -> Optional[Tuple[float, str]]:
        """处理单帧：OCR识别"""
        frame_idx, frame_path = frame_info
        timestamp = frame_idx * FRAME_INTERVAL

        text = self.call_ocr_api(frame_path)
        if text:
            return (timestamp, text)
        return None

    def process_frames_parallel(self, frames: List[Path]):
        """多线程并发处理帧"""
        print(f"\n🔍 开始OCR识别 (并发数: {MAX_WORKERS})")

        start_time = time.time()
        self.ocr_results = []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 提交所有任务
            futures = {
                executor.submit(self.process_frame, (idx, frame)): idx
                for idx, frame in enumerate(frames)
            }

            # 实时显示进度
            for future in as_completed(futures):
                self.stats['processed_frames'] += 1
                result = future.result()

                if result:
                    self.ocr_results.append(result)

                # 显示进度
                progress = self.stats['processed_frames'] / self.stats['total_frames']
                elapsed = time.time() - start_time
                eta = (elapsed / progress - elapsed) if progress > 0 else 0

                print(f"\r   进度: {self.stats['processed_frames']}/{self.stats['total_frames']} "
                      f"({progress*100:.1f}%) | "
                      f"已用: {elapsed:.1f}s | "
                      f"预计剩余: {eta:.1f}s", end='')

        self.stats['ocr_time'] = time.time() - start_time
        print(f"\n✅ OCR完成: {len(self.ocr_results)}条结果 | 耗时: {self.stats['ocr_time']:.1f}秒")

    def is_noise(self, text: str) -> bool:
        """判断是否为噪声文本"""
        # 必须包含中文
        if not CHINESE_PATTERN.search(text):
            return True

        # 过滤网址
        if URL_PATTERN.search(text):
            return True

        # 过滤广告关键词
        text_lower = text.lower()
        for keyword in AD_KEYWORDS:
            if keyword.lower() in text_lower:
                return True

        return False

    def filter_noise(self):
        """过滤噪声和重复内容"""
        print(f"\n🧹 清洗字幕数据...")

        # 第一轮：基础过滤
        filtered = [(ts, text) for ts, text in self.ocr_results if not self.is_noise(text)]

        # 第二轮：动态黑名单（高频噪声）
        if filtered:
            text_counter = Counter([text for _, text in filtered])
            threshold = len(filtered) * NOISE_THRESHOLD
            blacklist = {text for text, count in text_counter.items() if count > threshold}

            if blacklist:
                print(f"   发现高频噪声 {len(blacklist)} 条:")
                for text in list(blacklist)[:3]:
                    print(f"     - {text[:30]}...")

            filtered = [(ts, text) for ts, text in filtered if text not in blacklist]

        removed = len(self.ocr_results) - len(filtered)
        print(f"✅ 过滤完成: 移除 {removed} 条噪声 | 保留 {len(filtered)} 条")

        self.ocr_results = filtered

    def merge_similar_subtitles(self) -> List[Tuple[float, float, str]]:
        """合并相似字幕，生成时间轴"""
        print(f"\n🔗 合并相似字幕 (相似度阈值: {SIMILARITY_THRESHOLD})...")

        if not self.ocr_results:
            return []

        # 按时间排序
        self.ocr_results.sort(key=lambda x: x[0])

        merged = []
        current_start = self.ocr_results[0][0]
        current_text = self.ocr_results[0][1]

        for i in range(1, len(self.ocr_results)):
            timestamp, text = self.ocr_results[i]

            # 计算相似度
            similarity = SequenceMatcher(None, current_text, text).ratio()

            if similarity >= SIMILARITY_THRESHOLD:
                # 相似，延长当前字幕时间
                continue
            else:
                # 不相似，保存当前字幕，开始新字幕
                current_end = timestamp
                merged.append((current_start, current_end, current_text))
                current_start = timestamp
                current_text = text

        # 添加最后一条字幕
        final_end = current_start + FRAME_INTERVAL
        merged.append((current_start, final_end, current_text))

        self.stats['valid_subtitles'] = len(merged)
        print(f"✅ 合并完成: {len(merged)} 条字幕")

        return merged

    def format_timestamp(self, seconds: float) -> str:
        """格式化时间戳为SRT格式 (hh:mm:ss,ms)"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int((seconds % 1) * 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def generate_srt(self, subtitles: List[Tuple[float, float, str]]):
        """生成SRT字幕文件"""
        print(f"\n💾 生成SRT文件: {self.output_srt.name}")

        with open(self.output_srt, 'w', encoding='utf-8') as f:
            for idx, (start, end, text) in enumerate(subtitles, 1):
                f.write(f"{idx}\n")
                f.write(f"{self.format_timestamp(start)} --> {self.format_timestamp(end)}\n")
                f.write(f"{text}\n\n")

        print(f"✅ 文件已保存: {self.output_srt}")

    def cleanup(self):
        """清理临时文件"""
        if self.temp_dir.exists():
            try:
                shutil.rmtree(self.temp_dir)
                print(f"🗑️  已清理临时文件: {self.temp_dir}")
            except Exception as e:
                print(f"⚠️  清理临时文件失败: {e}")

    def print_stats(self):
        """打印统计信息"""
        total_time = self.stats['extract_time'] + self.stats['ocr_time']
        print(f"\n{'='*50}")
        print(f"📊 处理统计")
        print(f"{'='*50}")
        print(f"  抽帧耗时:     {self.stats['extract_time']:.1f}秒")
        print(f"  OCR耗时:      {self.stats['ocr_time']:.1f}秒")
        print(f"  总耗时:       {total_time:.1f}秒")
        print(f"  总帧数:       {self.stats['total_frames']}")
        print(f"  有效字幕:     {self.stats['valid_subtitles']}")
        print(f"{'='*50}")

    def run(self):
        """执行完整流程"""
        try:
            # 检查FFmpeg
            if not self.check_ffmpeg():
                raise RuntimeError("未找到FFmpeg，请先安装: https://ffmpeg.org/download.html")

            # 1. 抽帧
            frames = self.extract_frames()
            if not frames:
                raise RuntimeError("未能抽取任何帧")

            # 2. 等待OCR服务就绪
            self.wait_for_service_ready()

            # 3. OCR识别
            self.process_frames_parallel(frames)

            # 4. 清洗数据
            self.filter_noise()

            # 5. 合并字幕
            subtitles = self.merge_similar_subtitles()

            # 6. 生成SRT
            if subtitles:
                self.generate_srt(subtitles)
            else:
                print("⚠️  未识别到有效字幕")

            # 6. 打印统计
            self.print_stats()

        finally:
            # 清理临时文件
            self.cleanup()


def select_video_file() -> Optional[Path]:
    """交互式选择视频文件"""
    # 查找当前目录的MP4文件
    mp4_files = list(Path('.').glob('*.mp4'))

    if not mp4_files:
        print("❌ 当前目录未找到MP4文件")
        return None

    if len(mp4_files) == 1:
        return mp4_files[0]

    # 多个文件，提供选择
    print(f"\n📁 发现 {len(mp4_files)} 个视频文件:")
    for idx, file in enumerate(mp4_files, 1):
        size_mb = file.stat().st_size / (1024 * 1024)
        print(f"  [{idx}] {file.name} ({size_mb:.1f} MB)")

    while True:
        try:
            choice = input(f"\n请选择 [1-{len(mp4_files)}] 或按 Ctrl+C 退出: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(mp4_files):
                return mp4_files[idx]
            else:
                print(f"⚠️  请输入 1-{len(mp4_files)} 之间的数字")
        except ValueError:
            print("⚠️  请输入有效数字")
        except KeyboardInterrupt:
            print("\n\n👋 已取消")
            return None


def main():
    """主函数"""
    print("="*60)
    print("🎬 视频字幕OCR提取工具")
    print("="*60)

    # 获取视频文件
    video_path = None

    if len(sys.argv) > 1:
        # 命令行参数指定
        video_path = Path(sys.argv[1])
        if not video_path.exists():
            print(f"❌ 文件不存在: {sys.argv[1]}")
            return 1
    else:
        # 交互式选择
        video_path = select_video_file()
        if not video_path:
            return 1

    # 执行OCR
    try:
        ocr = SubtitleOCR(video_path)
        ocr.run()
        print("\n✨ 处理完成!")
        return 0
    except KeyboardInterrupt:
        print("\n\n⚠️  用户中断")
        return 130
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
