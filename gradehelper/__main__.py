"""命令行入口：python -m gradehelper --reports <dir> --template <xlsx>。
也可直接运行：python gradehelper/__main__.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 支持两种启动方式：
# 1. python -m gradehelper          （__package__ 为 "gradehelper"，走相对导入）
# 2. python gradehelper/__main__.py （__package__ 为 None，走绝对导入并把项目根目录加入 sys.path）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from gradehelper import __version__
    from gradehelper.config import load_config
    from gradehelper.grader import Grader
    from gradehelper.median_merge import MedianError, run_median
    from gradehelper.progress import install_logging_bridge
else:
    from . import __version__
    from .config import load_config
    from .grader import Grader
    from .median_merge import MedianError, run_median
    from .progress import install_logging_bridge


def _parse_bool(value: str) -> bool:
    """解析显式布尔参数：只接受 true / false（不区分大小写）。"""
    text = str(value).strip().lower()
    if text == "true":
        return True
    if text == "false":
        return False
    raise argparse.ArgumentTypeError("--incremental 只接受 true 或 false")

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gradehelper",
        description="课程报告自动化评分系统（MinerU 解析 + 规则引擎 + LLM 评估）",
    )
    reports_dir = str(Path.cwd())
    gradeTemplate_dir = None

    reports_dir = "D:\AI\Reports"

    # reports_dir = "D:\AI\Reports\课程报告"
    # gradeTemplate_dir = "云安全-课程报告-评分模板.xlsx"

    reports_dir = "D:\AI\Reports\实验报告"
    gradeTemplate_dir = "云安全-docker实验报告-评分模板.xlsx"

    skip_images = 2
    stage = "doc"
    # stage = "pic"
    stage = "pic-e"
    stage = "sen"
    # stage = "full"
    # stage = "full-e"
    incremental = False
    # incremental = True

    files = None
    # files = ["云安全-信息安全2302-3230604054-念欣怡-实验报告：docker的安装配置与操作"
    #         ,"云安全-信息安全2502-3240110024-王警-实验报告：docker的安装配置与操作"
    #         ]

    # 只更新指定的报告文件（主名或完整文件名）；None 表示处理目录内全部文件。
    # 名单非空时，增量/全量只对名单内文件有效；
    # 日志用「模式」记录：全部全量 / 全部增量 / 指定全量 / 指定增量。

    parser.add_argument("--config", default=None, help="配置文件路径（默认项目根目录 config.yaml）")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}",
                        help="显示版本号并退出")
    parser.add_argument("--reports", default=reports_dir,
                        help="报告目录（默认当前工作目录）")
    parser.add_argument("--template", default=gradeTemplate_dir,
                        help="评分模板 xlsx（必填；仅 rule 表来源，结果不写入模板）")
    parser.add_argument("--output", default=None,
                        help="结果输出 xlsx；默认按 config.yaml paths.result_name_format 生成（与模板同目录）")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    parser.add_argument("--tagDep", default="系部专业",
                        help="系部专业标识，用于结果文件命名（默认：系部专业）")
    parser.add_argument("--skip-images", type=int, default=skip_images, help="解析时跳过前面 N 张图片（默认 2）")
    parser.add_argument("--stage",
                        choices=["doc", "pic", "pic-e", "sen", "full", "full-e"],
                        default=stage,
                        help="执行阶段：doc=解析+程序规则，pic=vllm图片识别，"
                             "pic-e=优先复用图片描述 txt（缺失的再交给 vllm），"
                             "sen=llm语义评分，full=全部（默认），"
                             "full-e=全部（图片阶段使用 pic-e）")
    parser.add_argument("--incremental", type=_parse_bool, default=incremental,
                        metavar="true|false",
                        help="增量模式：true=开启，false=关闭（默认 false）。"
                             "doc 只解析结果文件中无记录的文档；pic 只识别无文字描述的图片；"
                             "sen 只评分 LLM 项未评分的报告")
    parser.add_argument("--files", nargs="+", default=files, metavar="NAME",
                        help="只更新指定的报告文件（主名或完整文件名，逗号/空格分隔，"
                             "对 doc/pic/pic-e/sen 三个阶段均有效）；省略=处理全部（默认）")
    ####################################################################


    # 可选子命令：median。不带子命令时解析行为与旧版完全一致（command=None）。
    subparsers = parser.add_subparsers(dest="command")
    median_parser = subparsers.add_parser(
        "median", help="多份评分结果按学号取总分中位数，合成新的评分结果")
    median_parser.add_argument("base", metavar="主名称",
                               help="原评分结果文件主名称（自动匹配 {主名称}-<n>.xlsx，"
                                    "忽略不带 -n 的文件；可带 .xlsx 后缀）")
    median_parser.add_argument("--dir", default=None,
                               help="原评分结果所在目录（默认当前工作目录）")
    median_parser.add_argument("--output", default=None,
                               help="输出 xlsx（默认 {主名称}-median.xlsx，与原文件同目录）")
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # 日志输出前先清掉进度行，避免并行进度与日志互相覆盖
    install_logging_bridge()

    # median 子命令：不需要 config/模板/模型端点，先于评分流程分发
    if getattr(args, "command", None) == "median":
        try:
            result = run_median(args.base, directory=args.dir, output=args.output)
        except MedianError:
            # 错误详情已由 run_median 输出到控制台与 {主名称}-median.log
            return 1
        except Exception as exc:  # noqa: BLE001 CLI 顶层统一报错
            logging.error("median 处理失败: %s", exc)
            return 1
        print(f"median完成，结果已写入: {result}")
        return 0

    config = load_config(args.config)
    grader = Grader(config)
    try:
        output = grader.run(
            reports_dir=args.reports,
            template=args.template,
            output=args.output,
            stage=args.stage,
            skip_images=args.skip_images,
            tag_dep=args.tagDep,
            incremental=args.incremental,
            files=args.files,
        )
    except KeyboardInterrupt:
        # 已完成的结果已由阶段逻辑保存；这里兜底关闭仍在飞的连接
        grader.cancel()
        logging.error("已取消（Ctrl+C）：已中断进行中的 vllm/llm 生成，已完成的评分已保存")
        return 130
    except Exception as exc:  # noqa: BLE001 CLI 顶层统一报错
        grader.cancel()
        logging.error("评分失败: %s", exc)
        return 1
    if output is not None:
        print(f"评分完成，结果已写入: {output}")
    else:
        print(f"阶段 [{args.stage}] 执行完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())