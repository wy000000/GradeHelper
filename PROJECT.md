# GradeHelper — 项目文档

面向项目开发与维护：架构与流程、完整参数参考、配置项、机制细节、扩展与测试。安装、快速上手与常用参数见 [README.md](README.md)。

## 架构与流程

课程报告（docx / doc / pdf）自动评分：MinerU 解析 → 程序化规则批处理 → vllm 图片识别 → LLM 评分项逐项评估 → 结果写入与模板同目录的独立结果 xlsx 的 `result` 表。

程序由三个可独立执行的阶段组成，阶段之间通过已落盘的 md 文件与结果 xlsx 交互，不需要额外的中间状态文件：

| 阶段 | 依赖 | 主要动作 | 产物 |
| --- | --- | --- | --- |
| `doc` | 报告文档（docx/doc/pdf）+ 模板 | MinerU 解析 → 应用 `--skip-images` → 读取 `rule` 表 → 程序化规则打分 | md + 图片落盘（先写图片，全部成功后写 md）；结果 xlsx（仅更新 code 项，LLM 项保持原值/新行留空） |
| `pic` | `doc` 产出的 md/图片 + 模板 | vllm 并发识别图片 → 描述嵌入 md | md 覆盖落盘；`报告名-Pn.txt` 描述文件（均按批实时回写） |
| `sen` | `pic` 产出的 md + 模板 + 结果 xlsx | 结果文件前置校验（表头；缺行学号记错误跳过）→ llm 批量评估其余报告的 LLM 评分项 | 更新结果 xlsx（分数 + 置信度，按批实时回写） |
| `full` | 同上 | 依次执行 `doc` → `pic` → `sen`；当前阶段结束后若该阶段仍有错误，完成本阶段后停止，不再执行后续阶段（退出码 1） | 同三个阶段 |

## 文档解析

