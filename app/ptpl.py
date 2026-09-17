# -*- coding: utf-8 -*-
"""提示词模板库：内置模板 + 自定义模板 + 版本管理。

参照腾讯云 ADP 提示词模板设计：
- prompt_templates：模板主表（标题/概述/类型/标签/内置标记/收藏/当前版本）
- prompt_template_versions：版本表（每次保存内容生成新版本，可查看/回滚）
- 首次启动时种子写入内置模板（builtin=1，内置模板不可删除，可"另存为"自定义）
"""
import time
from .db import execute, query, query_one

_SCHEMA = """
CREATE TABLE IF NOT EXISTS prompt_templates(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  overview TEXT DEFAULT '',
  ttype TEXT DEFAULT '通用',
  tags TEXT DEFAULT '',
  builtin INTEGER DEFAULT 0,
  creator TEXT DEFAULT '',
  favorite INTEGER DEFAULT 0,
  current_version INTEGER DEFAULT 1,
  copy_count INTEGER DEFAULT 0,
  created_at REAL,
  updated_at REAL
);
CREATE TABLE IF NOT EXISTS prompt_template_versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tpl_id INTEGER NOT NULL,
  version INTEGER NOT NULL,
  content TEXT DEFAULT '',
  note TEXT DEFAULT '',
  created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_ptplv_tpl ON prompt_template_versions(tpl_id);
CREATE INDEX IF NOT EXISTS idx_ptpl_tag ON prompt_templates(tags);
"""

