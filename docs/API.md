# AlonTrip API

跟代码走。改路由先改 `backend/app/main.py`，再改本页。本机交互文档：`http://127.0.0.1:5003/docs`。

前缀 `/api/trip`。错误 `{"detail":"英文原因"}`。对外 id 是 slug（`senso-ji`）。

密钥在仓库根目录 `.env`（见 `.env.example`），由 `backend/app/config.py` 读取。

**`main.py` 不是算法。** 它是前台接待：浏览器来一个地址，它决定叫谁干活、把结果拼成 JSON。路线规划（哪天去哪、住哪）在 `backend/app/services/planner.py` 的 `plan_trip()`。

---

## SerpApi / DeepSeek 谁打

| 外部服务 | 环境变量 | 干什么 |
|----------|----------|--------|
| SerpApi | `SERPAPI_KEY` | 日韩港公交；目录搜不到时搜景点/酒店。金额折成 USD |
| 高德 | `AMAP_KEY` | 大陆公交。空钥匙则大陆只出打车估价 |
| DeepSeek | `DEEPSEEK_API_KEY` | 把景点分到各天。不查车。型号 `deepseek-v4-flash`，请求关闭 thinking（常量在 `grouping.py`，`planner.py` 共用） |

没有 key 也能跑：公交用缓存/估算，分组用规则。浏览器打的是**你们自己的** `/api/trip/...`，不是白嫖 SerpApi。钥匙缺了，进程不要崩（D-010）：没有 SerpApi 就读 `data/transit_cache/` 或直线估算；没有 DeepSeek 就规则分组。没有钥匙时**不会去打** `serpapi.com` 或 `api.deepseek.com`。

| 接口 | SerpApi | DeepSeek |
|------|:-------:|:--------:|
| `GET /health` `/cities` `/pois` `/lodgings` `/transport-hubs` | 否 | 否 |
| `GET /pois/search` `/lodgings/search` | 目录未命中才打 | 否 |
| `POST /optimize-route` | 每段公交（命中 `data/transit_cache/` 则不打） | 有 key 就分组，失败退回规则 |
| `POST /transit-legs` | 未缓存的段 | 否 |

勾选已有景点、系统选酒店：都不打 SerpApi。

---

## 接口一览

**`GET /health`** — 探活。`transit_provider` / `grouper` 是进程选了哪套实现，不是「上次一定打通了」。

**`GET /cities`** — 九城 + `countries`（Japan / China / Korea）。

**`GET /pois?city=`** · **`GET /lodgings?city=`** · **`GET /transport-hubs?city=`** — 目录，不打外部 API。未知 city → 400。

**`GET /pois/search?city=&q=`** · **`GET /lodgings/search`** — `q` ≥ 2 字。`source`：`db` | `local-json` | `serpapi`。

**`POST /optimize-route`** — 规划页 Generate。同 IP 10 秒冷却。

| 字段 | 说明 |
|------|------|
| `cities[]` | 1–4 座，顺序即行程 |
| `days` | ≥ 城市数 |
| `poi_ids[]` | 每城至少一个 |
| `hotel_mode` | `system_one` / `system_multi` / `custom` |
| `arrival_hub_id` / `departure_hub_id` | 首城到达 / 末城离开 |
| `first_day_density` / `last_day_density` | 各 `none`（能空则 0 个点）或 `few`（尽量少，**不是**硬卡 1 个）。四组合都合法，默认 few / none |
| `edge_density` | 旧打包字段；两个开关都没传时才用。含 `first_none_last_none` |

只传旧字段 `city` 仍可用（当成单城 + `system_one`）。

**`POST /transit-legs`** — 结果页微调。最多 30 段。不跑 DeepSeek。

---

## `main.py` 逐段（当接线员读）

路径：`backend/app/main.py`。行号随文件改动会变，按**名字**找，不要死记行号。

### 这个文件干什么、不干什么

干：认城市、挡非法请求、10 秒冷却、把目录装进内存、调用 `plan_trip`、对相邻两点查车、把一天拼成 JSON。

不干：不决定「浅草和涩谷哪天去」（那是 `planner.py`）；不把坐标变成地铁线路名（那是 `transit.py`）；不搜新店名（那是 `search.py`）。

本地启动（在 `backend/` 目录）：

```bash
uvicorn app.main:app --reload --port 5003
python -m app.main          # 读 APP_PORT，默认 5003
```

