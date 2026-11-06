# GradeHelper — 课程成绩助手

课程成绩助手是一个使用AI大模型辅助的课程成绩助手，计划用于自动评分课程报告、实验报告、试题、作业、试卷等材料。

本系统设计易于扩展，可较容易地适配到各种课程的报告、试题、作业、试卷等材料的评阅。

评阅项与评阅规则完全依赖于 `评分模板.xlsx` 的 `rule` 表（决定评分项、权重、处理方法与评估标准）；评分结果写入与模板同目录的独立结果 xlsx 的 `result` 表（命名模式见 `paths.result_name_format`）。**不同课程不同任务使用不同要求的评分模板**。

AI server API端点、路径及 Prompt 等均可在 `config.yaml` 中配置。

ver 0.1.0。当前版本支持课程报告自动评分。

## 流程

对课程报告（docx）自动评分：docling 解析 → 程序化规则批处理 → vllm 图片识别 → LLM 评分项逐项评估 → 结果写入与模板同目录的独立结果 xlsx 的 `result` 表。

## 安装

```powershell
pip install -e .
# 或直接安装依赖：pip install python-docx openpyxl PyYAML requests docling
```

## 运行方式

| 入口 | 命令 | 说明 |
| --- | --- | --- |
| 模块方式（推荐） | `python -m gradehelper [参数...]` | 安装后（`pip install -e .`）或已设置 `PYTHONPATH` 时使用 |
| 控制台脚本 | `gradehelper [参数...]` | 由 `pyproject.toml` 的 `[project.scripts]` 注册，安装后可直接调用 |
| 直接运行脚本 | `python gradehelper/__main__.py [参数...]` | 自动把项目根目录加入 `sys.path`，未安装也能运行 |

```powershell
python -m gradehelper --help      # 查看全部启动参数
python -m gradehelper --version   # 查看版本号
```

## 启动参数（按分类）

启动参数分为四类：**配置与信息**、**任务与路径**、**流程控制**、**日志与诊断**。命令行参数只覆盖 `config.yaml` 中对应的 `paths.*` 项；端点与 Prompt 必须通过配置文件提供。

### 参数总览

| 分类 | 参数 | 取值 | 默认值(不准确，需设置) |
| --- | --- | --- | --- |
| 配置与信息 | `--config PATH` | 文件路径 | 项目根目录 `config.yaml` |
| 配置与信息 | `-h, --help` | 开关 | — |
| 配置与信息 | `--version` | 开关 | — |
| 任务与路径 | `--reports DIR` | 目录 | `paths.reports_dir` |
| 任务与路径 | `--template XLSX` | 文件 | `paths.template` |
| 任务与路径 | `--output XLSX` | 文件 | `paths.output`；留空则按 `paths.result_name_format` 生成 |
| 任务与路径 | `--tagDep NAME` | 字符串 | `系部专业` |
| 流程控制 | `--stage STAGE` | `doc` / `pic` / `sen` / `full` | `full` |
| 流程控制 | `--skip-images N` | 非负整数 | `0` |
| 日志与诊断 | `-v, --verbose` | 开关 | 关闭（INFO 级别） |

### 1. 配置与信息类

| 参数 | 说明 |
| --- | --- |
| `--config PATH` | 指定配置文件。文件不存在时不报错，按内置默认值运行（端点、路径、Prompt 全为空），此时必须显式给出 `--reports`、`--template`，否则 `doc` 阶段找不到报告、`sen` 阶段拿不到有效 Prompt |
| `-h, --help` | 打印参数帮助并退出（不执行评分，退出码 0） |
| `--version` | 打印版本号并退出（不执行评分，退出码 0） |

### 2. 任务与路径类

| 参数 | 说明 |
| --- | --- |
| `--reports DIR` | 报告目录，覆盖 `paths.reports_dir`。`doc` 阶段只扫描该目录**顶层**符合命名格式的 `.docx`（不搜索子目录）；`pic` / `sen` 阶段不重新解析 docx，只扫描该目录**顶层**的 `*.md` |
| `--template XLSX` | 评分模板，覆盖 `paths.template`。只读取模板中的 `rule` 表（评分项、权重、处理方法、评分标准），**结果不写入模板** |
| `--output XLSX` | 结果 xlsx，覆盖 `paths.output`。相对路径以当前工作目录为准；留空时按 `paths.result_name_format` 生成（与模板同目录） |
| `--tagDep NAME` | 系部专业标识，用于结果文件名与日志文件名中的 `{tagDep}` 占位符（默认 `系部专业`） |

注意：