`doc` 阶段使用 [MinerU](https://github.com/opendatalab/MinerU)（4.x）解析报告，实际扫描的后缀由 `config.yaml` 的 `paths.document_suffixes` 决定（默认 `.docx` / `.doc` / `.pdf`）。所有格式均由 MinerU 统一解析，**不设本地兜底解析器**：某份文档解析失败时会进入异常队列重试（`paths.retry_rounds`），重试后仍失败则记录到运行日志并跳过，最终计入该阶段的失败数。

MinerU 本身还支持 PPT/PPTX、XLS/XLSX、RTF、ODT/ODS/ODP、EPUB、OFD、HTML/MHTML、CSV/TSV 以及 PNG/JPG/JPEG/WEBP/GIF/BMP/TIFF/JP2 图片；如有需要，把对应后缀加入 `paths.document_suffixes` 即可让 `doc` 阶段一并处理。图片识别（`pic`）与 LLM 评分（`sen`）阶段只处理 `doc` 阶段落盘的 `.md` 与图片，与原始文档格式无关。

> 安装 MinerU 需要 Python 3.11–3.14；PDF 的 `standard`/`advanced` 档位首次使用会自动下载模型。若只想快速跑通，可把 `paths.mineru_tier` 改为 `flash`。

- `mineru_tier`：`flash` / `basic` / `standard` / `advanced`，仅 PDF/图片生效；办公文档固定用 MinerU 的 `flash` 原生解析。
- `doc` 阶段只扫描报告目录**顶层**、且文件名符合 `paths.report_name_format` 的文档，不搜索子目录；命名规则见 README，格式与分隔符由 `paths.report_name_format` / `paths.report_name_separator` 配置（必须配置，否则所有报告都会被忽略）。

## 运行入口

| 入口 | 命令 | 说明 |
| --- | --- | --- |
| 模块方式（推荐） | `python -m gradehelper [参数...]` | 安装后（`pip install -e .`）或已设置 `PYTHONPATH` 时使用 |
| 控制台脚本 | `gradehelper [参数...]` | 由 `pyproject.toml` 的 `[project.scripts]` 注册，安装后可直接调用 |
| 直接运行脚本 | `python gradehelper/__main__.py [参数...]` | 自动把项目根目录加入 `sys.path`，未安装也能运行 |

```powershell
python -m gradehelper --help      # 查看全部启动参数
python -m gradehelper --version   # 查看版本号
```

## 启动参数完整参考

启动参数分为四类：**配置与信息**、**任务与路径**、**流程控制**、**日志与诊断**。命令行参数只覆盖 `config.yaml` 中对应的 `paths.*` 项；端点与 Prompt 必须通过配置文件提供。

### 参数总览

| 分类 | 参数 | 取值 | 默认值 |
| --- | --- | --- | --- |
| 配置与信息 | `--config PATH` | 文件路径 | 项目根目录 `config.yaml` |
| 配置与信息 | `-h, --help` | 开关 | — |
| 配置与信息 | `--version` | 开关 | — |
| 任务与路径 | `--reports DIR` | 目录 | 当前工作目录 |
| 任务与路径 | `--template XLSX` | 文件 | **必填**（留空则报错退出） |
| 任务与路径 | `--output XLSX` | 文件 | `paths.output`；留空则按 `paths.result_name_format` 生成 |
| 任务与路径 | `--tagDep NAME` | 字符串 | `系部专业` |
| 流程控制 | `--stage STAGE` | `doc` / `pic` / `sen` / `full` | `full` |
| 流程控制 | `--incremental true\|false` | `true` / `false`（显式给出） | `false` |
| 流程控制 | `--files NAME...` | 文件主名或完整名（逗号/空格分隔） | `None`（处理全部） |
| 流程控制 | `--skip-images N` | 非负整数 | `2` |
| 日志与诊断 | `-v, --verbose` | 开关 | 关闭（INFO 级别） |

> 注：表中为**设计默认值**。当前 `gradehelper/__main__.py` 的 `build_parser()` 留有本地调试硬编码（`--reports`、`--template`、`--stage`、`--incremental` 等的实际默认值被临时修改），实际默认可能与表中不一致——请显式传参，勿依赖默认值。

### 1. 配置与信息类

| 参数 | 说明 |
| --- | --- |
| `--config PATH` | 指定配置文件。文件不存在时不报错，按内置默认值运行（端点、路径、Prompt 全为空）；`--reports` 缺省为当前工作目录，`--template` 始终必填（留空报错退出） |
| `-h, --help` | 打印参数帮助并退出（不执行评分，退出码 0） |
| `--version` | 打印版本号并退出（不执行评分，退出码 0） |

### 2. 任务与路径类

| 参数 | 说明 |
| --- | --- |
| `--reports DIR` | 报告目录，仅由启动参数提供，**默认当前工作目录**。`doc` 阶段只扫描该目录**顶层**符合命名格式、且后缀在 `paths.document_suffixes` 中的文档（默认 `.docx`/`.doc`/`.pdf`，不搜索子目录）；`pic` / `sen` 阶段不重新解析原文档，只扫描该目录**顶层**的 `*.md` |
| `--template XLSX` | 评分模板，**仅由启动参数提供且必填**（为空则记录错误日志并退出）。只读取模板中的 `rule` 表（评分项、权重、处理方法、评分标准），**结果不写入模板** |
| `--output XLSX` | 结果 xlsx，覆盖 `paths.output`。相对路径以当前工作目录为准；留空时按 `paths.result_name_format` 生成（与模板同目录） |
| `--tagDep NAME` | 系部专业标识，用于结果文件名与日志文件名中的 `{tagDep}` 占位符（默认 `系部专业`） |

注意：

- 报告文件名必须符合 `paths.report_name_format` / `paths.report_name_separator`（默认 `{course}-{klass}-{student_id}-{stdName}-{reportName}`，分隔符 `-`），不符合格式的文档会被静默忽略。
- `--output` 优先级：`--output` > `paths.output` > `paths.result_name_format`（用第一份报告的课程名与报告名生成文件名）。
- `--tagDep` 参与结果文件命名：`doc` 与 `sen` 阶段应传同一个值，否则 `sen` 会写到另一个结果文件，读不到 `doc` 阶段的程序化分数。
- `pic` / `sen` 阶段依赖 `doc` 阶段落盘的 md 与图片，三个阶段应使用同一个 `--reports`。

### 3. 流程控制类

`--stage STAGE`：阶段定义、依赖与产物见上文[架构与流程](#架构与流程)的阶段表。

`--incremental`：增量模式，**必须显式给出 `true` 或 `false`**（不写值会报错，默认 `false`）。开启后各阶段只处理尚未完成的部分：

| 阶段 | 全量（`false`） | 增量（`true`） |
| --- | --- | --- |
| `doc` | 解析目录内所有符合命名格式的文档并重写结果表 | 只解析结果文件 `result` 表中没有对应学号记录的文档（与 md 是否存在无关）；结果文件不存在时全部解析；全部已有记录时跳过解析、规则读取与写表 |
| `pic` | md 中所有图片都交给 vllm 识别 | 只识别 md 中尚无 `prompts.image_context` 描述块的图片；已描述的报告不重写 |
| `sen` | 结果表中所有报告都重新跑 LLM 评分 | 只评分结果表中任一 LLM 评分项未评分的报告；全部已评分时跳过 LLM 与写表 |

- `sen` 的“未评分”判定：该 LLM 项在结果表中没有分数（空白单元格）即视为未评分；有任何一项为空就重新评分该报告。
- `pic` 依赖 `prompts.image_context` 中的 `{caption}` 占位符定位“已有描述”；模板缺少该占位符时无法判断，会给出警告并按全量识别。
- 增量跳过的数量记录在阶段总结的 `增量跳过` 行；本次运行的模式记录在阶段配置的 `模式` 行（紧跟 `运行开始` 行）：`全部全量`、`全部增量`、`指定全量`、`指定增量`（后两者由 `--files` 触发，并额外记录 `指定文件` 行）。
- `doc` 增量以结果文件 `result` 表中是否存在该学号的记录为准：结果文件不存在时解析全部文档；文档存在但结果表无对应记录时解析。不会删除结果表中已不存在的行；需要完整重建结果表时请用 `--incremental false`。
- `pic` 仅在 `rule` 表存在 LLM 评分项时才识别图片；没有 LLM 项时跳过识图。

`--files NAME...`：只更新指定的报告文件，**对 `doc` / `pic` / `sen` 三个阶段均有效**（默认 `None` 表示处理目录内全部文件；显式写 `none` 等价）。

- 取值：文件主名（不含后缀，如 `云安全-安全2401-2021001-张三-课程报告`）或完整文件名（如 `...-课程报告.docx`）均可，忽略大小写；支持 `--files a.docx,b.docx`、`--files "a.docx;b.docx"` 与 `--files a.docx b.docx` 三种写法。
- 一份名单三阶段通用：`doc` 命中原始文档（`.docx`/`.doc`/`.pdf`），`pic`/`sen` 自动命中同名 `.md`。
- 与 `--incremental` 组合决定四种模式：

| 模式 | `--files` | `--incremental` | 行为 |
| --- | --- | --- | --- |
| `全部全量` | `None`（省略） | `false` | 目录内所有文件全部重新处理 |
| `全部增量` | `None`（省略） | `true` | 所有文件按增量规则跳过已完成部分 |
| `指定全量` | 有名单 | `false` | 只处理名单内文件，且全部重新处理（不看已有结果） |
| `指定增量` | 有名单 | `true` | 只处理名单内文件，并在名单内套用增量跳过规则 |

- 名单内的文件在目录中找不到（或不符合命名格式）时逐条记为错误并跳过，继续处理命中的文件；**一个都没命中**时报错退出（退出码 1），避免“指定了文件却什么都没做”仍返回成功。
- 增量判定标准不因指定模式改变：`doc` 看结果表 `result` 是否有该学号记录、`pic` 看 md 是否已有图片描述块、`sen` 看结果表该学号任一 LLM 项分数是否为空。名单只收窄处理范围，不改变“已完成”的判定依据。
- `sen` 指定增量时，名单内文件若在结果表中完全没有行，记为错误并跳过（提示先执行 `doc` 阶段，写入运行日志与阶段总结），名单内其余文件继续评分；不会静默跳过，也不再整体报错退出。

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

## 阶段容错、写表与日志机制

- 三个阶段都按文件多线程处理（`paths.parse_workers`）：单份文档的解析、加载或保存失败只会进入异常队列，不会中断其它文档；整批处理完成后自动重试异常队列（`paths.retry_rounds`，默认再重试 1 轮），重试后仍失败的文件记录到运行日志并跳过。其中 `pic` / `sen` 进入重试轮后，vllm/llm 的并发（`batch_size`）按配置值减半——只在进入重试时减一次，后续重试轮沿用该值，不逐轮递归减半（下限 1 线程）。重试过程中的**中间错误**（如某次解析/加载/识图/评分失败、MinerU 解析失败、图片缺失等）会逐条记录、每轮各记一条，不省略、不用最新一条覆盖旧记录：三个阶段都在处理过程中**按时序逐条写入日志**（每条带记录时间与级别），阶段总结里只保留 `中间错误: N 条` 计数。以上均为**阶段内**的容错。`full` 模式下**阶段之间**还会把关：当前阶段结束后若该阶段仍有最终失败（或中断），完成本阶段后不再执行下一阶段——已完成的产物保留，跳过原因写入控制台与运行日志，并以退出码 1 结束；修复后重跑，或用 `--stage` 单独执行后续阶段续跑。
- `sen` 阶段的失败以**文档全名**登记（md 文件名，如 `云安全-安全2401-2021001-张三-课程报告.md`）：处理过程中按时序写入的出错/报警信息保留文档名与出错原因，阶段总结的标题、`失败文档` 列表与控制台汇总都用全名；不再为每个失败文档在中途单独写一条失败记录。失败按文档全名去重：同一文档多轮重试失败只计一次；重试成功后从失败数中移除，并在阶段日志中记为 `重试成功: N 份（…md）`。LLM 返回的 JSON 被截断/无法解析、缺少评分项、某项不是对象、`score` 缺失或不是数字时，该文档一律计为**处理失败**（不写分数、不做“取最低分”兜底），进入异常队列重试。
- 三个阶段的运行日志**每个阶段一条记录**（按时间顺序写在同一个记录块里）：开始的**配置信息**（标题、执行阶段、运行开始、报告目录、评分模板、结果文件、并发、重试轮数、跳过图片等；`pic` / `sen` 还含各自 vllm/llm 的 base_url 与 model）→ 处理过程中的**出错/报警信息**（每条带记录时间与级别，保留文档名与出错内容）→ 阶段结束的**处理结果**（结束时间、用时、处理/成功/失败、中间错误条数、最终未成功文档名、状态）。`pic` 阶段的合计是文档与图片之和（如 `共处理: 45 条（文档 4 份 + 图片 41 张）`），`共处理` 行会写明构成，分项见 `文档:` / `图片:` 行。标题、执行阶段、运行开始与配置信息在同一阶段只出现一次，不重复记录；不写 `记录时间`（与 `运行开始` 同值）、`状态: 开始`，也不单独追加运行尾记录（状态与用时由阶段记录本身给出），因此一次运行里同一项信息只出现一遍。不再为每个失败文档在中途单独写一条失败记录；控制台同样显示处理过程信息（出错/报警按 WARNING/ERROR 同步输出，不再只留在日志文件），最后输出与日志相同的结束部分。
- `sen` 阶段结束（包括中途失败/中断）都会在控制台输出一行汇总：`【阶段 sen】处理 N 份文档：成功 X 份，失败 Y 份；失败文档：<全名、全名>`，失败文档缺省时不显示该段。
- 并发数来自配置：`pic` 用 `endpoints.vllm.client.batch_size`（并发识图数），`sen` 用 `endpoints.llm.client.batch_size`（并发报告数）。
- `doc` 只写结果表中的 code 项，LLM 项的分数/置信度/评语保持原值（新行留空），因此可重复运行 `doc` 而不覆盖 `sen` 已写入的 LLM 分数；表头与当前规则不一致时仍按既有逻辑清空重建（此时旧列本身已失效）。
- `doc` / `sen` 阶段开始前会先检查结果 xlsx 能否写入：若文件已在其它程序中打开（如 Excel），立即提示“结果文件无法写入……”并以退出码 1 结束，该阶段不做解析与评分；结果文件不存在（首次运行）视为可写。
- 结果 xlsx 的写入方式：`doc` 用 upsert（按学号匹配，存在则更新该行，不存在则追加新行）；`sen` 用 strict（只更新已有行，不会新增行）。`result` 表表头与当前处理规则不一致时，`doc` 会清空该表重建，`sen` 则在提交任何 LLM 请求之前报错退出。
- `sen` 阶段有两道**前置校验**：① `result` 表表头必须与当前规则完全一致，不满足时在发出第一个 LLM 请求之前以退出码 1 结束；② 报告学号不在 `result` 表中的报告**记为错误并跳过**（运行日志逐条记录文档名与提示“请先执行 doc 阶段生成结果文件”，阶段总结计入失败份数与「学号缺失」），其余报告继续提交 LLM 评分——不因个别缺行整体退出，也不会为写不进结果表的报告白跑 LLM。若所有报告的学号都缺失，则不提交任何 LLM 请求并正常结束本阶段。
- 没有 LLM 评分项时，`sen` 只沿用它从结果文件中读到的分数，不再重写结果文件（内容无变化）。
- `doc` / `sen` 运行期间会**独占占用**结果 xlsx（Windows 上以“只允许别人读”的共享方式持有文件句柄）：其它程序（如 Excel）此时只能以只读方式打开，避免跑到最后写表时才发现文件被锁；阶段结束（成功、失败或中断）立即释放。
- 结果 xlsx 一律**原子写**：内容先完整写到同目录临时文件 `.<结果文件名>.<pid>.tmp.xlsx`，再用 `os.replace` 覆盖目标文件，写入失败不会损坏原结果文件。目标被占用时自动重试（默认 6 次、间隔 1 秒）；仍失败则退出码 1，并把本次完整结果留在临时文件中，关闭 Excel 后可改名取用。
- `pic` / `sen` 阶段支持**实时回写**：`pic` 每识别 `endpoints.vllm.client.batch_size` 张图片就把描述写入 `-Pn.txt` 并更新对应 md（阶段结束不再全量重写）；`sen` 每完成 `endpoints.llm.client.batch_size` 份报告就把已完成评分写回结果 xlsx（单次回写失败只记录告警、不中断评分，并隔一批后再重试；阶段结束仍会汇总写一次，阶段日志会显示 `实时回写: 失败 N 次`）。
- `pic` 实时回写时若某张描述 txt 或某份 md 写盘失败（如文件被占用、磁盘已满），只记录该张/该份的失败（运行日志失败记录含 `环节: 写盘`，阶段日志显示 `写盘失败: N 张 txt / M 份 md`），不中断识图、也不影响同批其它文件；处理后重跑 `--stage pic` 即可补齐。
- 中断（Ctrl+C）时，已完成的评分会保存，退出码 130。

## 进阶示例

```powershell
# 1) 分阶段执行（便于排查与续跑；三个阶段参数保持一致）
python -m gradehelper --stage doc --reports D:\AI\Reports --template 云安全-课程报告-评分模板.xlsx
python -m gradehelper --stage pic --reports D:\AI\Reports --template 云安全-课程报告-评分模板.xlsx
python -m gradehelper --stage sen --reports D:\AI\Reports --template 云安全-课程报告-评分模板.xlsx --output 结果.xlsx

# 2) 跳过前 5 张图片，并指定系部专业
python -m gradehelper --reports D:\AI\Reports --template 云安全-课程报告-评分模板.xlsx --skip-images 5 --tagDep 计算机系

# 3) 使用其它配置文件并输出调试日志
python -m gradehelper --config .\config.test.yaml -v --reports D:\AI\Reports --template 云安全-课程报告-评分模板.xlsx

# 4) 只重跑指定报告（三阶段均只处理名单内文件），并对名单内文件做增量
python -m gradehelper --reports D:\AI\Reports --template 云安全-课程报告-评分模板.xlsx `
    --files 云安全-安全2401-2021001-张三-课程报告,云安全-安全2401-2021002-李四-课程报告 `
    --incremental true
```

一键全流程与增量运行示例见 [README.md](README.md)。

## 运行产物（细节）

- 报告 md：`课程名称-专业班级-学号-姓名-报告名.md`（与原文档同目录，doc 阶段已去除空行）
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
| `batch_size` | `4` | `llm` 为 `sen` 阶段并发报告数；`vllm` 为并发识图数。进入 `retry_rounds` 重试轮后并发按此值减半（只减一次，不逐轮递归减半，最小 1） |

`api`（原样透传给 `/chat/completions`，新增任意键自动生效）：`temperature`、`top_p`、`top_k`、`min_p`、`presence_penalty`、`frequency_penalty`、`repeat_penalty`、`seed`、`reasoning`、`max_tokens`（默认注释掉，即不限制）。

### paths：路径与命名

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `output` | 空 | 结果 xlsx；留空按 `result_name_format` 生成（可用 `--output` 覆盖） |
| `captions_dir` | 空 | 图片描述 txt 保存目录；留空表示与原报告同目录 |
| `log_path` | 空 | 运行日志文件路径；留空按 `log_name_format` 生成 |
| `log_name_format` | `{course}-{tagDep}-{reportName}.log` | 运行日志文件名格式（与模板同目录） |
| `log_max_size_kb` | `32` | 日志超过该大小（KB）才清理旧记录；`0` 表示不清理 |
| `log_retention_days` | `1` | 日志记录保留天数；`0` 表示不清理 |
| `parse_workers` | `4` | 单文件处理并发数（`doc` 解析/落盘，`pic`/`sen` 加载 md） |
| `retry_rounds` | `1` | 单文件失败后的额外重试轮数；`0` 表示不重试；`pic`/`sen` 进入重试轮后并发按 `batch_size` 减半 |
| `document_suffixes` | `[".docx", ".doc", ".pdf"]` | `doc` 阶段扫描并解析的文档后缀；支持 YAML 列表或逗号分隔字符串。可加入 MinerU 支持的其它后缀（如 `.pptx`、`.xlsx`、`.png`） |
| `mineru_tier` | `standard` | MinerU 解析档位（仅 PDF/图片生效）：`flash` / `basic` / `standard` / `advanced`；办公文档固定用 MinerU 的 `flash` 原生解析 |
| `report_name_format` | `{course}-{klass}-{student_id}-{stdName}-{reportName}` | 报告文件名解析格式，必须配置，否则所有报告都会被忽略 |
| `report_name_separator` | `-` | 报告文件名分隔符 |
| `result_name_format` | `{course}-{tagDep}-{reportName}-评分结果.xlsx` | 结果文件命名模式（与模板同目录，`评分结果` 为固定字符串） |

### prompts：提示词

| 键 | 占位符 | 说明 |
| --- | --- | --- |
| `llm_batch` | `{items_section}`、`{report}` | LLM 评分项批量 Prompt，一次提交一份报告的全部 LLM 项 |
| `image_context` | `{n}`、`{caption}` | 图片描述嵌入 md 的模板 |
| `image_prompt` | — | vllm 识图提示词，原样作为文本消息提交到 `/chat/completions` |

## 扩展与测试

评分项完全由 `rule` 表驱动：

- `处理方法=code` 且在 `gradehelper/rules/` 中注册过的评分项走程序化规则（按评分项名称匹配注册表）
- 其余 `处理方法=LLM` 的项走通用 LLM 通道（逐文件批量：一次提交该文件的全部 LLM 项）
- 换课程/换报告类型只需换模板文件；新增程序化规则在 `gradehelper/rules/` 实现并注册即可

## 测试

```powershell
python -m unittest discover tests -v
```
