from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import tempfile
from pathlib import Path


DEFAULT_MAX_SIZE_KB = 500
SEEK_PREROLL_SECONDS = 3.0
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or "未知错误"
        raise RuntimeError(f"命令执行失败：{detail}")
    return result


def _find_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")

    # 有些 Windows 发行版只把 ffmpeg.exe 加入 PATH，自动寻找同目录的 ffprobe。
    if ffmpeg and not ffprobe:
        sibling = Path(ffmpeg).with_name("ffprobe.exe")
        if sibling.is_file():
            ffprobe = str(sibling)

    missing = []
    if not ffmpeg:
        missing.append("ffmpeg")
    if not ffprobe:
        missing.append("ffprobe")
    if missing:
        raise RuntimeError(
            "找不到 " + ", ".join(missing) + "。请将 FFmpeg 的 bin 目录加入 PATH。"
        )
    return ffmpeg, ffprobe


def _probe_video(ffprobe: str, video_path: Path) -> dict:
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        (
            "stream=duration,width,height,pix_fmt,profile,color_range,color_space,"
            "color_transfer,color_primaries:stream_side_data=side_data_type,dv_profile:"
            "format=duration"
        ),
        "-of",
        "json",
        str(video_path),
    ]
    result = _run(command)
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FFprobe 返回了无法解析的信息") from exc

    stream = (data.get("streams") or [{}])[0]
    format_info = data.get("format") or {}
    duration_value = stream.get("duration") or format_info.get("duration")
    try:
        duration = float(duration_value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("无法读取视频时长") from exc
    if duration <= 0:
        raise RuntimeError("视频时长无效")

    return {
        "duration": duration,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "color_range": stream.get("color_range"),
        "color_space": stream.get("color_space"),
        "color_transfer": stream.get("color_transfer"),
        "color_primaries": stream.get("color_primaries"),
        "pix_fmt": stream.get("pix_fmt"),
        "profile": stream.get("profile"),
        "side_data_types": tuple(
            str(item.get("side_data_type") or "")
            for item in stream.get("side_data_list") or ()
        ),
    }


def _is_hdr(info: dict) -> bool:
    transfer = str(info.get("color_transfer") or "").lower()
    if transfer in HDR_TRANSFERS:
        return True
    return any(
        "dovi" in value.lower() or "dolby vision" in value.lower()
        for value in info.get("side_data_types") or ()
    )


def _color_options(info: dict) -> list[str]:
    options: list[str] = []
    for option, key in (
        ("-color_range", "color_range"),
        ("-colorspace", "color_space"),
        ("-color_trc", "color_transfer"),
        ("-color_primaries", "color_primaries"),
    ):
        value = info.get(key)
        if value and value not in {"unknown", "reserved", "N/A"}:
            options.extend([option, str(value)])
    return options


def _temp_path(output_dir: Path, extension: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        prefix=".random-video-screenshots-",
        suffix=f".{extension}",
        dir=output_dir,
        delete=False,
    )
    path = Path(handle.name)
    handle.close()
    path.unlink(missing_ok=True)
    return path


def _base_command(ffmpeg: str, video_path: Path, timestamp: float) -> list[str]:
    # M2TS/HEVC 在目标点直接 fast seek 时，第一帧可能缺少参考帧。画面通常表现为
    # 整体发白、彩色块或马赛克，看起来很像 HDR 过曝。先从目标点前几秒开始，
    # 再在解码后精确跳过预滚区，既能恢复参考帧，也不必从片头完整解码。
    preroll = min(max(timestamp, 0.0), SEEK_PREROLL_SECONDS)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(timestamp - preroll, 0.0):.3f}",
        "-i",
        str(video_path),
    ]
    if preroll:
        command += ["-ss", f"{preroll:.3f}"]
    command += [
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-an",
    ]
    return command


