"""运行日志：阶段日志记录、日志文件路径解析、追加与过期清理。"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__
from .models import ParsedReport
from .parser import parse_filename, scan_reports

logger = logging.getLogger(__name__)

# 日志记录的时间标记；清理时按该时间判断新旧（当前格式，不兼容旧日志）
_RECORD_TIME_RE = re.compile(
    r"(?m)^记录时间:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$"
)
# 阶段记录不再单独写「记录时间」（与「运行开始」同值、属重复信息），只有出错条目带
# 「记录时间」；一条记录两者都没有时，回退用「运行开始」判断新旧
_RUN_START_RE = re.compile(
    r"(?m)^运行开始:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*$"
)
_SEP_EQ = "=" * 60
_SEP_DASH = "-" * 60
_LOG_TITLE = "GradeHelper \u8fd0\u884c\u65e5\u5fd7 Ver " + __version__


def format_duration(seconds: float) -> str:
    """把秒数格式化为 小时:分钟:秒（HH:MM:SS）。"""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_stage_title(stage: str, *, course: str = "", tag_dep: str = "",
                       report_name: str = "", document: str = "") -> str:
    """阶段标题：课程名称-系部专业-报告名称-阶段；单项失败时用文档名替代报告名。"""
    course = (course or "").strip() or "未知课程"
    tag_dep = (tag_dep or "").strip() or "系部专业"
    detail = ((document or "").strip() or (report_name or "").strip() or "未知报告")
    return f"{course}-{tag_dep}-{detail}-{stage}"


@dataclass
class StageLogEntry:
    """单个阶段的运行日志记录。"""

    stage: str                      # 阶段名：doc / pic / sen
    start_time: str                 # 开始时间 "YYYY-MM-DD HH:MM:SS"
    end_time: str                   # 结束时间
    duration_seconds: float         # 用时（秒）
    processed_count: int            # 共处理多少份
    start_student_id: str | None    # 开始学号（第一份报告的学号）
    end_student_id: str | None      # 结束学号（最后一份报告的学号）
    succeeded_count: int = 0        # 成功条数
    failed_count: int = 0           # 最终失败条数（同时用于状态判定）
    unit: str = "份"                # 计数单位
    processed_note: str = ""        # 合计行的构成说明（混合计数时用，如“文档 4 份 + 图片 41 张”）
    error: str | None = None        # 出错原因（若无则为 None）
    course: str = ""                # 课程名称（取第一份报告）
    tag_dep: str = ""               # 系部专业（取第一份报告；无报告时用 CLI 参数）
    report_name: str = ""           # 报告名称（取第一份报告）
    document: str = ""              # 具体文档名（单项失败时写入标题）
    reports_dir: str = ""           # reports directory (stage summary)
    template: str = ""              # reports directory (stage summary)
    output: str = ""                # result file (doc/sen stage)
    extra: dict[str, str] = field(default_factory=dict)  # 阶段附加信息
    warnings: list[str] = field(default_factory=list)   # 中间解析错误（非致命，单项仍成功）
    record_time: str = ""           # 日志记录写入时间（清理依据）
    list_warnings: bool = True      # 总结里是否逐条列出中间错误；sen 的中间错误按时序
                                    # 单独写入日志，总结只保留条数

    @property
    def title(self) -> str:
        """阶段标题：课程名称-系部专业-报告名称-阶段；单项失败时用文档名替代报告名。"""
        return format_stage_title(self.stage, course=self.course, tag_dep=self.tag_dep,
                                  report_name=self.report_name, document=self.document)


class RunLogMixin:
    """运行日志能力；宿主类需提供 ``self.config``。

    日志状态（``stage_logs`` / ``_log_path`` / ``_log_lock`` / ``_log_failed``）
    由 :meth:`_init_run_log` 初始化，便于宿主类复用同一套日志实现。
    """

    def _init_run_log(self) -> None:
        self.stage_logs: list[StageLogEntry] = []
        self._log_path: Path | None = None
        self._log_lock = threading.Lock()
        self._log_failed = False
        # 阶段记录块是否处于“打开”状态：开始部分（标题/执行阶段/运行开始/配置）
        # 已写入，出错信息与阶段结果随后追加到同一块内，由 _append_stage_log 闭合
        self._stage_block_open = False
        # 阶段开始时已写入日志的配置项（键→值）；阶段结果里不再重复输出同值字段
        self._stage_config_values: dict[str, str] = {}

    def _resolve_log_path(self, template: str | Path,
                          reports_dir: str | Path | None = None,
                          tag_dep: str = "系部专业") -> Path:
        """确定运行日志文件路径。

        - 若 config.paths.log_path 非空，直接使用；
        - 否则按 config.paths.log_name_format 生成，放在模板同目录；
        - 课程/报告名取报告目录中按文件名排序的第一份报告，无报告时用占位。
        """
        cfg = self.config
        log_path = (cfg.paths.log_path or "").strip()
        if log_path:
            return Path(log_path)

        course = ""
        report_name = ""
        if reports_dir is not None:
            try:
                candidates = scan_reports(
                    reports_dir,
                    name_format=cfg.paths.report_name_format,
                    separator=cfg.paths.report_name_separator,
                    suffixes=tuple(cfg.paths.document_suffixes),
                )
            except OSError:
                candidates = []
            if candidates:
                meta = parse_filename(
                    candidates[0],
                    name_format=cfg.paths.report_name_format,
                    separator=cfg.paths.report_name_separator,
                )
                if meta is not None:
                    course, _klass, _sid, _name, report_name = meta

        fmt = ((cfg.paths.log_name_format or "").strip()
               or "{course}-{tagDep}-{reportName}.log")
        try:
            name = fmt.format(
                course=course or "未知课程",
                tagDep=tag_dep or "系部专业",
                reportName=report_name or "未知报告",
            )
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning("日志文件名格式无效，使用默认名称: %s", exc)
            name = f"未知课程-{tag_dep or '系部专业'}-未知报告.log"
        # 禁止路径分隔符等非法文件名字符
        name = re.sub(r'[\\/:*?"<>|]', "_", name)
        return Path(template).parent / name

    @staticmethod
    def _split_log_records(text: str) -> list[str]:
        lines = text.splitlines(keepends=True)
        records: list[str] = []
        buf: list[str] = []
        idx = 0
        n = len(lines)
        def flush() -> None:
            nonlocal buf
            if "".join(buf).strip():
                records.append("".join(buf))
            buf = []
        while idx < n:
            if (idx + 1 < n and lines[idx].rstrip("\r\n") == _SEP_EQ
                    and "GradeHelper \u8fd0\u884c\u65e5\u5fd7" in lines[idx + 1]):
                flush()
                buf.append(lines[idx]); idx += 1
                buf.append(lines[idx]); idx += 1
                if idx < n and lines[idx].rstrip("\r\n") == _SEP_EQ:
                    buf.append(lines[idx]); idx += 1
                continue
            buf.append(lines[idx])
            marker = lines[idx].rstrip("\r\n")
            idx += 1
            if marker in (_SEP_EQ, _SEP_DASH) and (idx >= n or not lines[idx].strip()):
                flush()
                while idx < n and not lines[idx].strip():
                    idx += 1
        flush()
        return records


    def _trim_log(self) -> None:
        """日志超过配置大小时，按每条记录的时间标记清理过期记录。

        只在启动阶段调用。大小与保留天数来自 config.yaml：
        ``paths.log_max_size_kb`` / ``paths.log_retention_days``；
        任一为 0 时关闭清理。时间标记优先取“记录时间”（出错条目），没有则取
        “运行开始”（阶段记录）；两者都没有的旧格式记录会在清理时一并移除。
        清理采用同目录临时文件 + 原子替换。
        """
        if not self._log_path:
            return
        try:
            max_size_kb = int(self.config.paths.log_max_size_kb)
            retention_days = float(self.config.paths.log_retention_days)
        except (TypeError, ValueError):
            logger.warning("日志清理配置无效，跳过清理")
            return
        if max_size_kb <= 0 or retention_days <= 0:
            return

        log_path = Path(self._log_path)
        try:
            if not log_path.is_file():
                return
            size = log_path.stat().st_size
            if size <= max_size_kb * 1024:
                return
            text = log_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("读取运行日志失败，跳过旧记录清理: %s", exc)
            return

        records = self._split_log_records(text)
        if not records:
            return

        cutoff = datetime.now() - timedelta(days=retention_days)
        kept_records: list[str] = []
        removed = 0
        for record in records:
            match = _RECORD_TIME_RE.search(record) or _RUN_START_RE.search(record)
            if not match:
                # 当前格式的每条记录必须带“记录时间”或“运行开始”；两者都没有视为旧格式并清理
                removed += 1
                continue
            try:
                record_time = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                removed += 1
                continue
            if record_time < cutoff:
                removed += 1
            else:
                kept_records.append(record)

        if not removed:
            return

        tmp_path = log_path.with_name(log_path.name + ".trim.tmp")
        try:
            tmp_path.write_text("".join(kept_records), encoding="utf-8")
            tmp_path.replace(log_path)
        except OSError as exc:
            logger.warning("清理运行日志失败，保留原文件: %s", exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        logger.info(
            "运行日志 %.1f KB 超过 %d KB，已清理 %d 条超过 %.1f 天的记录，保留 %d 条",
            size / 1024, max_size_kb, removed, retention_days, len(kept_records),
        )

    def _append_log(self, text: str) -> None:
        """把文本追加到运行日志；写失败只警告一次，不影响主流程。"""
        if not self._log_path or self._log_failed:
            return
        log_path = Path(self._log_path)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_lock:
                with log_path.open("a", encoding="utf-8") as fh:
                    fh.write(text)
                    if text and not text.endswith("\n"):
                        fh.write("\n")
        except OSError as exc:
            self._log_failed = True
            logger.warning("运行日志写入失败，后续不再尝试: %s", exc)

    def _append_run_header(self, *, stage: str, reports_dir: str | Path,
                           template: str | Path, output: str | Path | None) -> None:
        lines = [
            _SEP_EQ,
            _LOG_TITLE,
            _SEP_EQ,
        ]
        self._append_log("\n".join(lines) + "\n")
        logger.info("%s", "\n".join(lines))


    def _append_stage_start_entry(self, *, stage: str, course: str = "",
                                  tag_dep: str = "", report_name: str = "",
                                  config: dict[str, str] | None = None) -> None:
        """阶段开始：写入该阶段记录块的开始部分（标题、执行阶段、运行开始与配置）。

        记录块保持打开：处理过程中的出错信息与阶段结束的处理结果（结束时间、用时、
        处理结果、状态）随后追加到同一块内，最后由 :meth:`_append_stage_log` 补上
        闭合分隔线——一个阶段只有一条记录，标题/执行阶段/运行开始/配置只出现一次。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            _SEP_EQ,
            format_stage_title(stage, course=course, tag_dep=tag_dep,
                               report_name=report_name),
            f"执行阶段: {stage}",
            f"运行开始: {now}",
        ]
        # 只登记真正写入日志的配置项（空值不写行，阶段结果里也无需屏蔽）
        self._stage_config_values = {
            key: value for key, value in (config or {}).items() if value
        }
        for key, value in (config or {}).items():
            if value:
                lines.append(f"{key}: {value}")
        text = "\n".join(lines) + "\n"
        self._append_log(text)
        self._stage_block_open = True
        logger.info("%s", "\n".join(lines))

    def _append_stage_event(self, *, stage: str, level: str, message: str) -> None:
        """处理过程中按时序追加一条出错/报警信息（含文档名与出错内容）。

        只写入日志文件；控制台的对应输出由各处理点的 logging 负责，避免重复刷屏。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            _SEP_DASH,
            f"记录时间: {now}",
            f"阶段: {stage}",
            f"级别: {level}",
            f"内容: {message}",
            _SEP_DASH,
        ]
        self._append_log("\n".join(lines) + "\n")

    def _build_stage_entry(self, *, stage: str, reports: list[ParsedReport],
                           tag_dep: str, start_dt: datetime, end_dt: datetime,
                           elapsed: float, error: str | None,
                           extra: dict[str, str] | None = None,
                           warnings: list[str] | None = None,
                           title_report: ParsedReport | None = None,
                           document: str = "",
                           reports_dir: str | Path | None = None,
                           template: str | Path | None = None,
                           output: str | Path | None = None,
                           processed: int | None = None,
                           succeeded: int | None = None,
                           failed: int = 0,
                           unit: str = "份",
                           processed_note: str = "",
                           list_warnings: bool = True) -> StageLogEntry:
        """构造阶段日志记录；标题取第一份报告的课程/系部专业/报告名。"""
        first = title_report or (reports[0] if reports else None)
        start_report = reports[0] if reports else first
        end_report = reports[-1] if reports else first
        extras = dict(extra or {})
        courses = {r.course.strip() for r in reports if r.course and r.course.strip()}
        report_names = {r.report_name.strip() for r in reports
                        if r.report_name and r.report_name.strip()}
        if len(courses) > 1 or len(report_names) > 1:
            extras["批次"] = f"{len(courses)} 个课程 / {len(report_names)} 种报告"
        total = len(reports) if processed is None else processed
        ok = total if succeeded is None else succeeded
        return StageLogEntry(
            stage=stage,
            start_time=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            duration_seconds=round(elapsed, 2),
            processed_count=total,
            start_student_id=start_report.student_id if start_report else None,
            end_student_id=end_report.student_id if end_report else None,
            succeeded_count=ok,
            failed_count=failed,
            unit=unit,
            processed_note=processed_note,
            error=error,
            course=first.course if first else "",
            tag_dep=(first.tag_dep if first else "") or tag_dep,
            report_name=first.report_name if first else "",
            document=document,
            reports_dir=str(reports_dir) if reports_dir is not None else "",
            template=str(template) if template is not None else "",
            output=str(output) if output is not None else "",
            extra=extras,
            warnings=list(warnings or []),
            list_warnings=list_warnings,
        )

    def _append_failure_entry(self, *, stage: str, error: str,
                              report: ParsedReport | None = None,
                              document: str = "", course: str = "",
                              tag_dep: str = "", report_name: str = "",
                              student_id: str | None = None,
                              extra: dict[str, str] | None = None) -> None:
        """追加单个文档/图片的失败记录，标题精确到具体文档。"""
        now = datetime.now()
        if not document and report is not None:
            document = report.base_name
        sid = student_id or (report.student_id if report is not None else None)
        self._append_stage_log(StageLogEntry(
            stage=stage,
            start_time=now.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
            duration_seconds=0.0,
            processed_count=0,
            start_student_id=sid,
            end_student_id=sid,
            error=error,
            course=course or (report.course if report is not None else ""),
            tag_dep=tag_dep or (report.tag_dep if report is not None else ""),
            report_name=report_name or (report.report_name if report is not None else ""),
            document=document,
            extra=dict(extra or {}),
        ))

    def _append_stage_log(self, entry: StageLogEntry) -> None:
        """追加阶段结束部分并闭合该阶段的记录块。

        阶段开始块（标题 / 执行阶段 / 运行开始 / 配置）已写入时，这里只补结束时间、
        处理结果与状态：一个阶段只有一条记录，标题等信息不重复输出。没有开始块时
        （阶段开始前就失败、单项失败记录、测试直接调用）写一条完整独立记录。
        """
        if not entry.record_time:
            entry.record_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.stage_logs.append(entry)
        lines: list[str] = []
        if not self._stage_block_open:
            lines = [_SEP_EQ, entry.title]
            lines.append(f"执行阶段: {entry.stage}")
            # 报告目录/评分模板/结果文件属于阶段开始的配置信息：开始已记录同值时不再重复
            for key, value in (("\u62a5\u544a\u76ee\u5f55", entry.reports_dir),
                               ("\u8bc4\u5206\u6a21\u677f", entry.template),
                               ("\u7ed3\u679c\u6587\u4ef6", entry.output)):
                if value and self._stage_config_values.get(key) != value:
                    lines.append(f"{key}: {value}")
            # 学号只在单项失败记录里保留（用于定位具体文档）；阶段记录不再重复
            if entry.start_student_id:
                lines.append(f"\u5f00\u59cb\u5b66\u53f7: {entry.start_student_id}")
            if entry.end_student_id:
                lines.append(f"\u7ed3\u675f\u5b66\u53f7: {entry.end_student_id}")
            lines.append(f"\u8fd0\u884c\u5f00\u59cb: {entry.start_time}")
        lines.append(f"\u7ed3\u675f\u65f6\u95f4: {entry.end_time}")
        lines.append(f"\u7528\u65f6: {format_duration(entry.duration_seconds)}")
        if entry.processed_count:
            unit = entry.unit or "份"
            # 混合计数（如 pic 的文档+图片）时写明构成，避免合计与分项对不上
            note = f"（{entry.processed_note}）" if entry.processed_note else ""
            lines.append(f"共处理: {entry.processed_count} {unit}{note}")
            lines.append(f"成功: {entry.succeeded_count} {unit}")
            lines.append(f"失败: {entry.failed_count} {unit}")
        for key, value in entry.extra.items():
            # 阶段开始已记录且值相同的配置项（如并发、跳过图片、base_url）不重复输出
            if self._stage_config_values.get(key) == value:
                continue
            lines.append(f"{key}: {value}")
        if entry.warnings:
            lines.append(f"\u4e2d\u95f4\u9519\u8bef: {len(entry.warnings)} \u6761")
            if entry.list_warnings:
                for warning in entry.warnings:
                    lines.append(f"  - {warning}")
        if entry.error:
            lines.append(f"状态: 失败 - {entry.error}")
        elif entry.failed_count:
            if entry.succeeded_count == 0 and entry.processed_count > 0:
                lines.append(f"状态: 失败 - {entry.failed_count} 条全部失败")
            else:
                lines.append(f"状态: 部分失败 - 失败 {entry.failed_count} 条")
        elif entry.warnings:
            lines.append("状态: 成功（有警告）")
        else:
            lines.append("状态: 成功")
        lines.append(_SEP_EQ)
        text = "\n".join(lines) + "\n"
        self._append_log(text)
        # 记录块已闭合：清空开始块登记的配置，后续记录（如单项失败）不再屏蔽同名字段
        self._stage_block_open = False
        self._stage_config_values = {}
        # 阶段完成时，log 记录的信息同样输出到控制台
        logger.info("%s", "\n".join(lines))
