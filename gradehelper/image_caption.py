"""vllm 图片批量识别预处理：描述嵌入 MD 并另存 …-Pn.txt。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
                   on_batch: Callable[[list[tuple[ParsedReport, ReportImage]]], None] | None = None,
                   ) -> int:
    """对所有报告的所有图片并发批量识别，结果写入 ReportImage.caption。

    返回成功识别的图片数量。图片内容与正文的相关性、合理性判定交给后续 LLM 评分项。
    image_prompt 会原样作为文本消息提交给 /chat/completions API。
    cancel 用于中断：置位后关闭在飞连接，让服务端停止生成。
    on_error 在单张图片识别失败时调用，便于记录到具体文档的运行日志。
    on_batch 每完成 cfg.batch_size 张图片（以及全部结束时）回调一次，参数为本批
    已完成识别的 (报告, 图片) 列表，用于实时回写描述；回调异常不影响识图。
    """
    client = LLMClient(cfg, cancel=cancel)
    tasks: list[tuple[ParsedReport, ReportImage]] = [
        (report, image) for report in reports for image in report.images
    ]
    if not tasks:
        return 0

    logger.info("开始 vllm 图片识别：%d 张，端点 %s，模型 %s，并发 %d",
                len(tasks), cfg.base_url, cfg.get_model(), cfg.batch_size)
    done = 0
    flush_every = max(1, cfg.batch_size)
    pending: list[tuple[ParsedReport, ReportImage]] = []

    def _flush_batch() -> None:
        """把本批已完成识别的图片交给回调实时回写（回调异常不影响识图）。"""
        if not pending:
            return
        items = list(pending)
        pending.clear()
        if on_batch is None:
            return
        try:
            on_batch(items)
        except Exception:  # noqa: BLE001 回写失败不影响识图
            logger.error("图片描述实时回写失败", exc_info=True)

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
            remaining = dict(futures)
            while remaining:
                completed, _ = wait(remaining, timeout=0.5, return_when=FIRST_COMPLETED)
                if not completed:
                    continue
                for future in completed:
                    report, image = remaining.pop(future)
                    try:
                        _, _, caption = future.result()
                        image.caption = caption
                        done += 1
                        pending.append((report, image))
                        if len(pending) >= flush_every:
                            _flush_batch()
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
        finally:
            _flush_batch()  # 收尾：不足一批的图片也要回写
    logger.debug("vllm 图片识别完成：%d/%d 成功", done, len(tasks))
    return done


def save_caption_txt(report: ParsedReport, image: ReportImage,
                     captions_dir: str | Path | None = None) -> Path | None:
    """把单张图片的描述另存为 '课程名称-专业班级-学号-姓名-Pn.txt'。

    captions_dir 为空时保存到与原报告相同路径。无描述时返回 None。
    """
    if not image.caption:
        return None
    target_dir = Path(captions_dir) if captions_dir else Path(report.source_path).parent
    target_dir.mkdir(parents=True, exist_ok=True)
    txt_path = target_dir / f"{report.base_name}-P{image.index}.txt"
    txt_path.write_text(image.caption, encoding="utf-8")
    return txt_path


def save_captions_txt(reports: list[ParsedReport], captions_dir: str | Path | None = None) -> int:
    """把每张图片的描述另存为 '课程名称-专业班级-学号-姓名-Pn.txt'。

    captions_dir 为空时保存到与原报告相同路径。返回写入的文件数。
    """
    written = 0
    for report in reports:
        for image in report.images:
            if save_caption_txt(report, image, captions_dir) is not None:
                written += 1
    return written