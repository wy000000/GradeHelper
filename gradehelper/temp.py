"""临时脚本：按 has_requirement() 的归一化规则计算 s1/s2 的相似度。"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gradehelper.rules.content import _normalize_for_match  # noqa: E402

s1 = '''1、针对某一云安全问题进行论述，叙述其产生的原因，带来的危害，解决的方法。
2、对本门课程的总结，包括收益、建议等。'''
s2 = '''1. 针对某一云安全问题进行论述，叙述其产生的原因、带来的危害、解决的方法；
2. 对本门课程的总结，包括收益、建议等。'''
n1 = _normalize_for_match(s1)
n2 = _normalize_for_match(s2)
print(f"s1 归一化: {n1}")
print(f"s2 归一化: {n2}")

ratio = difflib.SequenceMatcher(None, n1, n2).ratio()

print(f"difflib.SequenceMatcher.ratio() = {ratio:.6f}")
