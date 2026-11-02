"""vllm 图片批量识别预处理：描述嵌入 MD 并另存 …-Pn.txt。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .cancel import CancelToken, LLMAborted
from .config import EndpointConfig
from .llm_client import LLMClient
from .models import ParsedReport, ReportImage
from .progress import ParallelProgress

logger = logging.getLogger(__name__)


def caption_images(reports: list[ParsedReport], cfg: EndpointConfig, image_prompt: str,
                   cancel: CancelToken | None = None,
                   on_error: Callable[[ParsedReport, ReportImage, Exception], None] | None = None,
                   ) -> int:
    """对所有报告的所有图片并发批量识别，结果写入 ReportImage.caption。

    返回成功识别的图片数量。图片内容与正文的相关性、合理性判定交给后续 LLM 评分项。
    image_prompt 会原样作为文本消息提交给 /chat/completions API。
    cancel 用于中断：置位后关闭在飞连接，让服务端停止生成。
    on_error 在单张图片识别失败时调用，便于记录到具体文档的运行日志。
    """
    client = LLMClient(cfg, cancel=cancel)
    tasks: list[tuple[ParsedReport, ReportImage]] = [
        (report, image) for report in reports for image in report.images
    ]
    if not tasks:
        return 0

    logger.info("开始 vllm 图片识别：%d 张，端点 %s，模型 %s，并发 %d",
                len(tasks), cfg.base_url, cfg.get_api_param("model"), cfg.batch_size)
    done = 0
    progress = ParallelProgress("pic", len(tasks), unit="张",
                                workers=max(1, cfg.batch_size))

    def _describe(report: ParsedReport, image: ReportImage) -> tuple[str, int, str]:
        label = f"{report.student_id}-P{image.index}"
        with progress.task(label):
            return (report.student_id, image.index,
                    client.describe_image(image.data, image.ext, image_prompt))

    with progress:
        pool = ThreadPoolExecutor(max_workers=max(1, cfg.batch_size))
        try:
            futures = {pool.submit(_describe, r, i): (r, i) for r, i in tasks}
            for future in as_completed(futures):
                report, image = futures[future]
                try:
                    _, _, caption = future.result()
                    image.caption = caption
                    done += 1
                except LLMAborted:
                    raise  # 用户取消：不吞，向上传播
                except Exception as exc:  # noqa: BLE001 单张失败不影响其他图片
                    logger.error("图片识别失败 %s P%d: %s", report.base_name, image.index, exc)
                    if on_error is not None:
                        try:
                            on_error(report, image, exc)
                        except Exception:  # noqa: BLE001 记录失败不应影响其他图片
                            logger.debug("图片失败回调异常", exc_info=True)
        except BaseException:
            # 取消后丢弃未开始的任务；在飞的连接已由 cancel() 关闭，无需等待
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
    logger.debug("vllm 图片识别完成：%d/%d 成功", done, len(tasks))
    return done


def save_captions_txt(reports: list[ParsedReport], captions_dir: str | Path | None = None) -> int:
    """把每张图片的描述另存为 '课程名称-专业班级-学号-姓名-Pn.txt'。

    captions_dir 为空时保存到与原报告相同路径。返回写入的文件数。
    """
    written = 0
    for report in reports:
        target_dir = Path(captions_dir) if captions_dir else Path(report.source_path).parent
        target_dir.mkdir(parents=True, exist_ok=True)
        for image in report.images:
            if not image.caption:
                continue
            txt_path = target_dir / f"{report.base_name}-P{image.index}.txt"
            txt_path.write_text(image.caption, encoding="utf-8")
            written += 1
    return written