# ------------------------------------------------------------------ 内置模板
# 每条：title / overview / ttype / tags / content（支持 {{变量}} 占位符）
BUILTIN_TEMPLATES = [
    {
        "title": "通用团队协作",
        "overview": "团队协作规则模板。适用于通用多角色任务，包含组队、分工、通信、异常处理和停止规则。",
        "ttype": "Claw模式", "tags": "多Agent协同",
        "content": """# 团队协作规则

你在一个多智能体团队中工作，请严格遵守以下规则：

## 1. 角色与分工
- 你是「{{角色名}}」，职责：{{职责描述}}。
- 只处理自己职责范围内的任务，超出范围请通过转发交给对应成员。

## 2. 通信规范
- 向其他成员发送消息时使用格式：@{{目标成员}} [{{消息类型}}] 内容。
- 消息类型包括：任务、询问、答复、通报、求助。

## 3. 任务执行
- 接到任务后先输出执行计划（不超过 3 步），再开始执行。
- 每完成一步，用一行通报进度：[进度] 步骤 x/y 完成。
- 拿不准的信息先提问确认，不要猜测。

## 4. 异常处理
- 连续 2 次无法完成时，主动上报 @{{组长}} 说明卡点。
- 发现上游产出有误时，立即通报而不是带错继续。

## 5. 停止规则
- 任务完成并汇总后即停止，不要重复汇报。
- 收到「任务终止」指令后立即停止当前工作。""",
    },
    {
        "title": "并行研究与交叉核验",
        "overview": "团队协作规则模板。适用于多个研究角色并行取证，再通过交叉质询和定向补证形成可信结论。",
        "ttype": "Claw模式", "tags": "多Agent协同",
        "content": """# 并行研究与交叉核验

研究主题：{{研究主题}}

## 第一阶段：并行取证
你是一名独立研究员，请围绕分配到的子课题独立完成研究：
- 子课题：{{子课题}}
- 输出格式：结论 + 依据（每条依据标注来源与可信度 高/中/低）
- 禁止参考其他研究员的结论，保证独立性。

## 第二阶段：交叉质询
收到其他研究员的结论后：
1. 逐条审查其依据，标记"存疑点"；
2. 用提问形式向对方质询：@{{对方成员}} [质询] 关于"{{结论片段}}"，依据是什么？
3. 被质询方须在下一轮用证据回应，不得含糊。

## 第三阶段：定向补证
- 对仍存疑的关键结论，列出需要补充的证据清单；
- 按清单定向检索/计算补证，不做无关扩展。

## 第四阶段：汇总结论
- 只保留通过交叉核验或补证成功的结论；
- 输出：最终结论、置信度、全部依据清单、未决事项。""",
    },
    {
        "title": "任务执行",
        "overview": "适用于 Multi-Agent 模式的 Agent 任务执行提示词模板结构。",
        "ttype": "Multi-Agent模式", "tags": "任务执行",
        "content": """# 任务执行 Agent

你是任务执行智能体，负责把目标拆解为可执行步骤并逐项完成。

## 目标
{{任务目标}}

## 工作循环
每轮按以下顺序工作：
1. **理解**：复述当前要完成的子任务（一句话）。
2. **执行**：调用可用工具或推理完成子任务。
3. **自检**：检查结果是否满足验收标准，不满足则修正（最多重试 2 次）。
4. **汇报**：输出 [完成] / [进行中 x%] / [受阻：原因]。

## 约束
- 子任务粒度：单个动作可完成，不超过 10 分钟工作量；
- 中间产物保存在工作区，最终产出汇总为一份报告；
- 遇到资源不足、权限不够等无法自行解决的问题，立即上报而不是空转。""",
    },
    {
        "title": "工具调用模板结构",
        "overview": "适用于工具调用场景的 Agent 提示词模板结构。",
        "ttype": "Multi-Agent模式", "tags": "工具调用",
        "content": """# 工具调用 Agent

你可以调用以下工具完成用户请求：

## 可用工具
{{工具清单：名称 - 功能 - 参数 - 返回}}

## 调用规范
1. **先判断是否需要工具**：能凭已有知识直接回答的，不调用工具。
2. **选择最合适的工具**：优先选择路径最短的工具组合，能用一个工具解决就不用两个。
3. **参数完整性**：缺少必填参数时，先向用户提问补齐，不要用占位值猜测。
4. **结果校验**：工具返回后先检查是否成功；失败时分析原因，可重试一次，仍失败则如实告知用户。

## 输出要求
- 工具结果为原始数据时，提炼成用户能直接看懂的答案；
- 回答中注明数据来源与获取时间；
- 工具不可用时给出替代建议。""",
    },
    {
        "title": "Agent转发模板结构",
        "overview": "适用于多 Agent 之间任务转发/路由的提示词模板结构。",
        "ttype": "Multi-Agent模式", "tags": "多Agent协同",
        "content": """# 任务转发 Agent

你是团队的路由调度者，负责把请求转发给最合适的成员。

## 成员清单
{{成员清单：ID - 名称 - 擅长领域}}

## 转发规则
1. 按请求意图匹配成员擅长领域，只能转发给一个成员；
2. 多个成员都合适时，选择历史完成率最高的；
3. 无法判断归属时，向用户澄清关键信息后再转发，最多追问 1 轮；
4. 转发时附上完整上下文，格式：
   转发给 @{{成员}} ｜ 请求原文：{{原文}} ｜ 补充信息：{{你补充的上下文}}

## 禁止行为
- 不要自己回答专业问题，你只负责路由；
- 不要拆散一个完整请求分别转发多人（除非用户明确要求并行处理）。""",
    },
    {
        "title": "信息收集型主Agent",
        "overview": "主 Agent 通过多轮提问收集齐必要信息后，再交给执行 Agent 处理。",
        "ttype": "Multi-Agent模式", "tags": "多Agent协同",
        "content": """# 信息收集主 Agent

你的任务是在调用执行智能体之前，把必要信息收集完整。

## 必要信息清单
{{必要字段：字段名 - 说明 - 示例}}

## 工作方式
1. 逐项检查清单，找出缺失字段；
2. 每轮最多问 2 个问题，问题要具体、带示例；
3. 用户表述模糊时给出选项让他选，而不是开放式追问；
4. 信息齐备后，输出结构化摘要并调用执行智能体：
   { {{字段组装}} }

## 语气
友好、简洁，不要一次抛出全部问题让用户填表。""",
    },
    {
        "title": "角色扮演",
        "overview": "标准角色扮演提示词模板，定义角色的身份、语气、能力边界与对话规则。",
        "ttype": "标准模式", "tags": "角色扮演",
        "content": """# 角色设定

你现在扮演：{{角色名称}}

## 身份背景
{{角色的身份、经历、性格描述}}

## 说话风格
- 语气：{{语气，如：专业温和 / 幽默轻松 / 严谨正式}}
- 称呼用户为：{{称呼}}
- 常用口头禅 / 表达习惯：{{可选}}

## 能力边界
- 擅长回答：{{擅长领域}}
- 不回答：与角色无关的话题，用户偏离时自然拉回；
- 不知道的事实直接说不知道，不编造。

## 对话规则
- 始终保持角色，不要跳出设定谈论自己是 AI；
- 回答长度与问题复杂度匹配，日常闲聊保持简短。""",
    },
    {
        "title": "内容生成",
        "overview": "通用文本创作模板：给定主题、受众、风格与字数要求，生成结构化内容。",
        "ttype": "标准模式", "tags": "文本创作",
        "content": """# 写作任务

请按以下要求创作内容：

- **主题**：{{主题}}
- **受众**：{{目标读者}}
- **文体**：{{文体，如：公众号文章 / 演讲稿 / 产品介绍}}
- **风格**：{{风格，如：专业 / 轻松 / 有感染力}}
- **字数**：{{字数范围}}

## 结构要求
1. 开头 2 句话内抓住读者注意力；
2. 主体分 {{段落数}} 个部分，每部分有小标题；
3. 结尾给出行动建议或总结金句。

## 约束
- 不使用空话套话，每个观点都有支撑；
- 首次出现的专业术语用一句话解释。""",
    },
    {
        "title": "知识库问答",
        "overview": "基于知识库内容回答问题的客服/问答模板，含拒答与引用规范。",
        "ttype": "标准模式", "tags": "知识库问答",
        "content": """# 知识库问答助手

你是「{{产品/业务名}}」的问答助手，只能依据知识库内容回答。

## 回答流程
1. 先在知识库中检索与问题相关的内容；
2. 检索到：基于原文作答，答案后标注来源（文档名），关键数字/政策须与原文一致；
3. 检索不到：如实回复"知识库中暂无相关信息"，并建议用户咨询人工客服，禁止编造。

## 表达要求
- 答案简洁直接，先给结论再给依据；
- 用户问题与知识库主题无关时，礼貌说明服务范围；
- 涉及费用、时效、政策等敏感信息，必须引用原文，不得改写数字。""",
    },
    {
        "title": "文本分类模板结构",
        "overview": "把文本归类到给定类目体系的模板，含输出格式与兜底类目。",
        "ttype": "标准模式", "tags": "文本理解",
        "content": """# 文本分类

请将下面的文本归入唯一类目。

## 类目体系
{{类目1}} / {{类目2}} / {{类目3}} / 其他

## 分类规则
1. 优先按文本的核心意图归类，而不是表面关键词；
2. 多个类目相关时，选择与主要篇幅/主要诉求对应的类目；
3. 都不匹配时归入"其他"，并猜测一个最接近的类目写入 suggest 字段。

## 输出格式（严格 JSON）
{"category": "类目名", "confidence": 0-1 之间的小数, "suggest": "最接近的类目或空", "reason": "一句话依据"}

## 待分类文本
{{文本内容}}""",
    },
    {
        "title": "标签提取",
        "overview": "从文本中抽取关键词标签，适合内容打标、归档检索场景。",
        "ttype": "通用", "tags": "结构化生成",
        "content": """# 标签提取

从下面的文本中提取标签。

## 要求
- 数量：{{最少}}~{{最多}} 个；
- 标签来源：领域术语、产品/项目名、关键动作、关键结论；
- 粒度：2~6 个字，动宾短语或名词短语；
- 禁止：原文没有出现的泛化词（如"内容""信息""其他"）。

## 输出格式（严格 JSON）
{"tags": ["标签1", "标签2"], "each_reason": {"标签1": "一句话出处"}}

## 文本
{{文本内容}}""",
    },
    {
        "title": "用于参数提取",
        "overview": "从用户中文表达中提取业务参数，支持缺省值与追问提示。",
        "ttype": "通用", "tags": "参数提取",
        "content": """# 参数提取

从用户输入中提取以下参数：

## 参数定义
{{参数表：参数名 - 类型 - 必填 - 说明 - 示例}}

## 提取规则
1. 严格按类型提取：数字去单位、日期统一为 YYYY-MM-DD、枚举值映射到给定选项；
2. 用户原话有歧义（如"下周"无具体日期）时置 null 并在 need_confirm 中列出；
3. 不要编造用户没有提供的值。

## 输出格式（严格 JSON）
{"{{参数1}}": 值或null, "{{参数2}}": 值或null, "need_confirm": ["需要追问的参数"], "raw_matched": {"参数": "命中的原话"}}

## 用户输入
{{用户输入}}""",
    },
    {
        "title": "对话记录-智能分类",
        "overview": "把对话记录归入已有分类体系的提示词，适合工单/客服对话归档。",
        "ttype": "智能分类", "tags": "任务执行",
        "content": """# 对话记录智能分类

请把以下对话记录归入已有分类。

## 已有分类
{{分类清单}}

## 分类要点
1. 以用户最后的诉求为准，过程中的闲聊、寒暄不影响分类；
2. 客服已解决的问题按"问题类型"分类，未解决的按"待办类型"分类；
3. 找不到匹配分类时输出 "新建建议：<建议分类名>"，不要硬塞进错误分类。

## 输出格式（严格 JSON）
{"category": "分类名", "resolved": true/false, "urgency": "高/中/低", "reason": "一句话依据"}

## 对话记录
{{对话内容}}""",
    },
    {
        "title": "AI翻译官",
        "overview": "提供中英文学术和专业文档的直译和意译服务。",
        "ttype": "标准模式", "tags": "翻译,角色扮演",
        "content": """# 翻译任务

请翻译以下内容：源语言 {{源语言}} → 目标语言 {{目标语言}}

## 翻译策略
1. **直译优先**：技术文档、合同条款以准确为先，保留原文结构；
2. **意译辅助**：营销文案、惯用语按目标语言习惯表达；
3. **术语一致**：文中的专业术语先用 {{术语表}} 统一，无术语表时保持全文一致并在译文后列出术语对照表。

## 硬性规则
- 数字、单位、日期、专有名词不得改动；
- 不确定原文含义时，在译文中用 [?] 标注并附注你的理解；
- 译文后附一行：直译/意译比例说明（如"直译为主，2 处意译"）。

## 原文
{{原文内容}}""",
    },
    {
        "title": "会议记录员",
        "overview": "将会议提炼成简明扼要的总结，包括讨论主题、关键要点和行动项。",
        "ttype": "标准模式", "tags": "文本理解",
        "content": """# 会议纪要整理

请把下面的会议记录整理成正式纪要。

## 输出结构
1. **会议主题**：一句话；
2. **关键结论**：分条列出，每条注明发言人；
3. **行动项**：表格（事项 / 负责人 / 截止时间），负责人或时间不明确的标注"待确认"；
4. **遗留问题**：未达成一致或待跟进的事项。

## 整理规则
- 只保留有信息量的内容，删除寒暄、重复、跑题部分；
- 口语转书面语，但不得改变原意；
- 数字、金额、日期务必保留原样。

## 会议记录
{{会议原文}}""",
    },
    {
        "title": "周报生成器",
        "overview": "根据工作内容自动生成标准化的工作周报模板。",
        "ttype": "标准模式", "tags": "文本创作",
        "content": """# 周报生成

把以下零散的工作记录整理成周报。

## 周报结构
1. **本周完成**：按项目分组，每项一句话 + 结果/产出；
2. **进行中**：事项 + 当前进度 + 预计完成时间；
3. **下周计划**：按优先级排序；
4. **风险与求助**：需要领导决策或协调的事项。

## 整理规则
- 动词开头描述成果，尽量量化（完成 x 个、覆盖率 x%）；
- 同一事项的多次记录合并为一条；
- 记录中看不出结果的，标注"结果待补充"。

## 工作记录
{{本周记录}}""",
    },
    {
        "title": "Excel公式生成",
        "overview": "根据用户描述的计算或数据操作创建 Excel 公式。",
        "ttype": "标准模式", "tags": "文本理解,结构化生成",
        "content": """# Excel 公式助手

根据需求生成 Excel 公式。

## 工作流程
1. 先复述你对需求的理解（表格结构 + 计算目标）；
2. 给出公式（假设数据从第 2 行开始，列按 {{列布局或"常规A列起" }}）；
3. 逐段解释公式含义；
4. 给出一个具体示例：代入样例数据的计算结果。

## 约束
- 优先使用常见函数，兼容 {{Excel/WPS}} 版本：{{版本}}；
- 需求有歧义（如"汇总"没说明求和还是计数）时先提问；
- 涉及数组公式时注明需要 Ctrl+Shift+Enter 还是直接回车。

## 需求描述
{{需求}}""",
    },
    {
        "title": "代码生成书写",
        "overview": "按规范生成代码：先给设计思路，再给完整代码与用法示例。",
        "ttype": "标准模式", "tags": "代码编程",
        "content": """# 代码生成

按以下要求生成代码：

- **语言/版本**：{{语言及版本}}
- **任务**：{{功能描述}}
- **输入/输出**：{{输入输出约定}}
- **编码规范**：{{团队规范，如：命名风格、注释要求}}

## 输出结构
1. **思路**：3 行以内说明核心设计；
2. **代码**：完整可运行，关键逻辑加注释；
3. **用法示例**：调用示例 + 预期输出；
4. **注意事项**：边界条件、依赖、性能提示。

## 约束
- 不省略代码（不写 "..." 省略）；
- 默认包含参数校验和异常处理；
- 不确定需求时先列出假设再写代码。""",
    },
    {
        "title": "Prompt评分专家",
        "overview": "评估提示词的质量和效果，提供优化改进建议。",
        "ttype": "标准模式", "tags": "文本理解,提示词优化",
        "content": """# 提示词评审

请评审下面的提示词，并给出优化建议。

## 评分维度（每项 0-10 分）
1. **任务清晰度**：目标、输出格式、约束是否明确；
2. **上下文充分性**：模型需要的背景信息是否齐全；
3. **防错设计**：是否有拒答/兜底/格式校验规则；
4. **Token 效率**：是否有冗余表述；
5. **可复用性**：换个场景是否容易适配。

## 输出结构
1. 总分与一句话总评；
2. 维度评分表（含扣分原因）；
3. 按影响大小排序的 3~5 条改进建议，每条附改写示例；
4. 优化后的完整提示词。

## 待评审提示词
{{提示词原文}}""",
    },
    {
        "title": "按参考答案打分",
        "overview": "基准评测模板：把模型回答与标准答案对比打分，适合批量评测。",
        "ttype": "基准评测", "tags": "评测",
        "content": """# 基准评测打分

对比"模型回答"与"参考答案"进行打分。

## 评分标准（0-10 分）
- 10-9：关键点全部覆盖，无错误；
- 8-7：关键点覆盖，有小瑕疵（表述不精确）；
- 6-5：遗漏 1 个关键点或有 1 处错误；
- 4-0：多个关键点遗漏或存在事实性错误。

## 输出格式（严格 JSON）
{"score": 数字, "hit_points": ["覆盖的关键点"], "miss_points": ["遗漏的关键点"], "errors": ["错误之处"], "comment": "一句话点评"}

## 参考答案关键点
{{关键点清单}}

## 模型回答
{{模型回答}}""",
    },
    {
        "title": "指定优先进入的意图",
        "overview": "在用户表达相近意图的场景下，指定优先进入哪一个意图，适合智能路由调优。",
        "ttype": "大模型意图识别节点", "tags": "意图识别",
        "content": """# 意图优先级判定

用户输入可能同时命中多个相近意图，请按以下优先级判定。

## 意图与优先级
1. {{高优先意图}}：{{触发描述}}
2. {{次优先意图}}：{{触发描述}}
3. 其他

## 判定规则
- 用户话术同时暗示多个意图时，进入优先级更高的意图；
- 明确指向低优先意图的（用户点名要求），按用户明确的来；
- 都不匹配时输出 "其他"。

## 输出格式（严格 JSON）
{"intent": "意图名", "evidence": "用户原话中支持判定的片段", "confidence": 0-1}

## 用户输入
{{用户输入}}""",
    },
]