- 报告文件名必须符合 `paths.report_name_format` / `paths.report_name_separator`（默认 `{course}-{klass}-{student_id}-{stdName}-{reportName}`，分隔符 `-`），不符合格式的文档会被静默忽略。
- `--output` 优先级：`--output` > `paths.output` > `paths.result_name_format`（用第一份报告的课程名与报告名生成文件名）。
- `--tagDep` 参与结果文件命名：`doc` 与 `sen` 阶段应传同一个值，否则 `sen` 会写到另一个结果文件，读不到 `doc` 阶段的程序化分数。
- `pic` / `sen` 阶段依赖 `doc` 阶段落盘的 md 与图片，三个阶段应使用同一个 `--reports`。

### 3. 流程控制类

`--stage`：程序由三个可独立执行的阶段组成，阶段之间通过已落盘的 md 文件与结果 xlsx 交互，不需要额外的中间状态文件。

| 阶段 | 依赖 | 主要动作 | 产物 |
| --- | --- | --- | --- |
| `doc` | 报告 docx + 模板 | docling 解析（失败回退 python-docx）→ 应用 `--skip-images` → 读取 `rule` 表 → 程序化规则打分 | md + 图片落盘；结果 xlsx（LLM 项预置 0） |
| `pic` | `doc` 产出的 md/图片 + 模板 | vllm 并发识别图片 → 描述嵌入 md | md 覆盖落盘；`报告名-Pn.txt` 描述文件（均按批实时回写） |
| `sen` | `pic` 产出的 md + 模板 + 结果 xlsx | 结果文件前置校验（表头 + 学号）→ llm 批量评估所有 LLM 评分项 | 更新结果 xlsx（分数 + 置信度，按批实时回写） |
| `full` | 同上 | 依次执行 `doc` → `pic` → `sen` | 同三个阶段 |

- `pic` 仅在 `rule` 表存在 LLM 评分项时才识别图片；没有 LLM 项时跳过识图。
- 三个阶段都按文件多线程处理（`paths.parse_workers`）：单份文档的解析、加载或保存失败只会进入异常队列，不会中断其它文档；整批处理完成后自动重试异常队列（`paths.retry_rounds`，默认再重试 1 轮），重试后仍失败的文件记录到运行日志并跳过。重试过程中的**中间错误**（如某次解析/加载/识图/评分失败、docling 回退、图片缺失等）会逐条记录、每轮各记一条，不省略、不用最新一条覆盖旧记录：三个阶段都在处理过程中**按时序逐条写入日志**（每条带记录时间与级别），阶段总结里只保留 `中间错误: N 条` 计数。
- `sen` 阶段的失败以**文档全名**登记（md 文件名，如 `云安全-安全2401-2021001-张三-课程报告.md`）：处理过程中按时序写入的出错/报警信息保留文档名与出错原因，阶段总结的标题、`失败文档` 列表与控制台汇总都用全名；不再为每个失败文档在中途单独写一条失败记录。失败按文档全名去重：同一文档多轮重试失败只计一次；重试成功后从失败数中移除，并在阶段日志中记为 `重试成功: N 份（…md）`。LLM 返回的 JSON 被截断/无法解析、缺少评分项、某项不是对象、`score` 缺失或不是数字时，该文档一律计为**处理失败**（不写分数、不做“取最低分”兜底），进入异常队列重试。
- 三个阶段的运行日志都按时序分三段：开始的**配置信息**（报告目录、评分模板、结果文件、并发、重试轮数、跳过图片等；`pic` / `sen` 还含各自 vllm/llm 的 base_url 与 model）→ 处理过程中的**出错/报警信息**（每条带记录时间与级别，保留文档名与出错内容）→ 结束时的**总结**（处理/成功/失败、中间错误条数、最终未成功文档名）。不再为每个失败文档在中途单独写一条失败记录；控制台同样显示处理过程信息，最后输出与日志相同的总结块。
- `sen` 阶段结束（包括中途失败/中断）都会在控制台输出一行汇总：`【阶段 sen】处理 N 份文档：成功 X 份，失败 Y 份；失败文档：<全名、全名>`，失败文档缺省时不显示该段。
- 并发数来自配置：`pic` 用 `endpoints.vllm.client.batch_size`（并发识图数），`sen` 用 `endpoints.llm.client.batch_size`（并发报告数）。
- `doc` / `sen` 阶段开始前会先检查结果 xlsx 能否写入：若文件已在其它程序中打开（如 Excel），立即提示“结果文件无法写入……”并以退出码 1 结束，该阶段不做解析与评分；结果文件不存在（首次运行）视为可写。
- 结果 xlsx 的写入方式：`doc` 用 upsert（按学号匹配，存在则更新该行，不存在则追加新行）；`sen` 用 strict（只更新已有行，不会新增行）。`result` 表表头与当前处理规则不一致时，`doc` 会清空该表重建，`sen` 则在提交任何 LLM 请求之前报错退出。
- `sen` 阶段有两道**前置校验**，任一不满足都会在发出第一个 LLM 请求之前以退出码 1 结束：① `result` 表表头必须与当前规则完全一致；② 报告中所有学号都必须已存在于 `result` 表中（报错信息会列出缺少的学号）。这样不会出现“跑完一整轮 LLM 才发现写不进结果表”的情况。
- 没有 LLM 评分项时，`sen` 只沿用它从结果文件中读到的分数，不再重写结果文件（内容无变化）。
- `doc` / `sen` 运行期间会**独占占用**结果 xlsx（Windows 上以“只允许别人读”的共享方式持有文件句柄）：其它程序（如 Excel）此时只能以只读方式打开，避免跑到最后写表时才发现文件被锁；阶段结束（成功、失败或中断）立即释放。
- 结果 xlsx 一律**原子写**：内容先完整写到同目录临时文件 `.<结果文件名>.<pid>.tmp.xlsx`，再用 `os.replace` 覆盖目标文件，写入失败不会损坏原结果文件。目标被占用时自动重试（默认 6 次、间隔 1 秒）；仍失败则退出码 1，并把本次完整结果留在临时文件中，关闭 Excel 后可改名取用。
- `pic` / `sen` 阶段支持**实时回写**：`pic` 每识别 `endpoints.vllm.client.batch_size` 张图片就把描述写入 `-Pn.txt` 并更新对应 md（阶段结束不再全量重写）；`sen` 每完成 `endpoints.llm.client.batch_size` 份报告就把已完成评分写回结果 xlsx（单次回写失败只记录告警、不中断评分，并隔一批后再重试；阶段结束仍会汇总写一次，阶段日志会显示 `实时回写: 失败 N 次`）。
- `pic` 实时回写时若某张描述 txt 或某份 md 写盘失败（如文件被占用、磁盘已满），只记录该张/该份的失败（运行日志失败记录含 `环节: 写盘`，阶段日志显示 `写盘失败: N 张 txt / M 份 md`），不中断识图、也不影响同批其它文件；处理后重跑 `--stage pic` 即可补齐。
- 中断（Ctrl+C）时，已完成的评分会保存，退出码 130。

