"""evaluator 判定口径单元测试（仅依赖标准库）：
python3 -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evaluator import (BROKEN, OK, UNKNOWN, FormatError, build_report,
                           parse_dependencies, parse_sources)


def make_deps(outputs_by_job, external=None):
    return parse_dependencies({
        "jobs": [{"name": j, "outputs": o} for j, o in outputs_by_job.items()],
        "external": external or [],
    })


def report_for(deps, before, after):
    return build_report(deps, parse_sources(before), parse_sources(after))


def col(rep, name):
    return next(c for c in rep["columns"] if c["name"] == name)


class TestParse(unittest.TestCase):
    def test_sources_forms(self):
        self.assertEqual(parse_sources({"tables": {"t": ["a"]}}), {"t": {"a"}})
        self.assertEqual(parse_sources({"t": ["a", "b"]}), {"t": {"a", "b"}})
        self.assertEqual(parse_sources(["t.a", "t.b", "u.c"]),
                         {"t": {"a", "b"}, "u": {"c"}})

    def test_sources_bad(self):
        with self.assertRaises(FormatError):
            parse_sources({})
        with self.assertRaises(FormatError):
            parse_sources({"t": "not-a-list"})
        with self.assertRaises(FormatError):
            parse_sources(["no-dot"])

    def test_deps_bad(self):
        with self.assertRaises(FormatError):
            parse_dependencies({"jobs": []})
        with self.assertRaises(FormatError):  # 重复作业名
            parse_dependencies({"jobs": [{"name": "j", "outputs": {"a": ["t.x"]}},
                                         {"name": "j", "outputs": {"b": ["t.x"]}}]})
        with self.assertRaises(FormatError):  # external 指向不存在的列
            make_deps({"j": {"a": ["t.x"]}}, external=["j.nope"])
        with self.assertRaises(FormatError):  # 空输入组
            make_deps({"j": {"a": [[]]}})

    def test_single_group_shorthand(self):
        deps = make_deps({"j": {"a": ["t.x", "t.y"]}})
        spec = deps.outputs[("j", "a")]
        self.assertEqual(len(spec.groups), 1)
        self.assertEqual(len(spec.groups[0]), 2)


class TestVerdicts(unittest.TestCase):
    def setUp(self):
        self.before = {"t": ["keep", "drop"], "u": ["uu"]}
        self.after = {"t": ["keep"], "u": ["uu"]}

    def test_ok_unchanged(self):
        rep = report_for(make_deps({"j": {"a": [["t.keep"]]}}), self.before, self.after)
        c = col(rep, "j.a")
        self.assertEqual((c["before"]["status"], c["after"]["status"]), (OK, OK))
        self.assertFalse(c["changed"])

    def test_broken_source_removed(self):
        rep = report_for(make_deps({"j": {"a": [["t.drop"]]}}), self.before, self.after)
        c = col(rep, "j.a")
        self.assertEqual(c["after"]["status"], BROKEN)
        self.assertTrue(c["changed"])
        self.assertIn("已被移除", c["after"]["groups"][0]["inputs"][0]["reason"])

    def test_backup_group_keeps_ok(self):
        deps = make_deps({
            "j": {"a": [["t.drop"], ["t.keep", "u.uu"]]},
        })
        rep = report_for(deps, self.before, self.after)
        c = col(rep, "j.a")
        self.assertEqual(c["after"]["status"], OK)
        self.assertIn("备用组", c["after"]["reason"])

    def test_group_requires_all_inputs(self):
        deps = make_deps({"j": {"a": [["t.keep", "t.drop"]]}})
        rep = report_for(deps, self.before, self.after)
        self.assertEqual(col(rep, "j.a")["after"]["status"], BROKEN)

    def test_missing_reference_unknown(self):
        deps = make_deps({"j": {"a": [["ghost_job.x"]]}})
        rep = report_for(deps, self.before, self.after)
        c = col(rep, "j.a")
        self.assertEqual((c["before"]["status"], c["after"]["status"]), (UNKNOWN, UNKNOWN))
        self.assertEqual(c["after"]["groups"][0]["inputs"][0]["kind"], "missing")

    def test_missing_column_of_known_job_unknown(self):
        deps = make_deps({"j": {"a": [["k.nope"]]}, "k": {"b": [["t.keep"]]}})
        rep = report_for(deps, self.before, self.after)
        self.assertEqual(col(rep, "j.a")["after"]["status"], UNKNOWN)

    def test_cycle_without_source_unknown(self):
        deps = make_deps({"a": {"x": [["b.x"]]}, "b": {"x": [["a.x"]]}})
        rep = report_for(deps, self.before, self.after)
        c = col(rep, "a.x")
        self.assertEqual(c["after"]["status"], UNKNOWN)
        self.assertIn("循环", c["after"]["reason"])  # 列级结论指出无源循环
        # 依赖路径嵌套层沿 a.x -> b.x -> a.x 回到起点，回边节点给出具体循环链路
        inp = c["after"]["groups"][0]["inputs"][0]  # ref b.x
        nested = inp["child"]["groups"][0]["inputs"][0]  # ref a.x（环上回边）
        self.assertIn("循环", nested["child"]["reason"])
        self.assertIn("a.x -> b.x -> a.x", nested["child"]["reason"])

    def test_cycle_with_source_group_ok(self):
        deps = make_deps({"a": {"x": [["a.x"], ["t.keep"]]}})
        rep = report_for(deps, self.before, self.after)
        self.assertEqual(col(rep, "a.x")["after"]["status"], OK)

    def test_self_cycle_and_missing_input_broken(self):
        # 组内一个确定缺失 + 一个循环引用 => 组确定不可用 => 断供
        deps = make_deps({"a": {"x": [["a.x", "t.drop"]]}})
        rep = report_for(deps, self.before, self.after)
        self.assertEqual(col(rep, "a.x")["after"]["status"], BROKEN)

    def test_broken_propagates_upstream(self):
        deps = make_deps({
            "mid": {"m": [["t.drop"]]},
            "top": {"a": [["mid.m"]]},
        })
        rep = report_for(deps, self.before, self.after)
        self.assertEqual(col(rep, "top.a")["after"]["status"], BROKEN)

    def test_unknown_propagates_as_unknown_not_broken(self):
        deps = make_deps({
            "mid": {"m": [["ghost.x"]]},
            "top": {"a": [["mid.m", "t.keep"]]},
        })
        rep = report_for(deps, self.before, self.after)
        self.assertEqual(col(rep, "top.a")["after"]["status"], UNKNOWN)

    def test_forced_ref_forms(self):
        deps = make_deps({"j": {"a": [[{"source": "t.keep"}]],
                              "b": [[{"column": "j.a"}]]}})
        rep = report_for(deps, self.before, self.after)
        self.assertEqual(col(rep, "j.b")["after"]["status"], OK)

    def test_final_and_external_flags(self):
        deps = make_deps({
            "mid": {"m": [["t.keep"]]},
            "top": {"a": [["mid.m"]], "b": [["t.keep"]]},
        }, external=["top.a"])
        rep = report_for(deps, self.before, self.after)
        self.assertFalse(col(rep, "mid.m")["is_final"])
        self.assertTrue(col(rep, "top.a")["is_final"])
        self.assertTrue(col(rep, "top.a")["is_external"])
        self.assertTrue(col(rep, "top.b")["is_final"])
        self.assertFalse(col(rep, "top.b")["is_external"])

    def test_summary_counts(self):
        deps = make_deps({
            "j": {"ok1": [["t.keep"]], "bad": [["t.drop"]], "unk": [["ghost.x"]]},
        })
        rep = report_for(deps, self.before, self.after)
        s = rep["summary"]
        self.assertEqual(s["after"], {OK: 1, BROKEN: 1, UNKNOWN: 1})
        self.assertEqual(s["before"], {OK: 2, BROKEN: 0, UNKNOWN: 1})
        self.assertEqual(s["changed"], 1)


class TestCyclicBackupRegression(unittest.TestCase):
    """回归：20 个作业的备用输入相互引用成环时，核查必须在线性时间内完成，
    且“组内全部可用、组间任一可用、无源循环待核实”的判定口径保持不变。"""

    def _ring_deps(self, n=20, fanout=2):
        jobs = []
        for i in range(n):
            j = f"job_{i:02d}"
            # 第 2 备用组引用前序作业的同名列，跨作业相互引用并形成环
            refs = [f"job_{(i - k - 1) % n:02d}.x" for k in range(fanout)]
            jobs.append({"name": j,
                         "outputs": {"x": [["t.keep"], refs]}})
        return parse_dependencies({"jobs": jobs})

    def test_ring_with_source_fallback_is_ok_and_fast(self):
        import json
        import time
        deps = self._ring_deps(20)
        t0 = time.perf_counter()
        rep = report_for(deps, {"t": ["keep"]}, {"t": ["keep"]})
        self.assertLess(time.perf_counter() - t0, 5.0, "环装备用组核查不应指数级膨胀")
        # 源字段兜底可用 -> 组间任一可用 -> 全部正常
        self.assertEqual({c["name"]: c["after"]["status"] for c in rep["columns"]},
                         {f"job_{i:02d}.x": OK for i in range(20)})
        # 报告体积随列数线性：紧凑引用收敛重复子树，而不是整棵子树反复内嵌
        # （修复前同场景两版报告约 9.6MB 且耗时近 30 秒）
        self.assertLess(len(json.dumps(rep, ensure_ascii=False)), 512 * 1024)

    def test_ring_growth_is_linear(self):
        import json
        sizes = {}
        for n in (20, 40, 80):
            deps = self._ring_deps(n)
            rep = report_for(deps, {"t": ["keep"]}, {"t": ["keep"]})
            sizes[n] = len(json.dumps(rep, ensure_ascii=False))
        # 规模翻倍，报告体积不应超过 ~4 倍（线性而非指数；留足常数余量）
        self.assertLess(sizes[80], sizes[20] * 8)
        self.assertLess(sizes[40], sizes[20] * 4)

    def test_ring_without_source_support_is_unknown(self):
        deps = self._ring_deps(20)
        rep = report_for(deps, {"t": ["gone"]}, {"t": ["gone"]})
        # 无源支撑的纯循环 -> 全部待核实；定点计算不受路径展开深度限制，判定始终精确
        self.assertTrue(all(c["after"]["status"] == UNKNOWN for c in rep["columns"]))
        self.assertTrue(all("循环" in c["after"]["reason"] for c in rep["columns"]))

    def test_short_cycle_shows_explicit_backedge_path(self):
        # 环长小于路径展开上限时，环上回边给出具体循环链路（深度更大的环则在列级原因标注）
        n = 6
        jobs = [{"name": f"j{i}", "outputs": {"x": [[f"j{(i - 1) % n}.x"]]}}
                for i in range(n)]
        deps = parse_dependencies({"jobs": jobs})
        rep = report_for(deps, {"t": ["gone"]}, {"t": ["gone"]})
        self.assertTrue(all(c["after"]["status"] == UNKNOWN for c in rep["columns"]))

        def backedge(node):
            for g in node.get("groups") or []:
                for inp in g["inputs"]:
                    child = inp.get("child")
                    if child is not None and child.get("groups") is None \
                            and "循环" in child["reason"]:
                        return child["reason"]
                    if child is not None:
                        found = backedge(child)
                        if found:
                            return found
            return None

        self.assertTrue(any(backedge(c["after"]) for c in rep["columns"]))

    def test_ring_source_removed_leaves_unknown_not_broken(self):
        deps = self._ring_deps(20)
        # 备用组只是相互引用（无源），源字段又在本版移除：没有确定断供，只能待核实
        rep = report_for(deps, {"t": ["keep"]}, {"t": ["gone"]})
        self.assertTrue(all(c["after"]["status"] == UNKNOWN for c in rep["columns"]))

    def test_group_and_backup_semantics_preserved_in_ring(self):
        # 组内一个确定断供 + 环引用 => 该组断供；但源字段备用组可用 => 列仍正常
        deps = parse_dependencies({"jobs": [
            {"name": "a", "outputs": {"x": [["a.x", "t.drop"], ["t.keep"]]}},
        ]})
        rep = report_for(deps, {"t": ["keep", "drop"]}, {"t": ["keep"]})
        c = col(rep, "a.x")
        self.assertEqual(c["after"]["status"], OK)
        groups = c["after"]["groups"]
        self.assertEqual(groups[0]["status"], BROKEN)  # 组内含确定缺失输入
        self.assertEqual(groups[1]["status"], OK)      # 备用组完整可用

    def test_reference_nodes_keep_authoritative_status(self):
        import json
        deps = self._ring_deps(20)
        rep = report_for(deps, {"t": ["keep"]}, {"t": ["keep"]})
        blob = json.dumps(rep, ensure_ascii=False)
        # 紧凑引用节点仍带状态，前端可照常渲染徽标；引用节点不应内嵌 groups 子树
        def walk(node):
            for g in node.get("groups") or []:
                for inp in g["inputs"]:
                    child = inp.get("child")
                    if child is not None:
                        self.assertIn(child["status"], (OK, BROKEN, UNKNOWN))
                        if child.get("reference"):
                            self.assertEqual(child["groups"], [])
                        walk(child)
        for c in rep["columns"]:
            walk(c["after"])
        self.assertIn("reference", blob)


class TestExamples(unittest.TestCase):
    def test_example_files(self):
        base = Path(__file__).resolve().parent.parent / "examples"
        import json
        before = json.loads((base / "sources_before.json").read_text(encoding="utf-8"))
        after = json.loads((base / "sources_after.json").read_text(encoding="utf-8"))
        deps = parse_dependencies(json.loads((base / "dependencies.json").read_text(encoding="utf-8")))
        rep = build_report(deps, parse_sources(before), parse_sources(after))
        expect_after = {
            "job_air_quality_hourly.pm25_avg": OK,
            "job_air_quality_hourly.co_avg": BROKEN,          # co 下线且无备用
            "job_air_quality_hourly.wind_speed_avg": OK,      # 备用组完整
            "job_air_quality_hourly.station_key": OK,
            "job_meteo_clean.wind_speed": OK,
            "job_meteo_clean.visibility_avg": BROKEN,         # visibility 下线
            "job_ghost_ref.aqi_extra": UNKNOWN,               # 缺失引用
            "job_cycle_a.x": UNKNOWN,                         # 无源循环
            "job_cycle_b.x": UNKNOWN,
            "job_daily_report.report_pm25": OK,
            "job_daily_report.report_co": BROKEN,             # 断供沿链传播
            "job_daily_report.report_wind": OK,
            "job_daily_report.report_visibility": BROKEN,
            "job_daily_report.report_aqi_extra": UNKNOWN,     # 待核实沿链传播
            "job_daily_report.station_name": OK,
        }
        self.assertEqual({c["name"]: c["after"]["status"] for c in rep["columns"]}, expect_after)
        # 变更前全部正常（除本就待核实的两类）
        for name, c in ((c["name"], c) for c in rep["columns"]):
            if "ghost" in name or "cycle" in name or "aqi_extra" in name:
                self.assertEqual(c["before"]["status"], UNKNOWN, name)
            else:
                self.assertEqual(c["before"]["status"], OK, name)


if __name__ == "__main__":
    unittest.main()
