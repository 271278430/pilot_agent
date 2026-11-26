"""Finite intent classification and validated plan preview, without hidden execution."""

import math
import re
import unicodedata
from collections import Counter
from typing import Literal

from pydantic import Field, model_validator

from pilot_core.capabilities import REGISTRY
from pilot_core.contracts import Contract, RunId


class RouteRequest(Contract):
    text: str = Field(min_length=1, max_length=3000)
    run_id: RunId | None = None


class PlanNode(Contract):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    capability: str
    depends_on: list[str] = Field(default_factory=list)


class TaskPlan(Contract):
    version: Literal[1] = 1
    recipe_id: str
    preview_only: Literal[True] = True
    nodes: list[PlanNode] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_dependencies(self):
        from .recipes import RECIPES

        if self.recipe_id not in RECIPES and self.recipe_id != "model-delivery-v1":
            raise ValueError("Unregistered task recipe")
        nodes = {node.id: node for node in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("Duplicate plan node IDs")
        for node in self.nodes:
            if node.capability not in REGISTRY:
                raise ValueError("Plan refers to an unregistered capability")
            if not set(node.depends_on) <= nodes.keys():
                raise ValueError("Plan references a missing dependency")
        ancestors = {}

        def visit(name, stack):
            if name in stack:
                raise ValueError("Cyclic plan")
            if name not in ancestors:
                seen = set()
                for dependency in nodes[name].depends_on:
                    seen.add(nodes[dependency].capability)
                    seen.update(visit(dependency, stack | {name}))
                ancestors[name] = seen
            return ancestors[name]

        required = {
            "supervisor.delegate_infra": {"supervisor.delegate_repo"},
            "supervisor.verify": {"supervisor.delegate_repo", "supervisor.delegate_infra"},
            "supervisor.finish": {"supervisor.verify"},
            "delivery_supervisor.delegate_repo": {"delivery_supervisor.preflight"},
            "delivery_supervisor.delegate_infra": {"delivery_supervisor.delegate_repo"},
            "delivery_supervisor.verify": {"delivery_supervisor.delegate_infra"},
            "delivery_supervisor.finish": {"delivery_supervisor.verify"},
        }
        for node in self.nodes:
            lineage = visit(node.id, set())
            if not required.get(node.capability, set()) <= lineage:
                raise ValueError("Plan bypasses acceptance dependencies")
        return self


class Candidate(Contract):
    id: str
    title: str
    similarity: float = Field(ge=0, le=1)
    exact_terms: list[str] = Field(default_factory=list)


class FunnelStage(Contract):
    layer: int = Field(ge=0, le=5)
    name: str
    outcome: str


class PendingChoice(Contract):
    kind: Literal["choose_recipe", "confirm_workflow", "confirm_project"]
    candidate_ids: list[str]
    created_at: float


class RoutingContext(Contract):
    active_id: str | None = None
    active_kind: Literal["service", "ml", "delivery", "app"] | None = None
    active_status: str | None = None
    touched_at: float = 0
    pending: PendingChoice | None = None


class RouteDecision(Contract):
    intent: Literal[
        "code_repair",
        "environment",
        "repair_and_deploy",
        "ml_environment",
        "model_delivery",
        "app_design",
        "project",
        "knowledge",
        "resources",
        "help",
        "status",
        "control",
        "chat",
        "clarify",
        "reject",
    ]
    normalized_text: str
    language: Literal["zh", "en"] = "zh"
    clauses: list[str] = Field(default_factory=list)
    rule_evidence: list[str]
    needs_clarification: bool
    question: str | None = None
    run_id: RunId | None = None
    control_action: Literal["approve", "pause", "resume", "cancel"] | None = None
    plan: TaskPlan | None = None
    workflow: Literal["service", "ml", "delivery", "app"] | None = None
    recipe_id: str | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    funnel: list[FunnelStage] = Field(default_factory=list)
    pending: PendingChoice | None = None
    reason: str = ""
    reply: str = ""
    executes_actions: Literal[False] = False
    classifier: str = "funnel_rules_tfidf_v3"
    tools: list[Literal["resources.snapshot", "knowledge.search"]] = Field(default_factory=list)
    model_used: str | None = None
    model_score: float | None = Field(default=None, ge=0, le=1)
    fallback: str | None = None
    observations: list[dict] = Field(default_factory=list)


def feature_tokens(text):
    english = re.findall(r"[a-z0-9_]+", text.lower())
    groups = re.findall(r"[\u4e00-\u9fff]+", text)
    return english + [part[i : i + 2] for part in groups for i in range(max(1, len(part) - 1))]


def candidates_for(text):
    """Local TF-IDF cosine retrieval; lexical vectors, not learned embeddings."""
    from .recipes import RECIPES

    definitions = [
        (r.id, r.title, r.keywords, r.title + " " + " ".join(r.keywords)) for r in RECIPES.values()
    ]
    definitions.append(
        (
            "ml-environment-v1",
            "搭建并验证模型训推环境",
            (
                "训练环境",
                "训推环境",
                "推理环境",
                "CUDA",
                "NCCL",
                "LoRA",
                "SmolLM",
                "A100",
                "GPU环境",
            ),
            "搭建模型训练推理环境 依赖 安装 CUDA NCCL LoRA checkpoint 服务 SmolLM A100 GPU环境",
        )
    )
    definitions.append(
        (
            "model-delivery-v1",
            "交付训练后的模型服务",
            ("模型服务", "交付模型", "已训模型"),
            "交付训练后的模型与配套仓库 服务接口 验收 批量推理 故障修复",
        )
    )
    docs = [Counter(feature_tokens(d[3])) for d in definitions]
    frequency = Counter(term for doc in docs for term in doc)
    idf = {term: math.log((len(docs) + 1) / (n + 1)) + 1 for term, n in frequency.items()}

    def vector(counts):
        return {t: (1 + math.log(n)) * idf[t] for t, n in counts.items() if t in idf}

    query = vector(Counter(feature_tokens(text)))
    norm = math.sqrt(sum(v * v for v in query.values()))
    values = []
    for definition, doc in zip(definitions, docs, strict=True):
        v = vector(doc)
        denominator = norm * math.sqrt(sum(n * n for n in v.values()))
        score = sum(query.get(t, 0) * n for t, n in v.items()) / denominator if denominator else 0
        terms = [word for word in definition[2] if word.lower() in text.lower()]
        values.append(
            Candidate(
                id=definition[0],
                title=definition[1],
                similarity=round(min(1, score), 4),
                exact_terms=terms,
            )
        )
    return sorted(values, key=lambda c: (not bool(c.exact_terms), -c.similarity, c.id))


def service_plan(recipe_id):
    if recipe_id == "model-delivery-v1":
        names = ["preflight", "delegate_repo", "delegate_infra", "verify", "finish"]
        return TaskPlan(
            recipe_id=recipe_id,
            nodes=[
                PlanNode(
                    id=name,
                    capability="delivery_supervisor." + name,
                    depends_on=[names[i - 1]] if i else [],
                )
                for i, name in enumerate(names)
            ],
        )
    return TaskPlan(
        recipe_id=recipe_id,
        nodes=[
            PlanNode(id="repo", capability="supervisor.delegate_repo"),
            PlanNode(id="infra", capability="supervisor.delegate_infra", depends_on=["repo"]),
            PlanNode(id="verify", capability="supervisor.verify", depends_on=["infra"]),
            PlanNode(id="finish", capability="supervisor.finish", depends_on=["verify"]),
        ],
    )


def fast_route(
    request: RouteRequest,
    *,
    task_exists=False,
    context: RoutingContext | None = None,
    current_time=None,
) -> RouteDecision:
    import time

    timestamp = time.time() if current_time is None else current_time
    context = context or RoutingContext(
        active_id=request.run_id if task_exists else None,
        active_kind="service" if task_exists else None,
        touched_at=timestamp,
    )
    text = unicodedata.normalize("NFKC", request.text).strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        raise ValueError("Empty normalized text")
    stages = [
        FunnelStage(layer=0, name="预处理", outcome="NFKC归一化；中文/英文；子句分隔保留原顺序")
    ]

    def decision(intent, reply="", **values):
        return RouteDecision(
            intent=intent,
            normalized_text=text,
            language="zh" if re.search(r"[\u4e00-\u9fff]", text) else "en",
            clauses=[s.strip() for s in re.split(r"[，,。.!！？?；;]+", text) if s.strip()],
            rule_evidence=values.pop("rule_evidence", []),
            needs_clarification=values.pop("needs_clarification", False),
            reply=reply,
            funnel=list(stages),
            **values,
        )

    denied = re.search(
        r"订机票|买机票|查天气|天气预报|价保|退款申请|买股票|股票推荐|写诗|写情书|点外卖|付款|转账|帮我下单|购买商品|book.*flight|weather|write.*poem",
        text,
        re.I,
    )
    read_only = bool(
        re.match(r"(?:请)?(?:解释|介绍|如何|怎样|为什么|怎么)", text)
    ) and not re.search(r"帮我|替我|立即执行", text)
    forbidden = (
        re.search(
            r"修改.*宿主.*驱动|升级.*驱动|删除|清空|杀掉.*进程|结束.*其他|访问.*密钥|输出.*密码",
            text,
        )
        if not read_only
        else None
    )
    stages.append(
        FunnelStage(layer=1, name="精确规则与业务边界", outcome="固定命令信号；越权/无关业务先拒绝")
    )
    if denied or forbidden:
        return decision(
            "reject",
            "这个请求超出 Pilot 的研发服务范围。这里只处理已登记的源码修复、服务部署、模型训推环境和项目任务查询。",
            reason="out_of_scope" if denied else "unsupported_permission",
            rule_evidence=[(denied or forbidden).group()],
        )
    if context.pending and context.pending.kind == "confirm_project" and text in {"确认", "批准", "好的", "好", "继续"}:
        return decision("project", "请在项目建议窗口中选择运行方式并确认；关闭窗口不会开始执行。",
                        reason="review_project", pending=context.pending)
    if re.search(r"(?:新建|创建).{0,30}项目|(?:导入|克隆|拉取).{0,80}https://github\.com/", text, re.I):
        return decision("project", reason="prepare_project", rule_evidence=["project_admission"])
    if re.search(r"(?:查看|列出|管理|有哪些|看看).{0,8}项目|项目列表|有哪些仓库", text):
        return decision("project", reason="list_projects", rule_evidence=["project_query"])
    # Do not silently drop an unrelated clause from an otherwise valid request.
    clauses = [s.strip() for s in re.split(r"[，,。.!！？?；;]+", text) if s.strip()]
    domain = r"代码|源码|修复|仓库|分页|订单|计价|加权|归一化|配置|分块|补齐|boltons|部署|服务|启动|环境|依赖|模型|训练|训推|推理|CUDA|NCCL|LoRA|GPU|A100|SmolLM|任务|进度|结果|验收|测试|补丁|接口|MCP|Skill|架构|记忆|上下文|存储|持久化|检查点|权限|隔离|沙箱|验证|复测|报错|异常|幂等|并发|延迟|吞吐|端口|故障|日志|agent|智能体|你是谁|你是什么|你能|能力|样例|示例|例子|\b(?:repair|fix|deploy|service|environment|model|train|test|code|repo|bug)\b"
    neutral = r"^(?:你好|谢谢|请|麻烦了|好的|好|hello|thanks|please)$"
    unrelated = next(
        (
            c
            for c in clauses
            if not re.search(
                domain + r"|demo|\bweb\b|网页|页面|界面|按钮|示例问题|输入框|回答展示", c, re.I
            )
            and not re.fullmatch(neutral, c, re.I)
            and (
                len(clauses) > 1
                or re.search(
                    r"帮我|替我|推荐|写一|写个|查一下|查询|购买|预订|分析|翻译|提醒|做一|做个", c
                )
            )
        ),
        None,
    )
    if unrelated:
        return decision(
            "reject",
            "请求中包含 Pilot 不支持的业务。请单独提出已登记的研发目标；当前不会创建任务或执行其中一部分。",
            reason="mixed_out_of_scope",
            rule_evidence=[unrelated],
        )
    negative = bool(re.search(r"(?:不要|禁止|别).{0,4}(?:部署|启动|修改|执行)", text))
    if negative:
        return decision(
            "clarify",
            "已保留禁止执行的要求。可以只读说明流程，当前不会创建执行计划。",
            needs_clarification=True,
            question="需要我只读说明哪个研发流程？",
            reason="negative_execution_constraint",
        )
    greeting = text.lower().rstrip("。.!！") in {"你好", "hello", "hi", "谢谢"}
    if greeting:
        return decision(
            "chat",
            "你好，我可以协助已登记的代码修复与服务验收、模型训推环境搭建，也能查询任务或解释项目架构。",
        )
    if re.search(
        r"你是谁|你是什么|你是.*agent|你能做什么|有哪些任务|服务范围|典型.*(?:样例|示例|例子)|给.*(?:样例|示例|例子)|^帮助[。.!！]?$|^help$",
        text,
        re.I,
    ) and not re.search(r"修复|部署|搭建|安装|服务器|资源|GPU|环境", text, re.I):
        return decision(
            "help",
            "我是 Pilot，协助把模型与配套代码交付成可调用、可验收的服务。Supervisor 负责协调，RepoPilot 修复服务代码，InfraPilot 准备环境并部署；失败后根据实际测试结果重新修复。\n\n你可以先问服务器有哪些模型和环境，或说：‘交付已训模型服务，修复配套仓库并验收接口’。我会先给出计划，确认后执行。目前执行限已登记仓库和模型配方，也支持项目咨询、只读资源查询和进度查询。",
        )
    actions = {
        "批准": "approve",
        "确认": "approve",
        "暂停": "pause",
        "继续": "resume",
        "恢复": "resume",
        "取消": "cancel",
    }
    command = next(
        (
            a
            for w, a in actions.items()
            if re.fullmatch(rf"(?:请)?{w}(?:任务|刚才的任务)?[。.!！]?", text)
        ),
        None,
    )
    query = bool(
        re.fullmatch(
            r"(?:请)?(?:查看)?(?:(?:这个|那个|刚才的)?任务)?(?:进度|状态|任务状态|到哪了|日志|结果|查看结果|任务进度)[。.!！]?",
            text,
        )
    )
    stages.append(
        FunnelStage(
            layer=2,
            name="会话状态与待澄清",
            outcome="仅绑定本会话已验证任务；待澄清15分钟、任务引用30分钟TTL",
        )
    )
    pending = (
        context.pending
        if context.pending and timestamp - context.pending.created_at <= 900
        else None
    )
    if (
        context.pending
        and not pending
        and re.fullmatch(r"(?:第[一二三]个|好|好的|就这个)[。.!！]?", text)
    ):
        return decision(
            "clarify",
            "刚才的选择已过期，请重新描述目标或选择登记任务。",
            needs_clarification=True,
            question="需要处理哪项研发任务？",
            reason="pending_choice_expired",
        )
    fresh_active = context.active_id and timestamp - context.touched_at <= 1800
    if pending:
        picked = None
        choices = pending.candidate_ids
        for i, key in enumerate(choices):
            ordinal = ["第一个", "第二个", "第三个"][i] if i < 3 else ""
            if (
                (ordinal and ordinal in text)
                or key in text
                or (
                    len(choices) == 1
                    and text.rstrip("。.!！") in {"好", "好的", "是", "确认", "继续", "就这个"}
                )
            ):
                picked = key
        matches = [c for c in candidates_for(text) if c.id in choices and c.exact_terms]
        if len(matches) == 1:
            picked = matches[0].id
        if picked:
            stages.append(
                FunnelStage(
                    layer=4,
                    name="登记与依赖决策",
                    outcome="从已保存候选绑定；计划只读，执行仍需确认",
                )
            )
            workflow = (
                "ml"
                if picked == "ml-environment-v1"
                else "delivery"
                if picked == "model-delivery-v1"
                else "service"
            )
            return decision(
                "ml_environment"
                if workflow == "ml"
                else "model_delivery"
                if workflow == "delivery"
                else "repair_and_deploy",
                "已补全任务选择，请核对执行范围。",
                workflow=workflow,
                recipe_id=picked,
                plan=None if picked == "ml-environment-v1" else service_plan(picked),
                reason="pending_choice_bound",
            )
    if command or query:
        if not fresh_active:
            return decision(
                "control" if command else "status",
                "请先选择本次要操作的任务；过期的“刚才那个”不会自动复用。",
                control_action=command,
                needs_clarification=True,
                question="请选择一个任务。",
                reason="task_reference_missing_or_expired",
            )
        return decision(
            "control" if command else "status",
            run_id=context.active_id,
            workflow=context.active_kind,
            control_action=command,
            reason="verified_task_reference",
        )
    if fresh_active and re.fullmatch(
        r"(?:查看|继续|暂停|取消)?(?:那个|这个|刚才的)(?:任务)?[。.!！]?", text
    ):
        action = next((a for w, a in actions.items() if text.startswith(w)), None)
        return decision(
            "control" if action else "status",
            run_id=context.active_id,
            workflow=context.active_kind,
            control_action=action,
            reason="resolved_task_reference",
        )
    if re.search(
        r"demo|应用|从零|从无到有|新模型|下载.*模型|模型.*下载", text, re.I
    ) and re.search(r"设计|搭建|部署|做|开发|可以|下载|模型", text, re.I):
        return decision(
            "app_design",
            tools=["resources.snapshot"],
            reason="application_goal_admission",
            workflow="app",
            needs_clarification=False,
        )
    resource_query = re.search(
        r"服务器|GPU|显存|资源|有哪些模型|模型.*(?:列表|可用|加载|有哪些)|环境.*(?:信息|有哪些|情况|版本)|有哪些.*环境",
        text,
        re.I,
    )
    asking = re.search(r"看看|查看|查询|盘点|告诉|列出|有哪些|多少|情况|信息|版本|可用|加载", text)
    execution = re.search(r"搭建|部署|安装|启动|运行|修复|训练一下|帮我训练", text)
    if resource_query and asking and not execution:
        return decision("resources", tools=["resources.snapshot"], reason="read_only_inventory")
    if re.search(
        r"架构|MCP|Skill|记忆|上下文|你用.*模型|项目.*(设计|存储|索引)|持久化|怎么.*(部署|训练|验收)",
        text,
        re.I,
    ) and not re.search(r"帮我.*(搭建|修复|安装|运行)", text):
        return decision(
            "knowledge", "可以只读检索本项目文档并给出来源。", reason="project_knowledge"
        )
    code = bool(re.search(r"修复|改代码|代码错误|bug|报错|\bfix\b|\brepair\b", text, re.I))
    infra = bool(
        re.search(
            r"交付|部署|启动|环境|依赖|CUDA|NCCL|端口|跑起来|deploy|environment|start.*service",
            text,
            re.I,
        )
    )
    ranked = candidates_for(text)
    stages.append(
        FunnelStage(
            layer=3,
            name="候选召回",
            outcome="登记目录TF-IDF词/字双gram向量；余弦相似度，不是语义embedding",
        )
    )
    exact = [c for c in ranked if c.exact_terms]
    stages.append(
        FunnelStage(
            layer=4,
            name="范围与登记决策门",
            outcome="唯一明确候选直达；多候选澄清；未知配方不执行；相似度不是置信概率",
        )
    )
    if len(exact) == 1 and (code or infra):
        item = exact[0]
        ml = item.id == "ml-environment-v1"
        if item.id == "model-delivery-v1":
            return decision(
                "model_delivery",
                "已识别模型服务交付，请核对计划。",
                workflow="delivery",
                recipe_id=item.id,
                plan=service_plan(item.id),
                candidates=[item],
                reason="registered_model_delivery",
            )
        if ml or (code and infra):
            return decision(
                "ml_environment" if ml else "repair_and_deploy",
                "已识别登记任务，请核对计划。",
                workflow="ml" if ml else "service",
                recipe_id=item.id,
                plan=None if ml else service_plan(item.id),
                candidates=[item],
                reason="unique_registered_candidate",
            )
        question = "当前源码配方包含修复、服务启动和独立验收，是否选择这条完整流程？"
        return decision(
            "code_repair" if code else "environment",
            question,
            needs_clarification=True,
            question=question,
            candidates=[item],
            pending=PendingChoice(
                kind="confirm_workflow", candidate_ids=[item.id], created_at=timestamp
            ),
            reason="workflow_scope_confirmation",
        )
    if len(exact) > 1 and (code or infra):
        options = exact[:3]
        question = "识别到多个登记任务，请明确选择：" + "、".join(c.title for c in options) + "。"
        return decision(
            "clarify",
            question,
            needs_clarification=True,
            question=question,
            candidates=options,
            pending=PendingChoice(
                kind="choose_recipe", candidate_ids=[c.id for c in options], created_at=timestamp
            ),
            reason="multiple_candidates",
        )
    stages.append(
        FunnelStage(
            layer=5,
            name="澄清或拒绝兜底",
            outcome="不将低相似度当作执行授权；不调用通用聊天模型接管业务",
        )
    )
    if code or infra:
        question = "这个研发目标尚未匹配登记配方。请选择已支持的任务；任意仓库、模型与环境需先注册并定义验收。"
        return decision(
            "clarify",
            question,
            needs_clarification=True,
            question=question,
            candidates=ranked[:3],
            reason="unregistered_development_task",
        )
    return decision(
        "reject",
        "这项业务不属于 Pilot 的服务范围。可以提出源码修复、模型环境、任务进度或项目架构相关请求。",
        reason="unresolved_request",
    )


def route(
    request, *, task_exists=False, context=None, current_time=None, resolver=None, history=()
):
    """Ordered funnel. A semantic classifier proposes intent; it never executes actions.

    Deterministic controls and verified references take priority. Unknown requests use
    an optional schema-validated resolver. Semantic task matches require a separate
    scope confirmation, then the existing planned/approve execution gate.
    """
    import time

    result = fast_route(
        request, task_exists=task_exists, context=context, current_time=current_time
    )
    if (
        result.reason
        not in {
            "unresolved_request",
            "unregistered_development_task",
            "mixed_out_of_scope",
            "negative_execution_constraint",
        }
        or resolver is None
    ):
        return result
    from .semantic import SemanticChoice, semantic_decision

    ranked = candidates_for(result.normalized_text)
    # Lexical similarity is evidence, not calibrated probability or execution authority.
    result.candidates = ranked[:3]
    result.funnel = [s for s in result.funnel if s.layer < 3] + [
        FunnelStage(
            layer=3, name="候选召回", outcome="按登记能力进行词法向量召回，保留候选供语义判断"
        ),
        FunnelStage(layer=4, name="决策门", outcome="词法证据不足或存在歧义，不直接创建任务"),
        FunnelStage(
            layer=5, name="语义兜底", outcome="模型仅识别意图；Schema、业务范围及执行权限由后端校验"
        ),
    ]
    try:
        value = SemanticChoice.model_validate(resolver(request, context, history, ranked[:3]))
        return semantic_decision(result, value, current_time or time.time())
    except Exception:
        # Provider failures and invalid JSON must never become execution permission.
        result.intent = "clarify"
        result.needs_clarification = True
        result.reply = "语义判断暂不可用。你可以查询资源、介绍项目，或选择已登记任务；执行前仍会展示计划。请更具体地说明目标。"
        result.reason = "semantic_unavailable"
        result.fallback = "model_or_schema_unavailable"
        result.plan = None
        result.workflow = None
        result.recipe_id = None
        result.pending = None
        return result
