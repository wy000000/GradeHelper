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
    from gradehelper.config import load_config
    from gradehelper.grader import Grader
else:
    from .config import load_config
    from .grader import Grader


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gradehelper",
        description="课程报告自动化评分系统（docling 解析 + 规则引擎 + LLM 评估）",
    )
    parser.add_argument("--config", default=None, help="配置文件路径（默认项目根目录 config.yaml）")
    parser.add_argument("--reports", default=None, help="报告目录（覆盖配置）")
    parser.add_argument("--template", default=None, help="评分模板 xlsx（rule 表来源与结果写入目标）")
    parser.add_argument("--output", default=None, help="结果输出 xlsx；默认写回模板文件")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    parser.add_argument("--skip-images", type=int, default=0, help="解析时跳过前面 N 张图片（默认 1）")
    stage = "doc"
    # stage = "pic"
    # stage = "sen"
    # stage = "full"
    parser.add_argument("--stage", choices=["doc", "pic", "sen", "full"], default=stage,
                        help="执行阶段：doc=解析+程序规则，pic=vllm图片识别，sen=llm语义评分，full=全部（默认）")
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    grader = Grader(config)
    try:
        output = grader.run(
            reports_dir=args.reports,
            template=args.template,
            output=args.output,
            stage=args.stage,
            skip_images=args.skip_images,
        )
    except Exception as exc:  # noqa: BLE001 CLI 顶层统一报错
        logging.error("评分失败: %s", exc)
        return 1
    if output is not None:
        print(f"评分完成，结果已写入: {output}")
    else:
        print(f"阶段 [{args.stage}] 执行完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())