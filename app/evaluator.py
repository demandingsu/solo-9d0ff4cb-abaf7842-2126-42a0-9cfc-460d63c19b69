"""核心评估逻辑：解析源字段清单与跨作业字段依赖，按三值逻辑判定每个产出列状态。

判定规则（针对某一版源字段清单）：
- 每个产出列允许有多组输入（备用组），组内全部输入可用该组才可用；
  任一组可用则该列可产出（正常 ok）。
- 所有组都确定不可用（源字段缺失、或上游列确定断供）=> 断供 broken。
- 仅能依靠“缺失引用”（引用了依赖清单中不存在的作业/列、或既非源表也非作业）
  或“无源支撑的循环”才能产出 => 待核实 unknown。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

OK = "ok"            # 正常
BROKEN = "broken"    # 断供
UNKNOWN = "unknown"  # 待核实

STATUS_LABEL = {OK: "正常", BROKEN: "断供", UNKNOWN: "待核实"}

# 嵌入报告中的依赖路径树最大展开深度（状态计算本身不受此限制）
MAX_TRACE_DEPTH = 8
# 递归求值的最大栈深，超出按待核实处理，防止极端依赖图打爆递归
MAX_EVAL_DEPTH = 2000


class FormatError(ValueError):
    """输入文件格式错误（会导致本次核查失败、保留旧版）。"""


# ---------------------------------------------------------------------------
# 源字段清单解析
# ---------------------------------------------------------------------------

def parse_sources(data: Any) -> dict[str, set[str]]:
    """解析源字段清单，接受三种形式：

    1) {"tables": {"表": ["字段", ...]}}
    2) {"表": ["字段", ...]}
    3) ["表.字段", ...]
    """
    tables: dict[str, set[str]] = {}
    if isinstance(data, dict):
        body = data.get("tables") if isinstance(data.get("tables"), dict) else data
        for t, cols in body.items():
            if not isinstance(t, str) or not t:
                raise FormatError(f"源表名必须是非空字符串：{t!r}")
            if not isinstance(cols, list) or not all(isinstance(c, str) and c for c in cols):
                raise FormatError(f"源表 {t!r} 的字段清单必须是非空字符串数组")
            tables.setdefault(t, set()).update(cols)
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, str) or "." not in item:
                raise FormatError(f"源字段项必须是 '表.字段' 形式的字符串：{item!r}")
            t, c = item.split(".", 1)
            tables.setdefault(t, set()).add(c)
    else:
        raise FormatError("源字段清单必须是 JSON 对象或字符串数组")
    if not tables:
        raise FormatError("源字段清单为空")
    return tables


# ---------------------------------------------------------------------------
# 依赖清单解析
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ref:
    raw: str                 # 原始写法，用于展示
    owner: str               # 源表名或作业名
    name: str                # 字段/列名
    forced: Optional[str] = None  # "source" | "column" | None（自动判定）


@dataclass
class OutputSpec:
    job: str
    name: str
    groups: list[list[Ref]]


@dataclass
class DependencySet:
    outputs: dict[tuple[str, str], OutputSpec]
    job_names: set[str]
    external: set[tuple[str, str]]  # 对外数据集列


def _parse_ref(item: Any) -> Ref:
    if isinstance(item, str):
        raw, forced = item, None
    elif isinstance(item, dict):
        if "source" in item:
            raw, forced = item["source"], "source"
        elif "column" in item:
            raw, forced = item["column"], "column"
        else:
            raise FormatError(f"输入引用对象必须含 source 或 column 键：{item!r}")
        if not isinstance(raw, str):
            raise FormatError(f"输入引用必须是非空字符串：{item!r}")
    else:
        raise FormatError(f"输入引用必须是字符串或对象：{item!r}")
    if "." not in raw:
        raise FormatError(f"输入引用必须是 '属主.名' 形式：{raw!r}")
    owner, name = raw.split(".", 1)
    if not owner or not name:
        raise FormatError(f"输入引用必须是 '属主.名' 形式：{raw!r}")
    return Ref(raw=raw, owner=owner, name=name, forced=forced)


def _parse_groups(value: Any, where: str) -> list[list[Ref]]:
    """产出列的输入定义：允许
    - ["a.b", "c.d"]            => 单组，组内两个输入
    - [["a.b"], ["c.d", "e.f"]] => 两组（备用组）
    """
    if not isinstance(value, list) or not value:
        raise FormatError(f"{where} 的输入组必须是非空数组")
    if all(isinstance(g, list) for g in value):
        groups_raw = value
    elif all(not isinstance(g, list) for g in value):
        groups_raw = [value]
    else:
        raise FormatError(f"{where} 的输入组写法混用了数组与非数组元素")
    groups: list[list[Ref]] = []
    for g in groups_raw:
        if not g:
            raise FormatError(f"{where} 存在空的输入组")
        groups.append([_parse_ref(it) for it in g])
    return groups


def parse_dependencies(data: Any) -> DependencySet:
    """解析跨作业字段依赖 JSON：

    {
      "jobs": [
        {"name": "job_a",
         "outputs": {"列名": [["t.c1", "t.c2"], ["job_b.x"]], ...}},
        ...
      ],
      "external": ["job_a.某列", ...]   // 可选，标记对外数据集列
    }
    """
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list) or not data["jobs"]:
        raise FormatError("依赖清单必须是含非空 jobs 数组的 JSON 对象")
    outputs: dict[tuple[str, str], OutputSpec] = {}
    job_names: set[str] = set()
    for job in data["jobs"]:
        if not isinstance(job, dict) or not isinstance(job.get("name"), str) or not job["name"]:
            raise FormatError(f"每个作业必须含非空 name：{job!r}")
        jname = job["name"]
        if jname in job_names:
            raise FormatError(f"作业名重复：{jname}")
        job_names.add(jname)
        outs = job.get("outputs")
        if isinstance(outs, dict):
            items = [{"name": k, "groups": v} for k, v in outs.items()]
        elif isinstance(outs, list):
            items = outs
        else:
            raise FormatError(f"作业 {jname} 的 outputs 必须是对象或数组")
        if not items:
            raise FormatError(f"作业 {jname} 没有任何产出列")
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("name"), str) or not it["name"]:
                raise FormatError(f"作业 {jname} 的产出列必须含非空 name：{it!r}")
            key = (jname, it["name"])
            if key in outputs:
                raise FormatError(f"产出列重复定义：{jname}.{it['name']}")
            outputs[key] = OutputSpec(job=jname, name=it["name"],
                                      groups=_parse_groups(it.get("groups"), f"{jname}.{it['name']}"))
    external: set[tuple[str, str]] = set()
    for item in data.get("external", []) or []:
        if not isinstance(item, str) or "." not in item:
            raise FormatError(f"external 项必须是 '作业.列' 字符串：{item!r}")
        j, c = item.split(".", 1)
        if (j, c) not in outputs:
            raise FormatError(f"external 标记了依赖清单中不存在的产出列：{item}")
        external.add((j, c))
    return DependencySet(outputs=outputs, job_names=job_names, external=external)


# ---------------------------------------------------------------------------
# 三值逻辑
# ---------------------------------------------------------------------------

def _and_status(items: list[str]) -> str:
    if BROKEN in items:
        return BROKEN
    if UNKNOWN in items:
        return UNKNOWN
    return OK


def _or_status(items: list[str]) -> str:
    if OK in items:
        return OK
    if UNKNOWN in items:
        return UNKNOWN
    return BROKEN


# ---------------------------------------------------------------------------
# 单版评估器
# ---------------------------------------------------------------------------

class VersionEval:
    """针对某一版源字段清单评估全部产出列。

    other_sources 为对照版源清单，仅用于在原因里标注“本版移除”之类的对照信息。
    """

    def __init__(self, deps: DependencySet, sources: dict[str, set[str]],
                 other_sources: Optional[dict[str, set[str]]] = None):
        self.deps = deps
        self.sources = sources
        self.other_sources = other_sources or {}
        self.memo: dict[tuple[str, str], dict] = {}
        # 求值途中遇到循环时，栈上节点结果可能只是“途经值”，不做缓存
        self.tainted: set[tuple[str, str]] = set()

    # -- 引用解析 ---------------------------------------------------------

    def _classify(self, ref: Ref) -> tuple[str, Optional[str]]:
        """返回 (kind, 问题描述)。kind ∈ source/column/missing。"""
        is_job = ref.owner in self.deps.job_names
        is_table = ref.owner in self.sources
        if ref.forced == "source":
            if is_table:
                return "source", None
            return "missing", f"缺失引用：源表 {ref.owner} 不在本版源字段清单中"
        if ref.forced == "column":
            if is_job:
                if (ref.owner, ref.name) in self.deps.outputs:
                    return "column", None
                return "missing", f"缺失引用：作业 {ref.owner} 未产出列 {ref.name}"
            return "missing", f"缺失引用：作业 {ref.owner} 未在依赖清单中定义"
        # 自动判定：作业优先于同名源表
        if is_job:
            if (ref.owner, ref.name) in self.deps.outputs:
                return "column", None
            return "missing", f"缺失引用：作业 {ref.owner} 未产出列 {ref.name}"
        if is_table:
            return "source", None
        return "missing", f"缺失引用：{ref.owner} 既非本版源表也非已定义作业"

    def _source_status(self, ref: Ref) -> tuple[str, str]:
        cols = self.sources.get(ref.owner, set())
        if ref.name in cols:
            return OK, "源字段存在"
        if ref.name in self.other_sources.get(ref.owner, set()):
            return BROKEN, f"源字段缺失：{ref.raw} 在本版清单中已被移除（对照版存在）"
        return BROKEN, f"源字段缺失：{ref.raw} 不在本版源字段清单中"

    # -- 求值 -------------------------------------------------------------

    def eval_ref(self, ref: Ref, stack: list[tuple[str, str]], depth: int) -> dict:
        kind, problem = self._classify(ref)
        node: dict[str, Any] = {"ref": ref.raw, "kind": kind}
        if kind == "missing":
            node.update(status=UNKNOWN, reason=problem)
            return node
        if kind == "source":
            status, reason = self._source_status(ref)
            node.update(status=status, reason=reason)
            return node
        # 上游列引用
        child = self._eval_column((ref.owner, ref.name), stack, depth + 1)
        node["status"] = child["status"]
        node["reason"] = f"上游列 {ref.raw}：{STATUS_LABEL[child['status']]}"
        if depth < MAX_TRACE_DEPTH and child.get("groups") is not None:
            node["child"] = {"job": ref.owner, "column": ref.name,
                             "status": child["status"], "reason": child["reason"],
                             "groups": child["groups"]}
        elif child.get("groups") is None:
            node["reason"] = child["reason"]  # 循环等无展开情形，透传原因
        else:
            node["truncated"] = True
        return node

    def _eval_column(self, key: tuple[str, str], stack: list[tuple[str, str]], depth: int) -> dict:
        if key in self.memo:
            return self.memo[key]
        if key in stack:
            idx = stack.index(key)
            self.tainted.update(stack[idx:])
            path = " -> ".join(f"{j}.{c}" for j, c in stack[idx:] + [key])
            return {"status": UNKNOWN, "reason": f"循环依赖且无源支撑：{path}", "groups": None}
        if depth > MAX_EVAL_DEPTH:
            return {"status": UNKNOWN, "reason": "依赖层级过深，无法完全展开", "groups": None}
        spec = self.deps.outputs[key]
        stack.append(key)
        group_results = []
        for i, group in enumerate(spec.groups, 1):
            inputs = [self.eval_ref(r, stack, depth) for r in group]
            gstatus = _and_status([it["status"] for it in inputs])
            bad = [it["ref"] for it in inputs if it["status"] == BROKEN]
            unk = [it["ref"] for it in inputs if it["status"] == UNKNOWN]
            if gstatus == OK:
                greason = "组内全部输入可用"
            elif gstatus == BROKEN:
                greason = "输入不可用：" + "、".join(bad)
            else:
                greason = "存在待核实输入：" + "、".join(unk)
            group_results.append({"index": i, "status": gstatus, "reason": greason,
                                  "inputs": inputs})
        stack.pop()
        status = _or_status([g["status"] for g in group_results])
        ok_groups = [g["index"] for g in group_results if g["status"] == OK]
        if status == OK:
            reason = f"存在完整可用组（第 {'、'.join(map(str, ok_groups))} 组）"
            if len(ok_groups) > 1 or len(spec.groups) > 1:
                reason += "，含备用组" if len(spec.groups) > 1 else ""
        elif status == BROKEN:
            reason = "所有输入组均不可用"
        else:
            reason = "无完整可用组，仅能依靠缺失引用或无源支撑的循环产出"
        result = {"status": status, "reason": reason, "groups": group_results}
        if key not in self.tainted:
            self.memo[key] = result
        return result

    def evaluate_all(self) -> dict[tuple[str, str], dict]:
        return {key: self._eval_column(key, [], 0) for key in self.deps.outputs}


# ---------------------------------------------------------------------------
# 对照两版构建报告
# ---------------------------------------------------------------------------

def build_report(deps: DependencySet,
                 sources_before: dict[str, set[str]],
                 sources_after: dict[str, set[str]]) -> dict:
    ev_before = VersionEval(deps, sources_before, other_sources=sources_after)
    ev_after = VersionEval(deps, sources_after, other_sources=sources_before)
    res_before = ev_before.evaluate_all()
    res_after = ev_after.evaluate_all()

    # 被其他产出列消费的列（用于识别“最终/对外”列）
    consumed: set[tuple[str, str]] = set()
    for spec in deps.outputs.values():
        for group in spec.groups:
            for ref in group:
                if (ref.owner, ref.name) in deps.outputs:
                    consumed.add((ref.owner, ref.name))

    rank = {BROKEN: 0, UNKNOWN: 1, OK: 2}
    columns = []
    for key, spec in deps.outputs.items():
        rb, ra = res_before[key], res_after[key]
        changed = rb["status"] != ra["status"]
        columns.append({
            "job": spec.job,
            "column": spec.name,
            "name": f"{spec.job}.{spec.name}",
            "is_final": key not in consumed,
            "is_external": key in deps.external,
            "changed": changed,
            "before": {"status": rb["status"], "reason": rb["reason"], "groups": rb["groups"]},
            "after": {"status": ra["status"], "reason": ra["reason"], "groups": ra["groups"]},
        })
    columns.sort(key=lambda c: (rank[c["after"]["status"]], not c["changed"], c["name"]))

    def count(results, status):
        return sum(1 for r in results.values() if r["status"] == status)

    summary = {
        "total": len(columns),
        "before": {s: count(res_before, s) for s in (OK, BROKEN, UNKNOWN)},
        "after": {s: count(res_after, s) for s in (OK, BROKEN, UNKNOWN)},
        "changed": sum(1 for c in columns if c["changed"]),
        "final_total": sum(1 for c in columns if c["is_final"] or c["is_external"]),
        "final_broken_after": sum(1 for c in columns
                                  if (c["is_final"] or c["is_external"]) and c["after"]["status"] == BROKEN),
        "final_unknown_after": sum(1 for c in columns
                                   if (c["is_final"] or c["is_external"]) and c["after"]["status"] == UNKNOWN),
    }
    return {"summary": summary, "columns": columns,
            "labels": STATUS_LABEL}
