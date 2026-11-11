"""pic-e 阶段测试：优先复用图片描述 txt，缺失的图片再交给 vllm。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.config import (  # noqa: E402
    AppConfig, EndpointConfig, PathsConfig, PromptsConfig,
)
from gradehelper.grader import Grader  # noqa: E402
from gradehelper.models import ParsedReport, ReportImage, RuleItem  # noqa: E402

BASE = "云安全-安全2401-{sid}-张三-课程报告"


def _config(batch: int = 2) -> AppConfig:
    return AppConfig(
        llm=EndpointConfig(llm_workers=batch, vllm_workers=batch),
        vllm=EndpointConfig(llm_workers=batch, vllm_workers=batch),
        paths=PathsConfig(
            report_name_format="{course}-{klass}-{student_id}-{stdName}-{reportName}",
            report_name_separator="-",
            result_name_format="{course}-{tagDep}-{reportName}-评分结果.xlsx",
        ),
        prompts=PromptsConfig(image_context="[图片描述 P{n}: {caption}]"),
    )


def _rules() -> list[RuleItem]:
    return [
        RuleItem(key="baseScore", default="25", weight=1.0, method="code",
                 evaluation="", comment="报告已提交，+25分"),
        RuleItem(key="event", default="0-15", weight=0.15, method="LLM",
                 evaluation="事件描述", comment=""),
    ]


def _report(tmp: Path, indices: tuple[int, ...] = (3, 4),
            sid: str = "2021001") -> ParsedReport:
    """构造一份带多个图片占位符的报告（md 已落盘）。"""
    base = BASE.format(sid=sid)
    md_path = tmp / f"{base}.md"
    placeholders = "".join(f"[[PIC:{base}-P{index}.png]]\n" for index in indices)
    md_text = f"正文\n{placeholders}"
    md_path.write_text(md_text, encoding="utf-8")
    return ParsedReport(
        course="云安全", klass="安全2401", student_id=sid, std_name="张三",
        report_name="课程报告", source_path=str(md_path), md_text=md_text,
        images=[ReportImage(index=index, ext="png", data=b"\x89PNG")
                for index in indices],
    )


def _write_txt(report: ParsedReport, index: int, text: str,
               directory: Path | None = None) -> Path:
    target = directory or Path(report.source_path).parent
    path = target / f"{report.base_name}-P{index}.txt"
    path.write_text(text, encoding="utf-8")
    return path


class TestPicEStage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _fake_caption_images(sent: list[int]):
        """替身 vllm：记录被送去识别的图片编号，返回固定描述。"""
        def fake(_reports, _cfg, _prompt, cancel=None, on_error=None, on_batch=None):
            batch: list[tuple[ParsedReport, ReportImage]] = []
            for report in _reports:
                for image in report.images:
                    image.caption = f"vllm 描述 P{image.index}"
                    sent.append(image.index)
                    batch.append((report, image))
            if batch and on_batch is not None:
                on_batch(batch)
            return len(batch)
        return fake

    def _run(self, grader: Grader, sent: list[int],
             rules: list[RuleItem] | None = None):
        with patch("gradehelper.stages.pic.caption_images",
                   self._fake_caption_images(sent)), \
                patch("gradehelper.stages.pic.read_rules",
                      return_value=rules if rules is not None else _rules()):
            grader.stage_pic_e(self.tmp, self.template)

    def test_reuses_existing_txt_and_sends_missing_to_vllm(self):
        report = _report(self.tmp)
        _write_txt(report, 3, "已有描述 P3")
        sent: list[int] = []
        grader = Grader(_config())
        grader._load_reports_for_pic = lambda *args, **kwargs: [report]

        self._run(grader, sent)

        # 只有缺 txt 的 P4 进入 vllm
        self.assertEqual(sent, [4])
        md = Path(report.source_path).read_text(encoding="utf-8")
        self.assertIn("已有描述 P3", md)
        self.assertIn("vllm 描述 P4", md)
        self.assertNotIn("vllm 描述 P3", md)
        self.assertEqual(md.count("已有描述 P3"), 1)

        summary = grader.stage_logs[-1]
        self.assertEqual(summary.stage, "pic-e")
        self.assertEqual(summary.extra.get("描述来源"), "复用 txt 1 张 / vllm 1 张")
        self.assertEqual(summary.extra.get("图片"), "处理 2 张 / 成功 2 张 / 失败 0 张")

    def test_without_txt_behaves_like_pic(self):
        report = _report(self.tmp)
        sent: list[int] = []
        grader = Grader(_config())
        grader._load_reports_for_pic = lambda *args, **kwargs: [report]

        self._run(grader, sent)

        self.assertEqual(sent, [3, 4])
        md = Path(report.source_path).read_text(encoding="utf-8")
        self.assertIn("vllm 描述 P3", md)
        self.assertIn("vllm 描述 P4", md)
        self.assertEqual(grader.stage_logs[-1].extra.get("描述来源"),
                         "复用 txt 0 张 / vllm 2 张")

    def test_empty_txt_falls_back_to_vllm(self):
        report = _report(self.tmp, indices=(3,))
        _write_txt(report, 3, "   \n")
        sent: list[int] = []
        grader = Grader(_config())
        grader._load_reports_for_pic = lambda *args, **kwargs: [report]

        self._run(grader, sent)

        self.assertEqual(sent, [3])
        md = Path(report.source_path).read_text(encoding="utf-8")
        self.assertIn("vllm 描述 P3", md)

    def test_reads_txt_from_captions_dir(self):
        report = _report(self.tmp, indices=(3,))
        captions_dir = self.tmp / "captions"
        captions_dir.mkdir()
        _write_txt(report, 3, "目录里的描述", captions_dir)
        sent: list[int] = []
        grader = Grader(_config())
        grader.config.paths.captions_dir = str(captions_dir)
        grader._load_reports_for_pic = lambda *args, **kwargs: [report]

        self._run(grader, sent)

        self.assertEqual(sent, [])
        md = Path(report.source_path).read_text(encoding="utf-8")
        self.assertIn("目录里的描述", md)
        self.assertEqual(grader.stage_logs[-1].extra.get("描述来源"),
                         "复用 txt 1 张 / vllm 0 张")

    def test_incremental_skips_images_already_described_in_md(self):
        report = _report(self.tmp, indices=(3,))
        _write_txt(report, 3, "已有 txt 描述")
        # md 中已有该图片的描述块：增量模式应整张跳过，不读 txt 也不调用 vllm
        md_path = Path(report.source_path)
        md_path.write_text(
            f"正文\n[[PIC:{report.base_name}-P3.png]]\n[图片描述 P3: md 内旧描述]\n",
            encoding="utf-8")
        report.md_text = md_path.read_text(encoding="utf-8")

        sent: list[int] = []
        grader = Grader(_config())
        grader.incremental = True
        grader._load_reports_for_pic = lambda *args, **kwargs: [report]

        self._run(grader, sent)

        self.assertEqual(sent, [])
        md = md_path.read_text(encoding="utf-8")
        self.assertIn("md 内旧描述", md)
        self.assertNotIn("已有 txt 描述", md)
        self.assertEqual(grader.stage_logs[-1].extra.get("增量跳过"),
                         "1 张已有描述图片（1 份报告）")

    def test_reuses_txt_even_without_llm_rules(self):
        report = _report(self.tmp, indices=(3,))
        _write_txt(report, 3, "无 LLM 项也要写入")
        sent: list[int] = []
        grader = Grader(_config())
        grader._load_reports_for_pic = lambda *args, **kwargs: [report]

        self._run(grader, sent, rules=[
            RuleItem(key="baseScore", default="25", weight=1.0, method="code",
                     evaluation="", comment=""),
        ])

        self.assertEqual(sent, [])
        md = Path(report.source_path).read_text(encoding="utf-8")
        self.assertIn("无 LLM 项也要写入", md)


class TestPicECli(unittest.TestCase):
    def test_stage_choice_accepts_pic_e(self):
        from gradehelper.__main__ import build_parser
        args = build_parser().parse_args(
            ["--stage", "pic-e", "--template", "template.xlsx"])
        self.assertEqual(args.stage, "pic-e")

    def test_default_stage_is_full_e(self):
        from gradehelper.__main__ import build_parser
        args = build_parser().parse_args(["--template", "template.xlsx"])
        self.assertEqual(args.stage, "full-e")


class TestFullEDispatch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _run_stage(self, stage: str) -> list[str]:
        grader = Grader(_config())
        calls: list[str] = []
        grader.stage_doc = lambda *args, **kwargs: calls.append("doc")
        grader.stage_pic = lambda *args, **kwargs: calls.append("pic")
        grader.stage_pic_e = lambda *args, **kwargs: calls.append("pic-e")
        grader.stage_sen = lambda *args, **kwargs: calls.append("sen")
        with patch.object(grader, "_resolve_log_path", return_value=None), \
                patch.object(grader, "_append_run_header"):
            grader.run(reports_dir=str(self.tmp), template="template.xlsx",
                       stage=stage)
        return calls

    def test_full_e_uses_pic_e(self):
        self.assertEqual(self._run_stage("full-e"), ["doc", "pic-e", "sen"])

    def test_full_keeps_using_pic(self):
        self.assertEqual(self._run_stage("full"), ["doc", "pic", "sen"])


if __name__ == "__main__":
    unittest.main()