文件末尾 `if __name__ == "__main__"`：只绑 `127.0.0.1`。生产由 Nginx 反代过来，端口不暴露公网。

### 开头常量

| 名字 | 干什么 |
|------|--------|
| `CITIES` | 九座城的身份证：`id`（`tokyo`）、中文名、英文名、`country`（japan/china/korea）。前端城市列表的权威来源之一。 |
| `CITY_IDS` | 上面九个 id 的集合。请求里出现巴黎 → 400 `unknown city`。 |
| `COUNTRY_NAMES` | `japan` → `Japan` 等。给前端分组标签，用英文。 |
| `CITY_TIMEZONES` | 每座城用哪个时区查车。避免用上海服务器的钟去问东京地铁。 |
| `INTRA_CITY_HOUR = 9` | 城内段：当地上午 9 点出发。 |
| `INTERCITY_HOUR = 16` | 换城段：当地下午 4 点出发（上午留给游览）。 |

产品还没收集真实出发日。查车用「从明天起算第 N 天」的日期，只为了让 SerpApi 拿到一个未来时刻；缓存键只按小时桶（`09` / `16`），不含具体日历日。

### 小工具（下划线开头 = 只给本文件用）

**`_depart_at(时区, 第几天, 几点)`**  
把「第几天 + 几点 + 时区」变成 Unix 时间戳，塞给 SerpApi 的 `depart_at`。

**`_timezone_for_lng(经度)`**  
结果页微调只给了经纬度、没有城市名。经度 &lt; 124 → 中国/香港（UTC+8）；否则日本/韩国（UTC+9）。

**`_query_leg(...)`**  
查**一段**路。`optimize-route` 和 `transit-legs` 共用。城内用 9 点、换城用 16 点，再问公交层（有 key 走 SerpApi + 缓存；直线 &lt; 800m 当步行）。失败不整单崩：打一行 warning，改走 `estimate_route`（直线距离装成地铁：约 20 km/h、每公里 30 日元、最低 180）。页面标 `estimated=true`。诚实「没查到公交 + 打车估价」是方案 T-020，代码还没改。

**`_check_cooldown`**  
同一 IP 10 秒内不能连点两次 Generate。`_last_optimize_call` 存在**进程内存**里，重启清空。微调接口不限，靠缓存省额度。

**`_require_known_city` / `_require_query`**  
城市必须在九座里；搜索词去掉空格后至少 2 个字。

### 开机 `lifespan`

进程起来时先调用工厂：景点、住宿、公交、分组各选一套实现，并打日志。终端里能看到 `Transit provider: serpapi` 或 `local-json`。选完缓存在进程里（`lru_cache`），不是每次请求再选。

然后 `FastAPI(...)` + CORS：默认允许本机 Vite `5173` / preview `4173`，可用环境变量 `CORS_ORIGINS` 改。

### 中间的 `class xxx(BaseModel)` = 快递箱规格

Pydantic 模型：前端 JSON 必须长这样，后端才能拆。字段约束故意放宽，好让拒绝走 `PlanningError` 的固定英文 `detail`，而不是 FastAPI 默认的 422。

| 类 | 谁用 |
|----|------|
| `CustomStayBody` | custom 住法：哪家酒店、占用哪几天 |
| `OptimizeRequest` | Generate 的整箱。`city`（单数）是旧字段：只传它时当成 `cities=[city]` + `hotel_mode=system_one` |
| `Node` / `Leg` | 返回给前端的点和段 |
| `LegNode` / `LegPairRequest` / `TransitLegsRequest` | 微调：最多 30 对；`kind` 只能是 `poi` / `lodging` / `hub`；坐标缺了走我们自己的 400，不是 422 |

### 每个 `@app.get` / `@app.post` 是一个接口

装饰器里的路径就是浏览器要打的地址（已含前缀 `/api/trip`）。

**`GET /health`**  
还活着吗？返回当前选了哪套：`poi_provider`、`lodging_provider`、`transit_provider`、`grouper`。有 DeepSeek key 时 `grouper` 开机可能显示 deepseek，**不等于**上一次 Generate 真打通了。真用没用看 `optimize-route` 返回的 `grouper`。

**`GET /cities`**  
九城 + 每城目录景点数量 + `countries`。不打外部 API。

**`GET /pois` · `/lodgings` · `/transport-hubs`**  
按 `?city=` 拿出清单。未知城 400。不搜网、不算行程。勾目录里的点不花 SerpApi 额度。

