# 地铁无障碍进出供数服务

电梯停运时，导航需要知道各站台还能不能无障碍进出。本服务做三件事：

1. 数据员按站上传 JSON：出入口、站台、可供轮椅通行的**有向**通道，通道可标一台电梯；
2. 发布时逐站台核算“最小用梯组合”，新图与组合表一起生效；
3. 网页可随时改停运名单，按名单判定各站台能否进出，并给出**最小复运组合**。

## 快速启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

数据保存在 named volume `nav-data` 的 SQLite 文件里（`/data/nav.db`）。

首次可以在容器里灌一份演示数据：

```bash
docker compose exec nav flask --app app.wsgi seed-demo
# 然后打开 http://localhost:8000/admin/publish 点“核算并发布”
```

## 供数接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/version` | 当前生效版本与站点列表 |
| GET | `/api/v1/stations` | 各站一句话状态摘要 |
| GET | `/api/v1/stations/<station_id>` | 单站完整判定（导航端按站取用） |
| GET | `/api/v1/outages` | 当前停运名单 |

`/api/v1/stations/<id>` 的返回（节选）：

```json
{
  "station_id": "S1",
  "name": "人民广场",
  "version_id": 3,
  "entrances": ["A", "B", "C"],
  "elevators": ["S1-E1", "S1-E2", "S1-E3", "S1-E4"],
  "outages_applied": ["S1-E2"],
  "outages_not_in_station": [],
  "platforms": [
    {
      "platform": "P1",
      "status": "blocked",
      "enterable": false,
      "exitable": false,
      "entrance_to_platform": {
        "possible": true,
        "accessible": false,
        "no_elevator_needed": false,
        "minimal_elevator_sets": [],
        "minimal_recovery_sets": [["S1-E2"]]
      },
      "platform_to_entrance": { "...": "同上" }
    }
  ]
}
```

判定字段含义：

* `status`
  * `ok`：两个方向都通，且需要电梯；
  * `no_elevator`：两个方向都通，且不用电梯；
  * `blocked`：图中本来能通，因当前停运名单而不通；
  * `unreachable`：完全不通——图中某方向即使全部电梯可用也不通。
* `possible`：该方向在图上是否存在可行通路（与停运名单无关）。
* `accessible`：`possible` 且当前名单下存在可行组合。
* `no_elevator_needed`：不用任何电梯即可通行（与“完全不通”区分）。
* `minimal_elevator_sets`：当前可用电梯下，所有**最小用梯组合**；每组去掉任一台即不通。
  `[[]]` 表示无须电梯；`[]` 表示当前不可通行。
* `minimal_recovery_sets`：该方向因停运而不通时，所有**最小复运组合**；
  每组少恢复任一台都仍不通。`null` 表示方向本来就通，或无论怎么复运都不通。

## 数据格式

每站一份 JSON：

```json
{
  "station_id": "S1",
  "name": "人民广场",
  "entrances": ["A", "B", "C"],
  "platforms": ["P1", "P2"],
  "passages": [
    {"id": "a-p1", "from": "entrance:A", "to": "platform:P1", "elevator": "S1-E1"},
    {"id": "p1-a", "from": "platform:P1", "to": "entrance:A", "elevator": "S1-E1"},
    {"id": "c-p1", "from": "entrance:C", "to": "platform:P1"}
  ]
}
```

* 节点写作 `entrance:<编号>` / `platform:<编号>`；
* `elevator` 可省略或为 `null`（表示该通道无须电梯）；
* 同一台电梯可出现在多条通道上；
* **每站最多 10 台电梯**（文档中出现的不同电梯编号数）；
* 通道是有向的：要双向通行请分别写两条。

## 工作流

1. **上传** `/admin/upload`：校验后存为草稿，不立即生效；
2. **发布** `/admin/publish`：把草稿与已生效图合并，逐站核算组合表，
   然后在一个 SQLite 事务里写入新版本并切换生效指针；
3. **停运名单** `/admin/outages`：随时可改。判定立刻按新名单重算，
   但只读取已生效的组合表，不需要重新发布；
4. **总览** `/`、**单站** `/stations/<id>`：查看各站台能否进出及组合。

### 发布一致性保证

* 核算在事务外进行，因此**核算期间仍可修改停运名单**；
* 提交阶段重新读取最新停运名单：若新图**缺少仍在停运的电梯**，发布中止；
* 若核算或保存失败（含草稿在核算期间被改动），**旧组合表 + 当前名单继续供数**；
* 新版本在事务内一次性写入并切换，不会出现“新图已生效、组合表未生效”的中间态。

## 本地开发与测试

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest
pytest -q
flask --app app.wsgi run --debug
```

数据库路径用环境变量 `DATABASE` 指定，默认 `data/nav.db`。
