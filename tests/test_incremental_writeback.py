"""实时回写测试：pic 每张图片回写描述与 md，sen 每份报告回写结果表。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper import excel_io  # noqa: E402
from gradehelper.config import (  # noqa: E402
    AppConfig, EndpointConfig, PathsConfig, PromptsConfig,
)
from gradehelper.excel_io import read_result_items, write_results  # noqa: E402
from gradehelper.grader import Grader  # noqa: E402
from gradehelper.image_caption import caption_images  # noqa: E402
from gradehelper.image_caption import save_caption_txt as real_save_caption_txt  # noqa: E402
from gradehelper.models import ItemScore, ParsedReport, ReportImage, RuleItem  # noqa: E402

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


def _pic_report(tmp: Path, sid: str = "2021001", index: int = 3) -> ParsedReport:
    """构造一份带图片占位符的报告（md 已落盘，供 pic 阶段回写）。"""
    base = BASE.format(sid=sid)
    md_path = tmp / f"{base}.md"
    md_text = f"正文\n[[PIC:{base}-P{index}.png]]\n"
    md_path.write_text(md_text, encoding="utf-8")
    return ParsedReport(
        course="云安全", klass="安全2401", student_id=sid, std_name="张三",
        report_name="课程报告", source_path=str(md_path), md_text=md_text,
        images=[ReportImage(index=index, ext="png", data=b"\x89PNG")],
    )


def _md_report(sid: str = "2021001") -> ParsedReport:
    return ParsedReport(
        course="云安全", klass="安全2401", student_id=sid, std_name="张三",
        report_name="课程报告", source_path=f"{BASE.format(sid=sid)}.md", md_text="正文",
    )


class _FakeClient:
    """替身 vllm 客户端：识图立即返回固定描述。"""

    def __init__(self, *_args, **_kwargs):
        pass

    def describe_image(self, _data, _ext, _prompt) -> str:
        return "自动描述"


class TestCaptionBatchCallback(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_on_batch_called_per_image(self):
        reports = [_pic_report(self.tmp, sid) for sid in ("2021001", "2021002", "2021003")]
        batches: list[int] = []
        with patch("gradehelper.image_caption.LLMClient", _FakeClient):
            done = caption_images(reports, EndpointConfig(vllm_workers=2), "prompt",
                                  on_batch=lambda items: batches.append(len(items)))
        self.assertEqual(done, 3)
        self.assertEqual(batches, [1, 1, 1])  # 每张识别完成立即回调，不分批
        self.assertEqual(reports[0].images[0].caption, "自动描述")

    def test_without_callback_still_captions(self):
        reports = [_pic_report(self.tmp)]
        with patch("gradehelper.image_caption.LLMClient", _FakeClient):
            self.assertEqual(caption_images(reports, EndpointConfig(vllm_workers=1), "prompt"), 1)
        self.assertEqual(reports[0].images[0].caption, "自动描述")



class TestPicStageWritesBackPerImage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")

    def tearDown(self):
        self._tmp.cleanup()

    def test_txt_and_md_written_before_stage_ends(self):
        reports = [_pic_report(self.tmp, sid) for sid in ("2021001", "2021002", "2021003")]
        grader = Grader(_config(batch=2))
        grader._load_reports_for_pic = lambda *args, **kwargs: reports
        flushes: list[list[str]] = []
        md_seen_at_first_flush: list[str] = []

        def fake_caption_images(_reports, _cfg, _prompt, cancel=None, on_error=None,
                                on_batch=None, **_kwargs):
            batch: list[tuple[ParsedReport, ReportImage]] = []
            done = 0
            for report in _reports:
                for image in report.images:
                    image.caption = f"图{image.index}描述"
                    batch.append((report, image))
                    done += 1
                    if on_batch is not None and len(batch) >= 2:
                        on_batch(batch)
                        flushes.append([item[0].student_id for item in batch])
                        if len(flushes) == 1:
                            md_seen_at_first_flush.append(
                                Path(report.source_path).read_text(encoding="utf-8"))
                        batch = []
            if batch and on_batch is not None:
                on_batch(batch)
                flushes.append([item[0].student_id for item in batch])
            return done

        with patch("gradehelper.stages.pic.caption_images", fake_caption_images), \
                patch("gradehelper.stages.pic.read_rules", return_value=_rules()):
            grader.stage_pic(self.tmp, self.template)

        self.assertEqual([len(item) for item in flushes], [2, 1])
        # 第一批回写时 md 里已经有图片描述（不是等阶段结束才写）
        self.assertIn("图3描述", md_seen_at_first_flush[0])
        for report in reports:
            md = Path(report.source_path).read_text(encoding="utf-8")
            self.assertEqual(md.count("图3描述"), 1)  # 多次回写不重复嵌入
            txt = self.tmp / f"{report.base_name}-P3.txt"
            self.assertEqual(txt.read_text(encoding="utf-8"), "图3描述")


class TestPicWriteFailureRecorded(unittest.TestCase):
    """写盘失败逐项记录：单项失败不影响同批/其他报告的回写。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _fake_caption_images(_reports, _cfg, _prompt, cancel=None, on_error=None,
                             on_batch=None, **_kwargs):
        """替身识图：每 2 张回调一次（模拟旧版分批行为，验证阶段侧按列表回写）。"""
        batch: list[tuple[ParsedReport, ReportImage]] = []
        done = 0
        for report in _reports:
            for image in report.images:
                image.caption = f"图{image.index}描述"
                batch.append((report, image))
                done += 1
                if on_batch is not None and len(batch) >= 2:
                    on_batch(batch)
                    batch = []
        if batch and on_batch is not None:
            on_batch(batch)
        return done

    @staticmethod
    def _summary_extra(grader: Grader) -> dict[str, str]:
        """取 pic 阶段汇总日志（带“文档”计数的那条）的 extra。"""
        entries = [entry for entry in grader.stage_logs
                   if entry.stage == "pic" and "文档" in entry.extra]
        assert entries, "缺少 pic 阶段汇总日志"
        return entries[-1].extra

    def test_txt_write_failure_recorded_and_others_continue(self):
        reports = [_pic_report(self.tmp, sid) for sid in ("2021001", "2021002", "2021003")]
        bad = reports[1]
        grader = Grader(_config(batch=2))
        grader.config.vllm.base_url = "http://vllm.test"
        grader.config.vllm.model = "vlm-test"
        grader._load_reports_for_pic = lambda *args, **kwargs: reports

        def fake_save(report, image, captions_dir=None):
            if report.student_id == bad.student_id:
                raise OSError("磁盘已满")
            return real_save_caption_txt(report, image, captions_dir)

        with patch("gradehelper.stages.pic.caption_images", self._fake_caption_images), \
                patch("gradehelper.stages.pic.save_caption_txt", fake_save), \
                patch("gradehelper.stages.pic.read_rules", return_value=_rules()), \
                self.assertLogs("gradehelper.grader", level="ERROR") as logs:
            grader.stage_pic(self.tmp, self.template)

        # 只有失败的那一张缺 txt，同批/其它报告照常回写
        self.assertFalse((self.tmp / f"{bad.base_name}-P3.txt").exists())
        for report in (reports[0], reports[2]):
            txt = self.tmp / f"{report.base_name}-P3.txt"
            self.assertEqual(txt.read_text(encoding="utf-8"), "图3描述")
        # txt 失败不影响 md 回写
        for report in reports:
            md = Path(report.source_path).read_text(encoding="utf-8")
            self.assertIn("图3描述", md)
            self.assertEqual(md.count("图3描述"), 1)
        # 中途不再写单独的失败记录；文档名登记在阶段总结的「失败文档」
        self.assertFalse(any("描述写盘失败" in (entry.error or "")
                             for entry in grader.stage_logs))
        # 阶段汇总：计数 + 提示可重跑
        summary = self._summary_extra(grader)
        self.assertEqual(summary.get("写盘失败"), "1 张 txt / 0 份 md")
        self.assertIn(Path(bad.source_path).name, summary.get("失败文档", ""))
        self.assertEqual(summary.get("base_url"), "http://vllm.test")
        self.assertEqual(summary.get("model"), "vlm-test")
        self.assertEqual(summary.get("文档"), "处理 3 份 / 成功 3 份 / 失败 0 份")
        self.assertEqual(summary.get("图片"), "处理 3 张 / 成功 2 张 / 失败 1 张")
        self.assertEqual(grader.stage_logs[-1].failed_count, 1)
        self.assertTrue(any("描述写盘失败" in line for line in logs.output))
        self.assertTrue(any("可重跑 --stage pic 补齐" in line for line in logs.output))

    def test_md_write_failure_recorded(self):
        reports = [_pic_report(self.tmp, sid) for sid in ("2021001", "2021002")]
        bad = reports[0]
        md_path = Path(bad.source_path)
        md_path.unlink()
        md_path.mkdir()  # 目录占位：写 md 必定失败

        grader = Grader(_config(batch=2))
        grader._load_reports_for_pic = lambda *args, **kwargs: reports
        with patch("gradehelper.stages.pic.caption_images", self._fake_caption_images), \
                patch("gradehelper.stages.pic.read_rules", return_value=_rules()):
            grader.stage_pic(self.tmp, self.template)

        # 同批另一份报告 md 正常写入
        self.assertIn("图3描述",
                      Path(reports[1].source_path).read_text(encoding="utf-8"))
        # 中途不再写单独的失败记录；文档名登记在阶段总结的「失败文档」
        self.assertFalse(any("md 回写失败" in (entry.error or "")
                             for entry in grader.stage_logs))
        summary = self._summary_extra(grader)
        self.assertEqual(summary.get("写盘失败"), "0 张 txt / 1 份 md")
        self.assertIn(Path(bad.source_path).name, summary.get("失败文档", ""))


