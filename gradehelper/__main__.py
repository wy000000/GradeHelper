"""命令行入口：python -m gradehelper --reports <dir> --template <xlsx>。"""

from __future__ import annotations

import argparse
import logging
import sys

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
    parser.add_argument("--skip-llm", action="store_true", help="跳过 LLM 评分项（只跑程序化规则）")
    parser.add_argument("--skip-vllm", action="store_true", help="跳过图片识别预处理")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
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
            skip_llm=args.skip_llm,
            skip_vllm=args.skip_vllm,
        )
    except Exception as exc:  # noqa: BLE001 CLI 顶层统一报错
        logging.error("评分失败: %s", exc)
        return 1
    print(f"评分完成，结果已写入: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
