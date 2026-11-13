# 银行流水对账 API

财务对账服务：核对银行到账流水与收款系统明细，按整批最大配对数求解，
只把**所有最优方案共有的配对**标为确定，其余行列出可参与最优方案的候选配对，
并单列必然未匹配行——同额款项不会被随意配对。支持人工锁定/撤销配对，
两个 API 进程共用同一 SQLite 库，乐观修订号保证并发修改安全。

## 快速开始

```bash
docker compose up --build
```

启动后：

| 入口 | 地址 |
|---|---|
| 网关（轮询分发到两个 API 进程） | http://localhost:8000/docs |
| API 进程 1 | http://localhost:8001/docs |
|  API 进程 2 | http://localhost:8002/docs |

`/docs` 为 Swagger UI 操作入口，可直接上传 CSV、锁定/撤销配对。

运行内置样例（同额歧义 + 并发锁定）：

```bash
python3 samples/demo.py            # 仅依赖 Python 标准库
```

## 对账语义

* **上传**：两份 CSV（各 ≤100 行），表头 `id,date,currency,amount_cents`；
  id 在本侧文件内唯一，金额为正整数分，日期 `YYYY-MM-DD`，币种 3 位字母。
* **配对规则**：币种相同、金额相同、入账日期相差 ≤ 2 天；每行最多配一次。
* **求解口径**：整批最大配对数（Hopcroft–Karp）。
* **分析口径**（基于 Dulmage–Mendelsohn 交错路刻画，与暴力法交叉验证）：
  * `confirmed_pairs`：人工锁定 + 所有最优方案共有的配对（确定）；
  * `possible_pairs_by_*`：其余每行可参与某个最优方案的候选配对；
  * `necessarily_unmatched_*_rows`：任何最优方案都匹配不到的行；
  * 不任选任何单一方案。
* **人工锁定**：锁定的配对须仍能达到原最大配对数，否则 `422` 拒绝并保留原状；
  撤销锁定无此限制。锁定/撤销后分析结果随修订号一并更新。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/batches` | 上传 `bank_file`、`system_file` 两份 CSV，创建批次并返回首次分析 |
| `GET` | `/batches` | 列出全部批次 |
| `GET` | `/batches/{id}` | 查询批次当前一致状态（修订号 + 锁定 + 分析结果） |
| `POST` | `/batches/{id}/locks` | 锁定一对配对，Body：`{left_id, right_id, expected_revision}` |
| `POST` | `/batches/{id}/unlocks` | 撤销锁定，Body：`{left_id, expected_revision}` |

错误码：`404` 批次/锁定不存在；`409` 修订号过期（响应含 `current_revision`）；
`422` CSV 非法或锁定不可行（`not_a_candidate_pair` / `already_locked` / `lock_infeasible`）。

### curl 示例

```bash
# 创建批次（响应含 revision，后续修改须携带）
curl -s -F bank_file=@samples/01_amount_ambiguity/bank.csv \
        -F system_file=@samples/01_amount_ambiguity/system.csv \
        http://localhost:8000/batches

# 锁定（expected_revision 须为所见修订号）
curl -s -X POST http://localhost:8000/batches/<batch_id>/locks \
     -H 'Content-Type: application/json' \
     -d '{"left_id":"B001","right_id":"S001","expected_revision":1}'

# 撤销锁定
curl -s -X POST http://localhost:8000/batches/<batch_id>/unlocks \
     -H 'Content-Type: application/json' \
     -d '{"left_id":"B001","expected_revision":2}'
```

## 并发与一致性设计

* **两进程共库**：`api1`/`api2` 两个容器挂载同一命名卷，共用 `/data/recon.db`
  （WAL 模式 + `busy_timeout`，读写互不阻塞，写者串行）。
* **计算不持写锁**：匹配分析在数据库事务外基于一致性快照进行；
  写事务仅做条件更新与插入，毫秒级完成。
* **乐观修订**：每次修改携带 `expected_revision`，写事务内执行
  `UPDATE ... WHERE revision = ?`，行数为 0 即拒绝（`409`），状态不变。
* **原子生效**：锁定记录、修订号、分析结果在同一事务提交，查询只读
  单行快照（`state_json`），不会混用不同版本的状态。
* **重启保留**：数据库文件落在命名卷上，容器/进程重启后批次、锁定、
  修订号与分析结果全部保留。

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

测试覆盖：匹配引擎与暴力法交叉验证（3×3 穷举 + 数千随机图）、
锁定可行性等价判定、CSV 校验、API 全流程、多线程并发锁定单胜者、
两实例共库、重启持久化。

## 目录结构

```
app/
  matching.py   # 最大匹配 + 最优方案集合分析（SCC/交错路刻画）
  analysis.py   # 配对规则、锁定约束下的批次分析
  csvio.py      # CSV 解析与校验
  db.py         # SQLite（WAL）连接、事务、schema
  main.py       # FastAPI 路由
  schemas.py    # 请求/响应模型
tests/          # pytest 测试
samples/        # 同额歧义与并发锁定样例 + 演示脚本
Dockerfile / docker-compose.yml / nginx.conf
```