def _now():
    return time.time()


def ensure_tables():
    from .db import conn as _conn, _lock
    with _lock:
        c = _conn()
        c.executescript(_SCHEMA)
        # 种子：仅在没有任何内置模板时写入，保证幂等且不覆盖用户改动
        n = c.execute("SELECT COUNT(*) FROM prompt_templates WHERE builtin=1").fetchone()[0]
        if n == 0:
            t = _now()
            for i, tpl in enumerate(BUILTIN_TEMPLATES):
                cur = c.execute(
                    "INSERT INTO prompt_templates(title,overview,ttype,tags,builtin,creator,"
                    "current_version,created_at,updated_at) VALUES(?,?,?,?,1,'AgentForge 平台',"
                    "1,?,?)",
                    (tpl["title"], tpl["overview"], tpl["ttype"], tpl["tags"],
                     t - (len(BUILTIN_TEMPLATES) - i), t - (len(BUILTIN_TEMPLATES) - i))
                ).lastrowid
                c.execute(
                    "INSERT INTO prompt_template_versions(tpl_id,version,content,note,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (cur, 1, tpl["content"], "内置模板 v1", t - (len(BUILTIN_TEMPLATES) - i))
                )
        c.commit()
        c.close()


# ------------------------------------------------------------------ CRUD
def list_templates(tab=None, tag=None, ttype=None, q=None, fav=False,
                   page=1, size=15):
    where, args = ["1=1"], []
    if tab == "builtin":
        where.append("builtin=1")
    elif tab == "custom":
        where.append("builtin=0")
    if tag:
        where.append("(tags=? OR tags LIKE ? OR tags LIKE ?)")
        args += [tag, tag + ",%", "%," + tag]
    if ttype:
        where.append("ttype=?")
        args.append(ttype)
    if fav:
        where.append("favorite=1")
    if q:
        where.append("(title LIKE ? OR overview LIKE ?)")
        args += ["%" + q + "%", "%" + q + "%"]
    cond = " AND ".join(where)
    total = query_one(f"SELECT COUNT(*) AS c FROM prompt_templates WHERE {cond}", args)["c"]
    size = max(1, min(100, int(size or 15)))
    page = max(1, int(page or 1))
    rows = query(
        f"SELECT * FROM prompt_templates WHERE {cond} ORDER BY builtin DESC,favorite DESC,updated_at DESC "
        f"LIMIT ? OFFSET ?", args + [size, (page - 1) * size])
    types = [r["ttype"] for r in query(
        "SELECT DISTINCT ttype FROM prompt_templates ORDER BY ttype")]
    return {"items": rows, "total": total, "page": page, "size": size, "types": types}


