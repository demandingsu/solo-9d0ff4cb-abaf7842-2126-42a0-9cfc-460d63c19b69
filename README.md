# 环境监测数仓 · 原始站点表字段变更核查

值班数据工程师在原始站点表字段调整上线前，用本工具核查**哪些对外数据集列可能断供**：
上传「变更前源字段清单」「变更后源字段清单」「跨作业字段依赖」三份 JSON，
系统对照两版逐列给出 **正常 / 断供 / 待核实** 判定和完整依赖路径。

技术栈：FastAPI + SQLite + 原生 HTML/JS 单页，Docker Compose 一键运行。

## 快速开始

```bash
docker compose up --build
# 打开 http://localhost:8000
```

页面上可直接点「载入内置示例核查」一键体验，或下载示例文件修改后上传：
`examples/sources_before.json`、`examples/sources_after.json`、`examples/dependencies.json`。

本地无 Docker 运行：

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

运行单元测试（仅标准库，无需安装依赖）：

```bash
python3 -m unittest discover -s tests -v
```

## 判定口径

每个产出列允许 **多组输入**：

- **组内全部输入可用，该组才可用；任一组可用即可产出** → `正常`
  （主用组失效但有完整备用组时，仍然正常，判定说明会标注「含备用组」）
- 所有输入组都确定不可用（源字段缺失、或上游列自身已断供）→ `断供`
- 仅能依靠 **缺失引用**（依赖清单中不存在的作业/列、既非源表也非作业的引用）
  或 **无源支撑的循环** 才能产出 → `待核实`

源字段缺失时会对照另一版清单标注「本版已移除（对照版存在）」还是「两版均无」。
判定沿依赖链传播：上游断供会导致下游断供；上游待核实只会让下游待核实，不会升级为断供。
「最终列」= 未被任何其他产出列消费的列；`external` 标记的列视为对外数据集列，页面默认聚焦这两类。

## 版本与一致性

- 核查（解析 + 评估）全部在写库前完成，**核查进行中旧版照常可查**；
- 成功后在一个事务内整版替换（旧版归档），**失败保留旧版**并在历史中留痕；
- 每个快照自包含三份输入与判定报告，查询只读 `active` 的唯一版本，**绝不混用两版**；
- SQLite 开启 WAL，写入期间并发读取不受阻塞。

## 输入文件格式

**源字段清单**（变更前/后各一份，三种写法等价）：

```json
{ "tables": { "raw_air_station_hourly": ["station_id", "pm25"] } }
{ "raw_air_station_hourly": ["station_id", "pm25"] }
[ "raw_air_station_hourly.station_id", "raw_air_station_hourly.pm25" ]
```

**跨作业字段依赖**：

```json
{
  "jobs": [
    {
      "name": "job_air_quality_hourly",
      "outputs": {
        "wind_speed_avg": [
          ["raw_air_station_hourly.wind_speed"],
          ["job_meteo_clean.wind_speed"]
        ]
      }
    }
  ],
  "external": ["job_daily_report.report_wind"]
}
```

- `outputs.列名` 的值是 **组的数组**：外层每一项是一组备用输入，组内是所有必须同时可用的输入；
  简写 `"col": ["t.a", "t.b"]`（元素非数组）等价于单组 `[["t.a", "t.b"]]`。
- 输入引用 `"属主.名"` 自动判定：属主是已定义作业 → 上游列；是源表 → 源字段；都不是 → 缺失引用（待核实）。
  同名歧义时可用 `{"source": "t.c"}` 或 `{"column": "j.c"}` 强制指定。
- `external`（可选）列出对外数据集列，必须引用已定义的产出列。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/snapshots` | multipart 上传 `sources_before` / `sources_after` / `dependencies` + 可选 `name`；成功整版发布，失败 400 且保留旧版 |
| GET | `/api/snapshots/current` | 现役版本全量内容（输入 + 判定报告） |
| GET | `/api/snapshots` | 版本历史（含失败记录） |
| GET | `/examples/{name}` | 三份示例文件下载 |
| GET | `/` | 核查页面 |

## 目录结构

```
app/
  main.py            FastAPI 入口与接口
  evaluator.py       解析 + 三值判定 + 依赖路径生成（核心，纯标准库）
  store.py           SQLite 快照存储（整版原子替换、失败留痕、WAL）
  static/index.html  核查页面
examples/            本地示例（覆盖断供/备用组/缺失引用/无源循环全部情形）
tests/               evaluator 单元测试（19 例）
Dockerfile / compose.yaml / requirements.txt
```
