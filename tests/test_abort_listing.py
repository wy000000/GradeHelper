"""中断（KeyboardInterrupt 等）时主 log / 控制台列出出错与未处理文档全名的测试。"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.config import (  # noqa: E402
    AppConfig, EndpointConfig, PathsConfig, PromptsConfig,
)
from gradehelper.excel_io import write_results  # noqa: E402
from gradehelper.grader import Grader  # noqa: E402
from gradehelper.models import (  # noqa: E402
    ItemScore, ParsedReport, ReportImage, RuleItem,
)

_NAME_FORMAT = "{course}-{klass}-{student_id}-{stdName}-{reportName}"
_RESULT_FORMAT = "{course}-{tagDep}-{reportName}-评分结果.xlsx"

# 学号 → （文件全名，姓名）
_DOCS = {
    "2021001": ("云安全-安全2401-2021001-张三-课程报告.md", "张三"),
    "2021002": ("云安全-安全2401-2021002-李四-课程报告.md", "李四"),
    "2021003": ("云安全-安全2401-2021003-王五-课程报告.md", "王五"),
}

SEN_RULES = [
    RuleItem(key="baseScore", default="25", weight=1.0, method="code",
             evaluation="", comment="规则评语"),
    RuleItem(key="event", default="0-15", weight=0.15, method="LLM",
             evaluation="事件描述", comment=""),
]

# pic 阶段只要模板里有 LLM 项就会启用识图
PIC_RULES = [
    RuleItem(key="event", default="0-15", weight=0.15, method="LLM",
             evaluation="事件描述", comment=""),
]

_LLM_SCORES = {"event": ItemScore(key="event", score=12.0, confidence=0.9,
                                  comment="LLM 评语")}


def _llm_config(batch: int, retry_rounds: int) -> AppConfig:
    return AppConfig(
        llm=EndpointConfig(llm_workers=batch, vllm_workers=batch),
        paths=PathsConfig(
            report_name_format=_NAME_FORMAT, report_name_separator="-",
            result_name_format=_RESULT_FORMAT,
            parse_workers=2, retry_rounds=retry_rounds,
        ),
    )


class TestSenAbortListing(unittest.TestCase):
    """sen 阶段中断：主 log 与控制台按「成功+失败+未处理」分类并列出文档全名。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")
        self.result_path = self.tmp / "result.xlsx"
        self.reports = [
            ParsedReport(
                course="云安全", klass="安全2401", student_id=sid,
                std_name=_DOCS[sid][1], report_name="课程报告",
                source_path=str(self.tmp / _DOCS[sid][0]), md_text="正文",
            )
            for sid in ("2021001", "2021002", "2021003")
        ]
        seed = {
            report.student_id: {
                "baseScore": ItemScore(key="baseScore", score=25.0, comment="规则评语"),
                "event": ItemScore(key="event", score=0.0, comment=""),
            }
            for report in self.reports
        }
        write_results(self.result_path, self.reports, SEN_RULES, seed, mode="upsert")

    def tearDown(self):
        self._tmp.cleanup()

    def _grader(self) -> Grader:
        grader = Grader(_llm_config(batch=1, retry_rounds=1))
        grader.result_path_for_test = self.result_path  # type: ignore[attr-defined]
        grader._load_reports_for_pic = lambda *args, **kwargs: list(self.reports)
        return grader

    def test_keyboard_interrupt_lists_error_and_unprocessed_docs(self):
        """中断时（全部=成功+失败+未处理）：失败过一次（仍在重试队列）→ 失败并列入
        「失败文档」；没轮到评分 → 单独计入「未处理」，不并入失败；已完成评分的
        2021001 计为成功。"""

        def fake_grade(_client, report, _llm_rules):
            if report.student_id == "2021001":
                time.sleep(0.2)  # 让已完成的结果先落定
                return _LLM_SCORES
            if report.student_id == "2021002":
                raise RuntimeError("临时失败")
            time.sleep(1.0)  # 中断发生在这份：确保前两份的结局先被记录
            raise KeyboardInterrupt

        grader = self._grader()
        grader._grade_with_llm_batch = fake_grade
        grader._log_path = self.tmp / "sen.log"
        with patch("gradehelper.stages.sen.read_rules", return_value=SEN_RULES), \
                self.assertLogs("gradehelper.grader", level="ERROR") as logs, \
                self.assertRaises(KeyboardInterrupt):
            grader.stage_sen(self.tmp, self.template, output=self.result_path,
                             skip_images=0)

        # 控制台汇总：成功 1 / 失败 1（出错未成功）/ 未处理 1 + 两份名单
        console = "\n".join(logs.output)
        self.assertIn("处理 3 份文档：成功 1 份，失败 1 份，未处理 1 份", console)
        self.assertIn(f"失败文档：\n  - {_DOCS['2021002'][0]}", console)
        self.assertIn(f"未处理文档：\n  - {_DOCS['2021003'][0]}", console)

        # 阶段总结（主 log 的 extra）：三类互斥、总和 = 全部；出错未成功并入失败名单
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("失败文档"), _DOCS["2021002"][0])
        self.assertEqual(entry.extra.get("未处理文档"), _DOCS["2021003"][0])
        self.assertNotIn("出错文档", entry.extra)
        self.assertEqual(entry.extra.get("报告"),
                         "处理 3 份 / 成功 1 份 / 失败 1 份 / 未处理 1 份")
        self.assertEqual((entry.succeeded_count, entry.failed_count), (1, 1))
        self.assertEqual(entry.unprocessed_count, 1)
        self.assertEqual(entry.processed_count, 3)
        self.assertIn("KeyboardInterrupt", entry.error or "")

        # 主 log 文本：三类计数齐全，名单逐行列出，不再有「出错文档」
        log_text = (self.tmp / "sen.log").read_text(encoding="utf-8")
        self.assertIn("成功: 1 份", log_text)
        self.assertIn("失败: 1 份", log_text)
        self.assertIn("未处理: 1 份", log_text)
        self.assertIn(f"失败文档:\n  - {_DOCS['2021002'][0]}", log_text)
        self.assertIn(f"未处理文档:\n  - {_DOCS['2021003'][0]}", log_text)
        self.assertNotIn("出错文档", log_text)