**`GET /pois/search` · `/lodgings/search`**  
`?city=&q=`。先目录（MySQL 或 JSON），命中则 `source` 为 `db` / `local-json`。未命中且有 `SERPAPI_KEY` 才打 SerpApi `google_maps`（这是 SerpApi 的引擎名，不是 Google 官方 API）。没命中又没钥匙：200 + 空列表。搜到且连着库：写入 `source=search`，下次走目录。

**`POST /optimize-route`（Generate）**

1. `_check_cooldown`。
2. `_resolve_cities_and_hotel_mode`：认城市列表和住法。
3. 把这些城的景点、青旅、车站从目录装进内存。
4. 调用 **`plan_trip(...)`**（`planner.py`）：排「哪天去哪、住哪」。有 `DEEPSEEK_API_KEY` 就按城填表，失败只这座城退回规则。校验失败 → 400，`detail` 为冻结英文句。
5. `_build_day_chain`：每天一条链。默认「早上酒店 → 景点… → 当晚酒店」。第 1 天若选了到达枢纽且当天有景点：`枢纽 → 酒店(checkin) → 景点… → 当晚酒店`（T-024）；没有景点则枢纽→酒店。最后一天若选了离开枢纽，终点换成车站/机场。
6. 相邻两点 `_query_leg`（SerpApi / 缓存 / 步行 / 估算）。只查相邻对，不是景点两两全矩阵。城际也是其中一段；日本国内 Google 若把新干线算进公交，会出现在结果里，没有另接 JR 接口。
7. `build_day_schedule`（`schedule.py`）：按链和查车结果累加钟点。09:00 起；城际若还在上午则跳到 16:00；午饭 60 分钟、晚饭 90 分钟；第一天入住约 20 分钟。停留用 `suggested_duration_min`。这是展示层，不改 `plan_trip` 的分点。
8. 离开日若终点是枢纽，当晚酒店按「早上那家」报，因为那天并不真入住链尾那家。
9. 返回 `itinerary`（每天含 `schedule`）+ `totals` + `first_day_density` / `last_day_density`。`grouper`：`deepseek` / `rule-based` / `mixed`。`all_real_data`：是否每一段都不是估算。

**`POST /transit-legs`（结果页微调）**

只重查变化了的相邻对。同一套 `_query_leg`。不跑 `plan_trip`、不打 DeepSeek、没有 10 秒冷却。`pairs` 空或超过 30 → 400。

---

## 路线规划算法在哪

商量算法用文字版 [算法.md](算法.md)（跟 `planner.py`），不要对着源码抠。

| 文件 | 干什么 |
|------|--------|
| **`backend/app/services/planner.py`** | **主算法。** `plan_trip()`：天数切给各城、每城分点、选酒店、拼每天节点。有钥匙时 `_deepseek_fill_city()` 按城打 DeepSeek。 |
| `backend/app/services/grouping.py` | DeepSeek 失败时的规则兜底（地理聚类 + 每天点数尽量均匀 + 最近邻排序）。`DEEPSEEK_MODEL` 写在这里。 |
| `backend/app/services/schedule.py` | 结果页钟点日程（午饭/晚饭/入住）。不改分点。 |
| `backend/app/services/transit.py` | 不排日程。两点怎么坐车（SerpApi / 缓存 / 步行 / 直线估算）。 |
| `backend/app/main.py` | 接线员。见上一节。 |

`GET /health` 里的 `grouper` 来自 `factory.py` 的 `LLMGrouper`；**真正填行程表**走的是 `planner.py`，不是开机那套 `get_grouper().group()`。

游玩时长 `suggested_duration_min` 已核对进目录 JSON，用于结果页时间轴；**还没有**用来限制「这一天还塞不塞得下」。营业时间仍未进算法（T-019）。

---

## 改接口时动这些文件

| 层 | 文件 |
|----|------|
| 路由 / 接线 | `backend/app/main.py` |
| 读环境变量 | `backend/app/config.py` |
| 有无 key 选实现 | `backend/app/services/factory.py` |
| 行程 / DeepSeek | `backend/app/services/planner.py`、`grouping.py` |
| 钟点日程 | `backend/app/services/schedule.py` |
| SerpApi 公交 | `backend/app/services/transit.py` |
| SerpApi 搜索 | `backend/app/services/search.py` |
