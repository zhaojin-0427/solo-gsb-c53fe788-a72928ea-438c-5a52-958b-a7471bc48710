# 建筑无障碍通路核验台

本地 Web 应用，用于核验轮椅使用者在建筑内的无障碍通行路线。基于 Python + FastAPI + SQLite + NetworkX 管理有向路网，浏览器使用原生 JavaScript 呈现楼层图。

## 功能特性

- **路网导入**：导入含楼层坐标、出入口、房间和通道的 JSON 文件；系统拒绝悬空节点和非法跨层连接（跨层仅允许电梯或坡道）。
- **封闭时段登记**：为通道登记带时区的封闭时段，支持跨时区。
- **轮椅路线查询**：按起终点、出发时刻、最小净宽、最大坡度查询路线。楼梯不可走，跨层仅允许电梯或坡道，可在节点等候，但通行全程不得与封闭时段相交。在出发后 24 小时内求最早到达，平局依次按路程和边 ID 序列择优。
- **楼层图呈现**：逐层标出路线与等待时间，不可达时列出阻断原因。
- **核验记录固化**：每次查询固化路网及封闭时段版本，后续修改不改变旧结果。

## 启动方式

### Docker Compose 一键启动（推荐）

```bash
docker compose up --build
```

启动后访问 <http://localhost:8000>。

### 本地开发启动

```bash
pip install -r backend/requirements.txt
cd backend
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

访问 <http://localhost:8000>。

## 使用流程

1. **导入路网**：在左侧面板点击「选择文件」，选择示例文件 `data/sample_network.json`（或自行编写的路网 JSON），点击「导入路网」。
2. **登记封闭时段**：在「封闭时段登记」中填写通道 ID、起止时间和时区，点击「登记封闭时段」。
3. **查询路线**：填写起点、终点、出发时刻、最小净宽和最大坡度，点击「查询路线」。
4. **查看结果**：右侧「核验结果」面板会逐层列出路线与等待时间；不可达时列出阻断原因。
5. **查看历史记录**：右侧「历史核验记录」列出所有核验记录，点击可查看当时的路网与结果。

## 路网 JSON 格式

```json
{
  "floors": [
    { "id": "f1", "name": "1F", "level": 0 }
  ],
  "nodes": [
    { "id": "n1", "floor_id": "f1", "x": 0, "y": 0, "kind": "entrance", "name": "主入口" }
  ],
  "edges": [
    {
      "id": "e1",
      "from": "n1",
      "to": "n2",
      "type": "corridor",
      "length": 10,
      "seconds": 12,
      "width": 1.8,
      "slope": 0
    }
  ]
}
```

### 字段说明

- **floors**：楼层列表。`id` 唯一标识，`name` 显示名称，`level` 楼层序号。
- **nodes**：节点列表。
  - `kind`：节点类型，可选 `entrance`（出入口）、`exit`（出口）、`room`（房间）、`junction`（节点）、`elevator`（电梯）。
  - `x` / `y`：楼层图坐标。
- **edges**：通道列表。
  - `type`：通道类型，可选 `corridor`（通道）、`stairs`（楼梯）、`elevator`（电梯）、`ramp`（坡道）。
  - `length`：长度（米）。
  - `seconds`：通行秒数。
  - `width`：净宽（米）。
  - `slope`：坡度（度）。

### 校验规则

- 节点 ID、通道 ID、楼层 ID 不可重复。
- 通道的起终点必须存在。
- 不允许悬空节点（未连接任何通道的节点）。
- 跨楼层通道的类型必须是 `elevator` 或 `ramp`；同楼层通道类型不可为 `elevator` 或 `ramp`。
- 长度、通行秒数、净宽不可为负；坡度在 0~90 之间。

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/network/import` | 导入路网 JSON |
| GET | `/api/network` | 获取当前路网 |
| GET | `/api/network/versions` | 列出路网版本 |
| GET | `/api/network/{version}` | 获取指定版本路网 |
| POST | `/api/closures` | 登记封闭时段 |
| GET | `/api/closures` | 列出封闭时段 |
| DELETE | `/api/closures/{id}` | 删除封闭时段 |
| POST | `/api/route/query` | 查询轮椅路线 |
| GET | `/api/records` | 列出核验记录 |
| GET | `/api/records/{id}` | 获取核验记录详情 |

## 技术栈

- 后端：Python 3.12、FastAPI、SQLite、NetworkX
- 前端：原生 JavaScript、SVG
- 部署：Docker、Docker Compose
