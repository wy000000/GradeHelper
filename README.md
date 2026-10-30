# GradeHelper — 课程成绩助手

课程成绩助手是一个使用AI大模型辅助的课程成绩助手，计划用于自动评分课程报告、实验报告、试题、作业、试卷等材料。

本系统设计易于扩展，可较容易地适配到各种课程的报告、试题、作业、试卷等材料的评阅。

评阅项与评阅规则完全依赖于 `评分模板.xlsx`（`rule` 表决定评分项、权重、处理方法与评估标准；`result` 表写入评分结果）。**不同课程不同任务使用不同要求的评分模板**。

AI server API端点、路径及 Prompt 等均可在 `config.yaml` 中配置。

ver 0.1.0。当前版本支持课程报告自动评分。

## 流程

对课程报告（docx）自动评分：docling 解析 → 程序化规则批处理 → vllm 图片识别 → LLM 评分项逐项评估 → 结果写回评分模板 xlsx 的 `result` 表。

## 安装

```powershell
pip install -e .
# 或直接安装依赖：pip install python-docx openpyxl PyYAML requests docling
```

## 使用

程序分为三个可独立执行阶段，通过 `--stage` 参数选择：

```powershell
# full（默认）：依次执行 doc → pic → sen 三个阶段
python -m gradehelper --reports D:\path\to\reports --template 云安全-课程报告-评分模板.xlsx

# doc 阶段：解析文档 + 程序化规则打分，写入 result 表（LLM 项预置 0）
python -m gradehelper --stage doc --reports D:\\path\\to\\reports --template 云安全-课程报告-评分模板.xlsx

# pic 阶段：vllm 识别图片，将描述嵌入 md（需先执行 doc）
python -m gradehelper --stage pic --reports D:\\path\\to\\reports --template 云安全-课程报告-评分模板.xlsx

# sen 阶段：llm 语义评分，更新 result 表（需先执行 pic）
python -m gradehelper --stage sen --reports D:\\path\\to\\reports --template 云安全-课程报告-评分模板.xlsx --output 结果.xlsx

# 跳过每份报告的前 N 张图片（默认 2）
python -m gradehelper --reports ... --skip-images 5
```

- 报告文件名格式：`课程名称-专业班级-学号-姓名.docx`（其他文件自动忽略）
- 评分要求来自模板 xlsx 的 `rule` 表；结果写入同一文件的 `result` 表（总分列为 Excel 加权公式 `=分项1*权重1+分项2*权重2+...`；`置信度` 列仅 LLM 项写入）
- 图片描述另存为 `课程名称-专业班级-学号-姓名-Pn.txt`，并嵌入内存中的 Markdown 供 LLM 评估；解析出的 md 与图片始终保存到与原报告相同目录
- `--skip-images N`：解析时跳过每份报告的前 N 张图片，剩余图片重新编号后参与后续识别与评分（默认 2）
- `--stage`：doc/ pic/ sen/ full，三个阶段间通过已保存的 md 文件与 result 表交互，无需额外中间状态文件

## 配置（config.yaml）

- `endpoints.llm` / `endpoints.vllm`：两个独立的 OpenAI 兼容端点（默认均为 `http://localhost:8080/` + `qwen35-thinking-general`，可各自覆盖）。llm用于文字评分，vllm用于图片识别。
- `prompts.llm_item`：LLM 评分项通用 Prompt；`prompts.image_context`：图片描述嵌入模板
- `paths.save_parsed`：已废弃，解析出的 md/图片始终保存到与原报告相同目录

## 灵活性

评分项完全由 `rule` 表驱动：
- `处理方法=rule` 且在 `gradehelper/rules/` 中注册过的评分项走程序化规则（按评分项名称匹配注册表）
- 其余 `处理方法=LLM` 的项走通用 LLM 通道（逐文件、逐项单独 Prompt）
- 换课程/换报告类型只需换模板文件；新增程序化规则在 `gradehelper/rules/` 实现并注册即可

## 测试

```powershell
python -m unittest discover tests -v
```