class TestSenCheckpointWriteback(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")
        self.result_path = self.tmp / "云安全-系部专业-课程报告-评分结果.xlsx"
        self.rules = _rules()
        self.reports = [_md_report(sid) for sid in ("2021001", "2021002", "2021003", "2021004")]
        seed = {
            report.student_id: {
                "baseScore": ItemScore(key="baseScore", score=25.0, comment="规则评语"),
                "event": ItemScore(key="event", score=0.0, comment=""),
            }
            for report in self.reports
        }
        write_results(self.result_path, self.reports, self.rules, seed, mode="upsert")

    def tearDown(self):
        self._tmp.cleanup()

    def test_sen_writes_back_every_report(self):
        grader = Grader(_config(batch=2))
        grader.config.llm.base_url = "http://llm.test"
        grader.config.llm.model = "llm-test"
        grader._load_reports_for_pic = lambda *args, **kwargs: list(self.reports)
        grader._grade_with_llm_batch = lambda client, report, llm_rules: {
            "event": ItemScore(key="event", score=12.0, confidence=0.9, comment="LLM 评语")
        }
        recorded: list[int] = []
        real_write = excel_io.write_results_atomic

        def _record(output_path, reports, rules, scores, mode="upsert", claim=None, **kwargs):
            recorded.append(len(reports))
            return real_write(output_path, reports, rules, scores, mode=mode,
                              claim=claim, **kwargs)

        with patch("gradehelper.stages.sen.write_results_atomic", _record), \
                patch("gradehelper.stages.sen.read_rules", return_value=self.rules):
            out = grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                   skip_images=0)

        # 每份完成时立即回写（1、2、3、4 份），最后汇总再写一次
        self.assertEqual(recorded, [1, 2, 3, 4, 4])
        self.assertEqual(out, self.result_path)
        items = read_result_items(self.result_path)
        for report in self.reports:
            self.assertEqual(items[report.student_id]["event"].score, 12.0)
            self.assertEqual(items[report.student_id]["baseScore"].comment, "规则评语")
        self.assertEqual(list(self.tmp.glob("*.tmp*")), [])
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.extra.get("base_url"), "http://llm.test")
        self.assertEqual(entry.extra.get("model"), "llm-test")
        self.assertEqual(entry.extra.get("报告"), "处理 4 份 / 成功 4 份 / 失败 0 份")
        self.assertEqual(entry.failed_count, 0)

    def test_truncated_json_is_failure_with_id_and_name(self):
        grader = Grader(_config(batch=2))
        grader._load_reports_for_pic = lambda *args, **kwargs: list(self.reports)

        def fake_grade(_client, report, _llm_rules):
            if report.student_id == "2021002":
                raise ValueError("无法从 LLM 输出中解析 JSON（长度=100）")
            return {"event": ItemScore(key="event", score=12.0, confidence=0.9,
                                       comment="LLM 评语")}

        grader._grade_with_llm_batch = fake_grade
        with patch("gradehelper.stages.sen.read_rules", return_value=self.rules), \
                self.assertLogs("gradehelper.grader", level="INFO") as logs:
            out = grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                   skip_images=0)

        self.assertEqual(out, self.result_path)
        items = read_result_items(self.result_path)
        self.assertEqual(items["2021002"]["event"].score, 0.0)  # 未评分，保留原值
        doc_name = "云安全-安全2401-2021002-张三-课程报告.md"
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.failed_count, 1)
        self.assertEqual(entry.extra.get("失败文档"), doc_name)
        # 中途不再为失败文档单独写记录；文档名出现在总结的标题与「失败文档」里
        self.assertFalse(any(item.error for item in grader.stage_logs))
        self.assertEqual(entry.document, doc_name)
        self.assertIn(doc_name, entry.title)
        console = "\n".join(logs.output)
        self.assertIn("处理 4 份文档：成功 3 份，失败 1 份", console)
        self.assertIn(f"失败文档：\n  - {doc_name}", console)


