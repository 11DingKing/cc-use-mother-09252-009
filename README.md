# 联合教研议题决议

跨国联合教研会上，同一议题会经历提案、翻译澄清、院校表态、附加条件与条件性同意。
本服务用**版本化提案**与**结构化条件**保存协商过程，并由**法定人数**与**利益冲突**规则决定决议是否生效：

- 提案每轮澄清/修订生成新版本（`seq` 递增），旧版本上未满足的条件自动标记为 `superseded`，旧票不参与新版本计数；
- 机构只能修改自己的立场（投票、撤回、自己提出的条件、自己的利益冲突声明）；
- 声明利益冲突的机构从法定基数中剔除，不能投票或签署；撤回回避后基数恢复并重算；
- 撤回表态时，尚未生效议题立即重算；已生效决议不可撤回，只能发起**修订流程**（原决议保持有效，另开后继议题）；
- 所有写操作幂等（`Idempotency-Key` 头或 `idempotency_key` 字段，另有同态自然幂等），并写入顺序审计日志；
- 截止时间按议题登记的 IANA 时区解释，逾期拒绝投票/附条件/改版；
- SQLite 持久化，重启后状态与审计顺序不变。

## 分层

| 模块 | 职责 |
| --- | --- |
| `models.py` | 不可变领域模型（议题、提案、条件、投票、签署、冲突、修订、审计） |
| `rules.py` | 纯函数规则：时区截止、适格基数、法定人数、生效评估 |
| `clock.py` | 可替换时钟与标识端口（`SystemClock` / `FixedClock`） |
| `storage.py` | SQLite 仓储，`BEGIN IMMEDIATE` 串行化写事务 |
| `service.py` | 应用服务：事务编排、幂等、审计、生效与修订流程 |
| `api.py` | 标准库 HTTP/JSON 接口边界 |

## 生效规则

对议题当前提案版本：

1. 适格成员 = 全体成员 − 已声明利益冲突的机构；
2. 法定人数：有效票数 / 适格成员数 ≥ `quorum_ratio`（默认 2/3）；
3. 适格成员中已投票者必须**全部赞成**（反对或弃权阻止生效，理由在 `evaluation.reasons` 中列出）；
4. 当前版本上没有 `outstanding` 条件；
5. 满足 2–4 后，还需赞成机构中达到 `signature_ratio`（默认 100%）完成签署，议题才变为 `adopted`。

## HTTP 接口

```text
POST   /institutions                              登记机构
POST   /institutions/{code}/delegate              更换代表（历史投票保留原代表姓名）
POST   /issues                                    创建议题（含初版提案、时区截止）
GET    /issues                                    议题列表
GET    /issues/{id}                               议题全貌 + 实时生效评估
POST   /issues/{id}/proposals                     提交新版提案（翻译澄清/修订）
POST   /issues/{id}/conditions                    附加结构化条件
POST   /issues/{id}/conditions/{code}/resolve     提出机构标记条件 fulfilled/waived
POST   /issues/{id}/conflicts                     声明/撤回利益冲突
POST   /issues/{id}/votes                         投票 for/against/abstain（可改票）
DELETE /issues/{id}/votes/{institution}           撤回表态
POST   /issues/{id}/signatures                    赞成机构签署
POST   /issues/{id}/revisions                     已生效决议发起修订（生成后继议题）
GET    /issues/{id}/rationale/{institution}       查询某机构各版本表态理由与所提条件
GET    /issues/{id}/audit                         顺序审计日志
GET    /health
```

错误响应统一为 `{"error": {"code": "...", "message": "..."}}`，状态码：
`422` 校验失败、`403` 越权（非成员/利益冲突/非本人条件）、`404` 不存在、
`409` 状态冲突（重复投票阶段外操作、逾期、已生效需修订）。

## 运行

```bash
# 默认数据库 ~/.resolution_service/resolution.db
RESOLUTION_HOST=127.0.0.1 RESOLUTION_PORT=8080 \
  python3 -m service_09252_009.api

# 自定义数据库路径
RESOLUTION_DB=/data/resolution.db python3 -m service_09252_009.api
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：协商到生效全链路、反对/弃权、条件阻塞与满足、版本化提案（旧条件失效、旧票不计）、
撤回重算、已生效后修订、利益冲突剔除与恢复、幂等（显式键/自然幂等/并发同键）、
代表更换留痕、时区截止边界、SQLite 重启后状态与审计顺序、多线程并发投票，以及 HTTP 接口。

## 编译检查

```bash
python3 -m compileall -q service_09252_009 tests
```
