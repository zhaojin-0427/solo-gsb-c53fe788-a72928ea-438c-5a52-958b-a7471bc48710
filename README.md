# 建筑无障碍通路核验台

本地 Web 应用，用于核验建筑内部有向路网的轮椅无障碍通路。技术栈：

- **后端**：Python 3.11+ · FastAPI · SQLite · NetworkX（有向多重图 + 时间依赖 Dijkstra）
- **前端**：原生 HTML / CSS / JavaScript（SVG 逐层绘制楼层图，无任何前端框架与构建步骤）
- **部署**：Dockerfile + docker-compose（SQLite 数据经命名卷持久化）

## 一、快速启动

### 方式 A：Docker Compose（推荐）

```bash
docker compose up -d --build
```

启动后浏览器访问：

- 应用首页：<http://localhost:8000/>
- 健康检查：<http://localhost:8000/healthz>
- OpenAPI 文档：<http://localhost:8000/docs>

停止：`docker compose down`；数据保留在命名卷 `accessibility-data` 中，
删除数据用 `docker compose down -v`。

### 方式 B：本地直接运行

```bash
python3 -m pip install -r requirements.txt
DATABASE_PATH=./data.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 二、使用流程（页面四个页签）

1. **路网导入**：点“载入示例路网”可加载仓库自带的 `sample_network.json`
   （B1/F1/F2 + 仅楼梯可达的夹层，含电梯、12% 陡坡、0.72m 窄廊）；
   也可选择本地 JSON 文件或直接粘贴文本。每次成功导入都生成一个**不可变新版本**，
   旧版本可随时切回。
2. **封闭时段**：选择通道边，填写当地日期时刻与 UTC 时区偏移（默认 +08:00），
   登记半开区间 `[开始, 结束)` 的封闭时段；可随时删除。
3. **路线核验**：选起点、终点、出发时刻（当地时刻 + 时区）、最小净宽(m)、
   最大坡度(%)，查询出发后 **24 小时**内的最早可达轮椅路线；
   页面逐层显示路线、边序号、节点等待时长与完整时间线；不可达时逐条列出阻断原因。
4. **核验记录**：每次查询都生成一条固化记录（内含当时的**路网快照**与
   **封闭时段快照**）；之后导入新路、改封闭时段都不会改变旧记录的结果，
   点“查看固化结果”可用当时的快照重新渲染楼层图。

## 三、导入 JSON 格式

```json
{
  "name": "楼栋名称",
  "nodes": [
    {"id": "F1_ENT", "name": "一层主入口", "floor": "F1",
     "x": 50, "y": 120, "type": "entrance"}
  ],
  "edges": [
    {"id": "c1", "name": "大厅走廊",
     "source": "F1_ENT", "target": "F1_HALL",
     "type": "corridor",
     "length": 8, "traverse_seconds": 8,
     "clear_width": 2.2, "slope": 0, "one_way": false}
  ]
}
```

- 节点：`id`、`floor`（字符串，支持 `B1`/`F1`/`M2` 等排序）、`x`/`y`（楼层平面坐标）、
  `type ∈ entrance | exit | room | junction`。
- 边：`source`/`target` 必须指向已存在的节点；
  `type ∈ corridor | elevator | ramp | stairs | door | other`；
  `length`(米)、`traverse_seconds`(通行秒数)、`clear_width`(净宽米)、
  `slope`(坡度百分数 0–100)；`one_way:false` 表示双向，导入时自动生成反向边。
- 所有时间输入（封闭时段、出发时刻）必须**带时区偏移**，如
  `2026-10-01T08:00:00+08:00` 或 `2026-10-01T00:00:00Z`。

### 导入校验规则（违反即整体拒绝，并列出全部错误）

- 悬空引用：边的端点不存在 → `dangling_node`；
- 悬空节点：没有任何边关联的孤立节点 → `isolated_node`；
- 非法跨层：两端楼层不同的边，其类型只允许 `elevator` / `ramp` / `stairs`；
  普通走廊/门等直接跨层 → `illegal_cross_floor`；
- 字段类型/取值（重复 id、缺坐标、负数、坡度 >100 等）逐项报错。

## 四、核验算法与规则

- 静态可行边：排除 `stairs`（楼梯轮椅不可走）；净宽 `< 最小净宽`、
  坡度 `> 最大坡度` 的边不可走；跨层移动只可能经电梯或坡道发生。
- 时间维度：每条边登记若干半开封闭区间并自动合并；允许在**节点处等候**，
  进入一条边的时刻必须使整个通行区间 `[enter, enter+通行秒数)` 与封闭时段不相交。
- 求解：时间依赖 FIFO 网络上的多标签 Dijkstra（以 NetworkX 有向多重图为底），
  标签按 `(到达时刻, 总路程)` 非支配保留，搜索截止于出发后 86400 秒。
- 择优顺序：**最早到达** → 总路程最短 → 边 ID 序列字典序最小。
- 不可达诊断：静态不通时给出跨越“起点可达集 → 终点可达集”的具体割边及原因
  （楼梯 / 净宽不足 / 坡度过大）；因封闭不通时给出关键封闭边、受阻时刻与
  下一个开放时刻。

## 五、主要 HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/networks/active` | 当前路网版本完整内容 |
| POST | `/api/networks/import` | 校验并导入为新版本（自动激活） |
| POST | `/api/networks/{id}/activate` | 切换当前版本 |
| GET/POST/DELETE | `/api/closures[/{id}]` | 当前版本封闭时段增删查 |
| POST | `/api/route/query` | 查询并固化一条核验记录 |
| GET | `/api/records[/{id}]` | 核验记录列表 / 含双快照的详情 |

## 六、测试

```bash
python3 test_planning.py
```

覆盖：悬空/跨层校验、楼梯禁行、电梯与坡道跨层、净宽与坡度过滤、
封闭时段节点等候（半开区间边界）、24 小时窗口超时、
路程与边 ID 平局择优、记录固化不受后续修改影响。
