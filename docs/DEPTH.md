# 涨潮断面通行簿深度登记

- 中型。用精确阈值交点及航行偏移求全程潮窗。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 潮尺读数采信 | 查重复刊头和隔离行 | `/api/stage/readings` | `app/modules/intake/readings.py` `app/modules/intake/service.py` | `tests/test_readings.py` |
| M2 站点基准统一 | 复核孪站裁决 | `/api/stage/datum` | `app/modules/datum/resolve.py` | `tests/test_datum.py` |
| M3 水位阈值交点 | 查看来源点和交点 | `/api/stage/crossings` | `app/modules/crossings/interpolate.py` | `tests/test_crossings.py` |
| M4 全程可行潮窗 | 查断面偏移后的区间 | `/api/stage/windows` | `app/modules/passage/service.py` `app/modules/passage/state.py` | `tests/test_windows.py` |
| M5 船期覆盖勾稽 | 回查申请时点和时长差额 | `/api/stage/coverage` | `app/modules/schedule/service.py` | `tests/test_coverage.py` |
| M6 批次留档复跑 | 同源复算及归档查阅 | `/api/journal` | `app/modules/journal/service.py` `app/modules/journal/store.py` | `tests/test_journal.py` |
| M7 潮窗证据签发 | 验证原件及报告摘要 | `/api/export/<id>` | `app/modules/proof/service.py` | `tests/test_proof.py` |
| M8 值班盘移库 | 验副本、腾位、原号回查、损坏停签 | `/api/journal/<id>/migrate` `/api/journal/rotate` | `app/modules/journal/store.py` `app/modules/journal/transfer.py` | `tests/test_migration.py` |

| TRAP | 现象 | 朴素处理后果 | 处理 | 位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 潮尺 BOM | 站号列错认 | 消除开头 BOM | `app/modules/intake/readings.py` | `tests/test_readings.py::test_bom` |
| TRAP-04 | 分段重复表头 | 表头当读数 | 记录分段并跳过 | `app/modules/intake/readings.py` | `tests/test_readings.py::test_repeated_header` |
| TRAP-05 | 多余列 | 原件错位 | 隔离留原列数组 | `app/modules/intake/readings.py` | `tests/test_readings.py::test_wrong_width` |
| TRAP-07 | 潮尺 N/A | 以零插值 | 隔离并保留原值 | `app/modules/intake/service.py` | `tests/test_readings.py::test_sentinel` |
| TRAP-10 | 三分之一秒交点 | 临界比较漂移 | 有理数插值 | `app/modules/crossings/interpolate.py` | `tests/test_crossings.py::test_exact_fraction` |
| TRAP-11 | Z 与东八区同刻 | 重复时间点 | 统一 UTC 秒 | `app/clock.py` | `tests/test_datum.py::test_timezone_repeat` |
| TRAP-14 | 同时潮尺矛盾 | 后报覆盖 | 同键冲突全部隔离 | `app/modules/datum/resolve.py` | `tests/test_datum.py::test_conflicting_station` |
| TRAP-15 | 厘米、米混用 | 水深错档 | 按单位换算后加基准 | `app/modules/datum/resolve.py` | `tests/test_datum.py::test_normalize_unit` |
| TRAP-19 | 孪站同刻 | 顺序决定选站 | 显式优先级 | `app/modules/datum/resolve.py` | `tests/test_datum.py::test_station_priority` |
| TRAP-23 | 船期右端 | 重复归属 | 排除右端 | `app/modules/passage/service.py` | `tests/test_windows.py::test_half_open_end` |

海棠17窗口 3000 秒 = 1500 可行 + 900 不可行 + 600 缺证。潮尺接收 20 条数据，18 条格式采信，11 条进入断面判定，2 条格式隔离、7 条业务隔离。重复表头及空行单计。以分钟级实测样例演示，不覆盖未带偏移的当地时刻、曲线外推和实时设备连接。

## 值班盘移库

- 值班盘只留一本时默认“复算热度优先”：有复算记录的旧簿优先于未复算的新归档，多本复算簿按 `last_rerun_at` 排序；都未复算时再按最近使用/归档时刻和单调序号裁决。因此题设中最近复算的旧簿保留，刚归档的新簿验副本后移库。
- `POST /api/journal/<id>/migrate` 先原子写副本，再从磁盘回读，核对原件 SHA-256、`result_sha256`，并从原件复算结论；全部通过后才删除值班盘完整原簿。
- 写副本或核验失败时状态置 `failed`，原簿不删，接口返回 `retryable:true`；同一路由可重试并覆盖未验副本。
- 移库与复算按原号串行化关键区；SQLite 行和原子替换的 JSON 副本均为完整快照，因此复算要么读到值班盘原簿，要么读到已验副本。
- 移库后仍以原 `run_id` 调 `GET /api/journal/<id>`；副本损坏时返回 409 `REPLICA_DAMAGED`，`GET /api/export/<id>` 不签发并返回 `INVALID_PROOF`。