`--skip-images N`：解析时跳过每份报告的前 N 张图片（默认 2，通常用于跳过封面、评分表等与正文无关的图片）。

- 剩余图片保留文档中的原始编号参与识图与评分，不重排编号。
- `N=0` 表示不跳过任何图片。
- 被跳过图片的占位符 `[[PIC:...]]` 不会写入 md；`pic` / `sen` 阶段会按同样的 N 再次过滤，因此**各阶段必须传相同的 N**，否则 md、图片与评分的对应关系会错位。

### 4. 日志与诊断类

| 参数 | 说明 |
| --- | --- |
| `-v, --verbose` | 控制台日志输出到 DEBUG 级别（默认 INFO）。文件日志（`paths.log_path` / `paths.log_name_format`）不受此参数影响，始终记录运行摘要 |

### 优先级与退出码

- 参数优先级：命令行参数 > `config.yaml` 的 `paths.*` > 内置默认值。
- 退出码：`0` 成功；`1` 运行失败（错误已输出到控制台）；`130` 用户 Ctrl+C 取消。

### 常用示例

```powershell
# 1) 一键全流程（doc → pic → sen）
python -m gradehelper --reports D:\AI\Reports --template 云安全-课程报告-评分模板.xlsx

# 2) 分阶段执行（便于排查与续跑；三个阶段参数保持一致）
python -m gradehelper --stage doc --reports D:\AI\Reports --template 云安全-课程报告-评分模板.xlsx
python -m gradehelper --stage pic --reports D:\AI\Reports --template 云安全-课程报告-评分模板.xlsx
python -m gradehelper --stage sen --reports D:\AI\Reports --template 云安全-课程报告-评分模板.xlsx --output 结果.xlsx

# 3) 跳过前 5 张图片，并指定系部专业
python -m gradehelper --reports D:\AI\Reports --template 云安全-课程报告-评分模板.xlsx --skip-images 5 --tagDep 计算机系

# 4) 使用其它配置文件并输出调试日志
python -m gradehelper --config .\config.test.yaml -v --reports D:\AI\Reports --template 云安全-课程报告-评分模板.xlsx
```

## 运行产物

