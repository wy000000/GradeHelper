"""vllm 图片批量识别预处理：描述嵌入 MD 并另存 …-Pn.txt。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .config import EndpointConfig
from .llm_client import LLMClient
from .models import ParsedReport, ReportImage

logger = logging.getLogger(__name__)

CAPTION_PROMPT = (
    "请详细描述这张课程报告插图的内容：图的类型（拓扑图/截图/表格/流程图/示意图等）、"
    "核心元素与文字标注、技术上的合理性线索。只输出描述文字，不要任何前缀。"
)


def caption_images(reports: list[ParsedReport], cfg: EndpointConfig) -> int:
    """对所有报告的所有图片并发批量识别，结果写入 ReportImage.caption。

    返回成功识别的图片数量。图片内容与正文的相关性、合理性判定交给后续 LLM 评分项。
    """
    client = LLMClient(cfg)
    tasks: list[tuple[ParsedReport, ReportImage]] = [
        (report, image) for report in reports for image in report.images
    ]
    if not tasks:
        return 0

    logger.info("开始 vllm 图片识别：%d 张，端点 %s，模型 %s，并发 %d",
                len(tasks), cfg.base_url, cfg.model, cfg.batch_size)
    done = 0

    def _describe(report: ParsedReport, image: ReportImage) -> tuple[str, int, str]:
        return report.student_id, image.index, client.describe_image(image.data, image.ext, CAPTION_PROMPT)

    with ThreadPoolExecutor(max_workers=max(1, cfg.batch_size)) as pool:
        futures = {pool.submit(_describe, r, i): (r, i) for r, i in tasks}
        for future in as_completed(futures):
            report, image = futures[future]
            try:
                _, _, caption = future.result()
                image.caption = caption
                done += 1
            except Exception as exc:  # noqa: BLE001 单张失败不影响其他图片
                logger.error("图片识别失败 %s P%d: %s", report.base_name, image.index, exc)
    logger.info("vllm 图片识别完成：%d/%d 成功", done, len(tasks))
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