class TestPicAbortListing(unittest.TestCase):
    """pic 阶段中断：按「成功+失败+未处理」分类；已完整识别并回写 md 的计为成功。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")
        for name in _DOCS.values():
            (self.tmp / name[0]).write_text("正文\n", encoding="utf-8")
        self.reports = [
            ParsedReport(
                course="云安全", klass="安全2401", student_id=sid,
                std_name=name[1], report_name="课程报告",
                source_path=str(self.tmp / name[0]),
                md_text=f"正文\n[[PIC:{Path(name[0]).stem}-P3.png]]\n",
                images=[ReportImage(index=3, ext="png", data=b"\x89PNG")],
            )
            for sid, name in _DOCS.items()
        ]

    def tearDown(self):
        self._tmp.cleanup()

    def _grader(self) -> Grader:
        grader = Grader(AppConfig(
            vllm=EndpointConfig(llm_workers=2, vllm_workers=2),
            paths=PathsConfig(
                report_name_format=_NAME_FORMAT, report_name_separator="-",
                result_name_format=_RESULT_FORMAT,
                parse_workers=2, retry_rounds=1,
            ),
            prompts=PromptsConfig(image_context="[图片描述 P{n}: {caption}]"),
        ))
        grader._load_reports_for_pic = lambda *args, **kwargs: list(self.reports)
        return grader

    def test_keyboard_interrupt_lists_error_and_unprocessed_docs(self):
        """中断时（全部=成功+失败+未处理）：2021001 识别失败且未恢复 → 失败并列入
        「失败文档」；2021002 已完整识别并回写 md → 计为成功；2021003 没轮到识别
        → 单独计入「未处理」，不并入失败。"""

        def fake_caption_images(reports, _cfg, _prompt, *, on_error=None,
                                on_batch=None, **_kwargs):
            on_error(reports[0], reports[0].images[0], RuntimeError("识别超时"))
            reports[1].images[0].caption = "已识别的图片描述"
            on_batch([(reports[1], reports[1].images[0])])
            raise KeyboardInterrupt

        grader = self._grader()
        grader._log_path = self.tmp / "pic.log"
        with patch("gradehelper.stages.pic.caption_images", fake_caption_images), \
                patch("gradehelper.stages.pic.read_rules", return_value=PIC_RULES), \
                self.assertLogs("gradehelper.grader", level="ERROR") as logs, \
                self.assertRaises(KeyboardInterrupt):
            grader.stage_pic(self.tmp, self.template, skip_images=0)

        # 控制台汇总：文档成功 1 / 失败 1 / 未处理 1（2021002 完成识别，进成功）
        console = "\n".join(logs.output)
        self.assertIn("处理 3 份文档：成功 1 份，失败 1 份，未处理 1 份", console)
        self.assertIn(f"失败文档：\n  - {_DOCS['2021001'][0]}", console)
        self.assertIn(f"未处理文档：\n  - {_DOCS['2021003'][0]}", console)

        # 阶段总结（主 log 的 extra）：三类互斥、总和 = 全部；识别失败计入文档失败
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("失败文档"), _DOCS["2021001"][0])
        self.assertEqual(entry.extra.get("未处理文档"), _DOCS["2021003"][0])
        self.assertNotIn("出错文档", entry.extra)
        self.assertEqual(entry.extra.get("文档"),
                         "处理 3 份 / 成功 1 份 / 失败 1 份 / 未处理 1 份")
        # 汇总计数含图片条目（文档 3 份 + 图片 2 张）：共 5 条 = 成功 2 + 失败 2 + 未处理 1
        self.assertEqual((entry.succeeded_count, entry.failed_count), (2, 2))
        self.assertEqual(entry.unprocessed_count, 1)
        self.assertEqual(entry.processed_count, 5)
        self.assertEqual(entry.extra.get("图片"), "处理 2 张 / 成功 1 张 / 失败 1 张")
        self.assertEqual(entry.unit, "条")
        self.assertIn("KeyboardInterrupt", entry.error or "")
        # md 已回写并落盘：2021002 的描述已嵌入 md
        self.assertIn("已识别的图片描述",
                      (self.tmp / _DOCS["2021002"][0]).read_text(encoding="utf-8"))

        # 主 log 文本：三类计数齐全，名单逐行列出，不再有「出错文档」
        log_text = (self.tmp / "pic.log").read_text(encoding="utf-8")
        self.assertIn("未处理: 1 条", log_text)
        self.assertIn(f"失败文档:\n  - {_DOCS['2021001'][0]}", log_text)
        self.assertIn(f"未处理文档:\n  - {_DOCS['2021003'][0]}", log_text)
        self.assertNotIn("出错文档", log_text)


if __name__ == "__main__":
    unittest.main()