def get_template(pid):
    t = query_one("SELECT * FROM prompt_templates WHERE id=?", (pid,))
    if not t:
        return None
    v = query_one("SELECT content FROM prompt_template_versions WHERE tpl_id=? AND version=?",
                  (pid, t["current_version"]))
    t["content"] = v["content"] if v else ""
    return t


def create_template(title, overview="", ttype="通用", tags="", content="", creator=""):
    t = _now()
    pid = execute(
        "INSERT INTO prompt_templates(title,overview,ttype,tags,builtin,creator,current_version,"
        "created_at,updated_at) VALUES(?,?,?,?,0,?,1,?,?)",
        (title, overview, ttype, tags, creator or "", t, t))
    execute("INSERT INTO prompt_template_versions(tpl_id,version,content,note,created_at)"
            " VALUES(?,1,?,?,?)", (pid, content or "", "初始版本", t))
    return pid


def update_template(pid, title=None, overview=None, ttype=None, tags=None,
                    content=None, note=""):
    t = query_one("SELECT * FROM prompt_templates WHERE id=?", (pid,))
    if not t:
        raise ValueError("模板不存在")
    sets, args = ["updated_at=?"], [_now()]
    if title is not None:
        sets.append("title=?"); args.append(title)
    if overview is not None:
        sets.append("overview=?"); args.append(overview)
    if ttype is not None:
        sets.append("ttype=?"); args.append(ttype)
    if tags is not None:
        sets.append("tags=?"); args.append(tags)
    if content is not None:
        cur = query_one("SELECT content FROM prompt_template_versions WHERE tpl_id=? AND version=?",
                        (pid, t["current_version"]))
        if not cur or cur["content"] != content:
            newv = (t["current_version"] or 0) + 1
            sets.append("current_version=?"); args.append(newv)
            execute("INSERT INTO prompt_template_versions(tpl_id,version,content,note,created_at)"
                    " VALUES(?,?,?,?,?)",
                    (pid, newv, content, note or ("v%d 保存" % newv), _now()))
    execute("UPDATE prompt_templates SET " + ",".join(sets) + " WHERE id=?", args + [pid])
    return get_template(pid)