class TestSenPreflight(unittest.TestCase):
    """sen 前置校验：表头不匹配仍报错退出；缺行报告记错误跳过、其余继续评分。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")
        self.result_path = self.tmp / "云安全-系部专业-课程报告-评分结果.xlsx"
        self.rules = _rules()

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def _seed(path: Path, reports: list[ParsedReport], rules: list[RuleItem]) -> None:
        write_results(path, reports, rules, {
            report.student_id: {
                "baseScore": ItemScore(key="baseScore", score=25.0, comment="规则评语"),
                "event": ItemScore(key="event", score=0.0, comment=""),
            } for report in reports
        }, mode="upsert")

    def test_missing_student_ids_skipped_while_others_graded(self):
        """缺行报告记错误跳过（不退出），其余报告继续 LLM 评分并写回。"""
        reports = [_md_report(sid) for sid in ("2021001", "2021002", "2021003")]
        self._seed(self.result_path, reports[:2], self.rules)  # 第三份在 result 表里没有行

        graded: list[str] = []

        def fake_grade(_client, report, _llm_rules):
            graded.append(report.student_id)
            return {"event": ItemScore(key="event", score=11.0, confidence=0.9)}

        grader = Grader(_config(batch=2))
        grader._log_path = self.tmp / "run.log"
        grader._load_reports_for_pic = lambda *args, **kwargs: list(reports)
        grader._grade_with_llm_batch = fake_grade
        with patch("gradehelper.stages.sen.read_rules", return_value=self.rules):
            out = grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                   skip_images=0)

        # 不抛异常；缺行的 2021003 不提交 LLM，其余两份正常评分写回
        self.assertEqual(out, self.result_path)
        self.assertEqual(set(graded), {"2021001", "2021002"})
        items = read_result_items(self.result_path)
        self.assertEqual(set(items), {"2021001", "2021002"})
        self.assertEqual(items["2021001"]["event"].score, 11.0)
        self.assertEqual(items["2021002"]["event"].score, 11.0)
        # 错误信息写入运行日志，阶段总结标记失败份数
        log_text = grader._log_path.read_text(encoding="utf-8")
        self.assertIn("学号不在结果文件 result 表中", log_text)
        self.assertIn("2021003", log_text)
        entry = grader.stage_logs[-1]
        self.assertEqual(entry.failed_count, 1)
        self.assertEqual(entry.extra.get("学号缺失"), "1 份（不在 result 表，已跳过）")
        self.assertEqual(entry.extra.get("报告"), "处理 3 份 / 成功 2 份 / 失败 1 份")
        # 结果文件未新增 2021003 行，也没有残留临时文件
        self.assertEqual(list(self.tmp.glob("*.tmp*")), [])

    def test_no_llm_rules_skips_result_rewrite(self):
        rules = [rule for rule in self.rules if not rule.is_llm]
        reports = [_md_report("2021001")]
        write_results(self.result_path, reports, rules, {
            "2021001": {"baseScore": ItemScore(key="baseScore", score=25.0,
                                               comment="doc 阶段规则评语")},
        }, mode="upsert")
        before = self.result_path.read_bytes()

        grader = Grader(_config(batch=2))
        grader._load_reports_for_pic = lambda *args, **kwargs: list(reports)
        with patch("gradehelper.stages.sen.read_rules", return_value=rules), \
                patch("gradehelper.stages.sen.write_results_atomic") as write_mock:
            out = grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                   skip_images=0)

        write_mock.assert_not_called()  # 分数没有变化，不做冗余写
        self.assertEqual(out, self.result_path)
        self.assertEqual(self.result_path.read_bytes(), before)
        self.assertEqual(read_result_items(self.result_path)["2021001"]["baseScore"].comment,
                         "doc 阶段规则评语")


class TestSenCheckpointRetry(unittest.TestCase):
    """实时回写失败不再永久放弃：冷却约一批的量后继续尝试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.template = self.tmp / "template.xlsx"
        self.template.write_bytes(b"")
        self.result_path = self.tmp / "云安全-系部专业-课程报告-评分结果.xlsx"
        self.rules = _rules()
        self.reports = [_md_report(sid) for sid in ("2021001", "2021002", "2021003", "2021004")]
        write_results(self.result_path, self.reports, self.rules, {
            report.student_id: {
                "baseScore": ItemScore(key="baseScore", score=25.0, comment="规则评语"),
                "event": ItemScore(key="event", score=0.0, comment=""),
            } for report in self.reports
        }, mode="upsert")

    def tearDown(self):
        self._tmp.cleanup()

    def test_failed_checkpoint_cools_down_then_retries(self):
        grader = Grader(_config(batch=2))
        grader._load_reports_for_pic = lambda *args, **kwargs: list(self.reports)
        grader._grade_with_llm_batch = lambda client, report, llm_rules: {
            "event": ItemScore(key="event", score=12.0, confidence=0.9, comment="LLM 评语")
        }
        calls: list[int] = []
        real_write = excel_io.write_results_atomic

        def _flaky(output_path, reports, rules, scores, mode="upsert", claim=None, **kwargs):
            calls.append(len(reports))
            if len(calls) == 1:
                raise RuntimeError("模拟结果文件被占用")
            return real_write(output_path, reports, rules, scores, mode=mode,
                              claim=claim, **kwargs)

        with patch("gradehelper.stages.sen.write_results_atomic", _flaky), \
                patch("gradehelper.stages.sen.read_rules", return_value=self.rules), \
                self.assertLogs("gradehelper.grader", level="WARNING"):
            out = grader.stage_sen(self.tmp, self.template, output=self.result_path,
                                   skip_images=0)

        # 第 1 份完成时回写失败 → 冷却 2 份（约一批的量）后第 3 份重试成功
        # → 第 4 份回写 → 阶段结束汇总再写一次
        self.assertEqual(calls, [1, 3, 4, 4])
        self.assertEqual(out, self.result_path)
        items = read_result_items(self.result_path)
        for report in self.reports:
            self.assertEqual(items[report.student_id]["event"].score, 12.0)
        self.assertEqual(grader.stage_logs[-1].extra.get("实时回写"), "失败 1 次")


if __name__ == "__main__":
    unittest.main()