def _filter_option(width: int | None, output_format: str, info: dict) -> list[str]:
    filters: list[str] = []

    if output_format == "jpg" and _is_hdr(info):
        # tonemap 要求线性、单精度浮点输入。显式转换可以避免 FFmpeg 自动插入
        # 不正确的整数像素格式转换，并兼容 HDR10/HDR10+/HLG/Dolby Vision 基础层。
        filters += [
            "zscale=transfer=linear:npl=100",
            "format=gbrpf32le",
            "zscale=primaries=bt709",
            "tonemap=tonemap=mobius:param=0.3:desat=2",
            "zscale=transfer=bt709:matrix=bt709:range=tv",
        ]

    if width:
        filters.append(f"scale={width}:-2:force_original_aspect_ratio=decrease")

    if output_format == "jpg":
        filters.append("format=yuv420p")

    return ["-vf", ",".join(filters)] if filters else []


def _candidate_widths(source_width: int, max_width: int) -> list[int]:
    first = min(source_width, max_width) if source_width > 0 else max_width
    values = [first, 1600, 1280, 960, 800, 640, 480, 360]
    return sorted({max(2, min(first, value)) for value in values}, reverse=True)


def _encode_until_small(
    *,
    ffmpeg: str,
    video_path: Path,
    output_path: Path,
    timestamp: float,
    info: dict,
    output_format: str,
    max_bytes: int,
    max_width: int,
) -> int:
    widths = _candidate_widths(info.get("width", 0), max_width)
    best_temp: Path | None = None
    best_size: int | None = None
    last_error: str | None = None

    if output_format == "avif":
        encoders = _run([ffmpeg, "-hide_banner", "-encoders"], check=False).stdout
        if "libaom-av1" not in encoders:
            raise RuntimeError("当前 FFmpeg 不包含 libaom-av1，无法生成 HDR AVIF。")
        # 从高质量开始，逐步降低质量；避免默认从 CRF 30 这种激进档位起步。
        # CRF 0 接近无损但在 4K 画面上编码很慢，因此从 CRF 2 开始。
        quality_values = [2, 4, 6, 8, 10, 12, 16, 20, 24, 30, 38, 46, 54, 63]
    elif output_format == "jpg":
        quality_values = [2, 5, 8, 12, 18, 24, 30]
    else:
        quality_values = [0]

    try:
        for width in widths:
            for quality in quality_values:
                temp = _temp_path(output_path.parent, output_format)
                command = _base_command(ffmpeg, video_path, timestamp)
                command += _filter_option(width, output_format, info)

                if output_format == "avif":
                    command += [
                        "-c:v",
                        "libaom-av1",
                        "-still-picture",
                        "1",
                        "-crf",
                        str(quality),
                        "-b:v",
                        "0",
                        "-cpu-used",
                        "6",
                        "-pix_fmt",
                        "yuv420p10le",
                    ]
                    command += _color_options(info)
                    command += ["-f", "avif"]
                elif output_format == "jpg":
                    command += ["-c:v", "mjpeg", "-q:v", str(quality), "-pix_fmt", "yuvj420p"]
                    if _is_hdr(info):
                        command += [
                            "-color_range",
                            "pc",
                            "-colorspace",
                            "bt709",
                            "-color_trc",
                            "bt709",
                            "-color_primaries",
                            "bt709",
                        ]
                    command += ["-f", "image2"]
                else:
                    command += ["-c:v", "png", "-pix_fmt", "rgb48be", "-f", "image2"]

                command += ["-map_metadata", "0", "-y", str(temp)]
                result = _run(command, check=False)
                if result.returncode != 0 or not temp.is_file():
                    last_error = result.stderr.strip() or "未知错误"
                    temp.unlink(missing_ok=True)
                    # 某些 FFmpeg/MJPEG 版本无法给复杂的 4K 帧分配最高质量档所需的
                    # 单帧缓冲区。继续尝试较低质量或较小尺寸，而不是让整个 prepare 中止。
                    continue

                size = temp.stat().st_size
                if best_size is None or size < best_size:
                    if best_temp:
                        best_temp.unlink(missing_ok=True)
                    best_temp, best_size = temp, size
                else:
                    temp.unlink(missing_ok=True)

                if size < max_bytes:
                    best_temp.replace(output_path)
                    return size

        if best_temp is None or best_size is None:
            raise RuntimeError(f"截图编码失败：{last_error or '没有生成有效截图'}")
        best_temp.replace(output_path)
        print(f"警告：已尽量压缩，但仍为 {best_size / 1024:.1f} KB")
        return best_size
    finally:
        if best_temp and best_temp.exists():
            best_temp.unlink(missing_ok=True)


