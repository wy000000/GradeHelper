"""阶段实现（doc / pic / sen），由 :class:`gradehelper.grader.Grader` 组合。"""

from .doc import DocStageMixin
from .pic import PicStageMixin
from .sen import SenStageMixin

__all__ = ["DocStageMixin", "PicStageMixin", "SenStageMixin"]