- 报告 md：`课程名称-专业班级-学号-姓名-报告名.md`（与原 docx 同目录，doc 阶段已去除空行）
- 报告图片：`课程名称-专业班级-学号-姓名-报告名-Pn.ext`（与 md 同目录，保留原始编号）
- 图片描述：`...-Pn.txt`（`paths.captions_dir` 为空时与原报告同目录）
- 结果 xlsx：`result` 表；总分列为 Excel 加权公式 `=分项1*权重1+分项2*权重2+...`；`置信度` 列仅 LLM 项写入
- 原子写临时文件：`.<结果文件名>.<pid>.tmp.xlsx`（写表过程中的中间文件，正常结束后不存在；写失败时保留完整结果）
- 运行日志：`paths.log_path`，或按 `paths.log_name_format` 生成（默认与模板同目录）

## 配置（config.yaml）

端点与 Prompt 全部来自 `config.yaml`，按用途分为三类：`endpoints`（模型端点）、`paths`（路径与命名）、`prompts`（提示词）。

### endpoints：模型端点

两套独立的 OpenAI 兼容端点，可各自覆盖：

| 键 | 用途 |
| --- | --- |
| `endpoints.llm` | 文字评分（`sen` 阶段） |
| `endpoints.vllm` | 图片识别（`pic` 阶段） |

每套端点包含三个子区域。`connection`（连接与模型，切换服务商只需改这一组）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `http://localhost:8080/` | OpenAI 兼容接口地址 |
| `api_key` | 空 | 鉴权密钥；本地无鉴权时留空 |
| `model` | 空 | 模型名；请求时作为 `model` 字段发送给 `/chat/completions` |

`client`（客户端参数，不发送给 API）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `connect_timeout` | `10` | TCP/TLS 连接超时（秒） |
| `read_timeout` | `300` | 流式读取时两个数据块之间的最大静默时间（秒） |
| `max_retries` | `3` | 请求失败重试次数 |
| `batch_size` | `4` | `llm` 为 `sen` 阶段并发报告数；`vllm` 为并发识图数 |

`api`（原样透传给 `/chat/completions`，新增任意键自动生效）：`temperature`、`top_p`、`top_k`、`min_p`、`presence_penalty`、`frequency_penalty`、`repeat_penalty`、`seed`、`reasoning`、`max_tokens`（默认注释掉，即不限制）。

### paths：路径与命名

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `reports_dir` | 空 | 报告目录（可用 `--reports` 覆盖） |
| `template` | 空 | 评分模板 xlsx（可用 `--template` 覆盖） |
| `output` | 空 | 结果 xlsx；留空按 `result_name_format` 生成（可用 `--output` 覆盖） |
| `captions_dir` | 空 | 图片描述 txt 保存目录；留空表示与原报告同目录 |
| `save_parsed` | `false` | 已废弃：md 与图片始终落盘到与原报告相同目录 |
| `log_path` | 空 | 运行日志文件路径；留空按 `log_name_format` 生成 |
| `log_name_format` | `{course}-{tagDep}-{reportName}.log` | 运行日志文件名格式（与模板同目录） |
| `log_max_size_kb` | `32` | 日志超过该大小（KB）才清理旧记录；`0` 表示不清理 |
| `log_retention_days` | `1` | 日志记录保留天数；`0` 表示不清理 |
| `parse_workers` | `4` | 单文件处理并发数（`doc` 解析/落盘，`pic`/`sen` 加载 md） |
| `retry_rounds` | `1` | 单文件失败后的额外重试轮数；`0` 表示不重试 |
| `report_name_format` | `{course}-{klass}-{student_id}-{stdName}-{reportName}` | 报告文件名解析格式，必须配置，否则所有报告都会被忽略 |
| `report_name_separator` | `-` | 报告文件名分隔符 |
| `result_name_format` | `{course}-{tagDep}-{reportName}-评分结果.xlsx` | 结果文件命名模式（与模板同目录，`评分结果` 为固定字符串） |

### prompts：提示词

| 键 | 占位符 | 说明 |
| --- | --- | --- |
| `llm_batch` | `{items_section}`、`{report}` | LLM 评分项批量 Prompt，一次提交一份报告的全部 LLM 项 |
| `image_context` | `{n}`、`{caption}` | 图片描述嵌入 md 的模板 |
| `image_prompt` | — | vllm 识图提示词，原样作为文本消息提交到 `/chat/completions` |

## 灵活性

评分项完全由 `rule` 表驱动：
- `处理方法=rule` 且在 `gradehelper/rules/` 中注册过的评分项走程序化规则（按评分项名称匹配注册表）
- 其余 `处理方法=LLM` 的项走通用 LLM 通道（逐文件批量：一次提交该文件的全部 LLM 项）
- 换课程/换报告类型只需换模板文件；新增程序化规则在 `gradehelper/rules/` 实现并注册即可

## 测试

```powershell
python -m unittest discover tests -v
```