def delete_template(pid):
    t = query_one("SELECT * FROM prompt_templates WHERE id=?", (pid,))
    if not t:
        return False
    if t["builtin"]:
        raise ValueError("内置模板不可删除，可将其「另存为」后编辑副本")
    execute("DELETE FROM prompt_template_versions WHERE tpl_id=?", (pid,))
    execute("DELETE FROM prompt_templates WHERE id=?", (pid,))
    return True


def toggle_favorite(pid):
    t = query_one("SELECT favorite FROM prompt_templates WHERE id=?", (pid,))
    if not t:
        raise ValueError("模板不存在")
    execute("UPDATE prompt_templates SET favorite=?,updated_at=? WHERE id=?",
            (0 if t["favorite"] else 1, _now(), pid))
    return not t["favorite"]


def mark_copy(pid):
    execute("UPDATE prompt_templates SET copy_count=copy_count+1 WHERE id=?", (pid,))


def duplicate(pid, creator=""):
    """内置/自定义模板 → 复制为新的自定义模板（带全部版本历史）。"""
    t = get_template(pid)
    if not t:
        raise ValueError("模板不存在")
    new_title = t["title"] + "（副本）" if not t["builtin"] else t["title"]
    nid = create_template(new_title, t["overview"], t["ttype"], t["tags"],
                          t["content"], creator or "")
    # 复制历史版本（从 v2 起，v1 已由 create 写入）
    for v in query("SELECT * FROM prompt_template_versions WHERE tpl_id=? AND version>1 "
                   "ORDER BY version", (pid,)):
        execute("INSERT INTO prompt_template_versions(tpl_id,version,content,note,created_at)"
                " VALUES(?,?,?,?,?)", (nid, v["version"], v["content"], v["note"], v["created_at"]))
    cur = query_one("SELECT MAX(version) AS m FROM prompt_template_versions WHERE tpl_id=?", (nid,))
    if cur and cur["m"]:
        execute("UPDATE prompt_templates SET current_version=? WHERE id=?", (cur["m"], nid))
    return get_template(nid)


def list_versions(pid):
    return {"items": query("SELECT version,note,created_at FROM prompt_template_versions "
                           "WHERE tpl_id=? ORDER BY version DESC", (pid,))}


def get_version(pid, ver):
    v = query_one("SELECT * FROM prompt_template_versions WHERE tpl_id=? AND version=?",
                  (pid, ver))
    if not v:
        raise ValueError("版本不存在")
    return v


def rollback(pid, ver):
    v = get_version(pid, ver)
    return update_template(pid, content=v["content"], note="回滚自 v%d" % ver)