def extract_screenshots(
    video_path: Path,
    output_dir: Path,
    count: int = 4,
    trim_ratio: float = 0.05,
    seed: int | None = None,
    output_format: str = "jpg",
    max_size_kb: int = DEFAULT_MAX_SIZE_KB,
    max_width: int = 3840,
) -> None:
    if not video_path.is_file():
        raise FileNotFoundError(f"找不到视频文件：{video_path}")
    if count < 1:
        raise ValueError("截图数量必须大于 0")
    if not 0 <= trim_ratio < 0.5:
        raise ValueError("trim_ratio 必须在 0 到 0.5 之间")
    if output_format not in {"avif", "jpg", "png"}:
        raise ValueError("输出格式只能是 avif、jpg 或 png")
    if max_size_kb < 1:
        raise ValueError("max_size_kb 必须大于 0")
    if max_width < 2:
        raise ValueError("max_width 必须大于 1")

    ffmpeg, ffprobe = _find_tools()
    info = _probe_video(ffprobe, video_path)
    duration = info["duration"]
    start = duration * trim_ratio
    end = duration * (1 - trim_ratio)
    rng = random.Random(seed)
    timestamps = sorted(rng.uniform(start, end) for _ in range(count))

    output_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = max_size_kb * 1024
    stem = video_path.stem

    for index, timestamp in enumerate(timestamps, start=1):
        output_path = output_dir / f"{stem}_{index:02d}_{timestamp:.1f}s.{output_format}"
        size = _encode_until_small(
            ffmpeg=ffmpeg,
            video_path=video_path,
            output_path=output_path,
            timestamp=timestamp,
            info=info,
            output_format=output_format,
            max_bytes=max_bytes,
            max_width=max_width,
        )
        print(f"已保存：{output_path} ({size / 1024:.1f} KB)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="随机提取视频截图，并压缩到指定大小。默认输出经过 HDR→SDR 映射的 JPG。"
    )
    parser.add_argument("video", type=Path, help="视频文件路径")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("screenshots"),
        help="输出目录，默认：screenshots",
    )
    parser.add_argument("-n", "--count", type=int, default=4, help="截图数量，默认：4")
    parser.add_argument(
        "--trim",
        type=float,
        default=0.05,
        help="首尾各排除的比例，默认：0.05（5%%）",
    )
    parser.add_argument(
        "--format",
        choices=("avif", "jpg", "png"),
        default="jpg",
        help="jpg=自动 HDR 转 SDR（默认）；avif=10 位 HDR 且体积小；png=无损但体积较大",
    )
    parser.add_argument(
        "--max-size-kb",
        type=int,
        default=DEFAULT_MAX_SIZE_KB,
        help="单张图片大小上限，默认：500 KB",
    )
    parser.add_argument(
        "--max-width",
        type=int,
        default=3840,
        help="最大图片宽度，默认：3840；压缩不足时会自动缩小",
    )
    parser.add_argument("--seed", type=int, help="随机种子；指定后可重复得到相同截图")
    args = parser.parse_args()

    extract_screenshots(
        video_path=args.video,
        output_dir=args.output,
        count=args.count,
        trim_ratio=args.trim,
        seed=args.seed,
        output_format=args.format,
        max_size_kb=args.max_size_kb,
        max_width=args.max_width,
    )


if __name__ == "__main__":
    main()
