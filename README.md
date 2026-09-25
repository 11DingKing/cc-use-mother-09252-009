# 联合教研议题决议

面向跨国联合教研会的纯服务端决议系统。针对“同一议题经历提案、翻译澄清、
院校表态与条件性同意，会议纪要无法准确表达哪些条件尚未满足”的问题，
以**版本化提案 + 结构化条件**保存协商过程，由**法定人数与利益冲突规则**
决定决议是否生效。

## 架构

代码按领域模型、应用服务、持久化与接口边界组织；时间通过可替换时钟端口
接入，测试可注入固定时钟稳定复现状态变化。

```
service_09252_009/
├── models.py    # 领域模型与纯函数 evaluate()：法定人数/多数/条件/截止 规则
├── errors.py    # 领域错误（携带 HTTP 状态码与机器可读 code）
├── storage.py   # SQLite 端口：WAL、BEGIN IMMEDIATE 串行写事务、单调序号
├── service.py   # 应用服务：用例编排、鉴权、幂等、审计、重算
├── api.py       # HTTP JSON API（仅标准库）
└── __main__.py  # 服务入口
```

## 核心规则

- **版本化提案**：议题 `(id, version)`；修订产生新版本，旧版本 `superseded`，
  其立场/条件/签署成为历史，新版本从零表决。
- **机构只能修改自己的立场**：表态、撤回、签署均以
  `X-Institution-Id` + `X-Representative-Id` 头鉴权；代表更换后旧代表
  立即失效，立场随院校保留，由新代表继承。
- **生效规则**（每次状态变化后对尚未生效的议题重算）：
  合格院校（排除利益冲突登记院校）中，有效表态（支持+反对，弃权不计）
  ≥ 法定人数，且支持 > 反对，且所有必备条件已满足，且未过截止时刻。
- **撤回表态**：议题未生效 → 立即重算；决议已生效 → 拒绝静默推翻，
  必须走修订流程（`POST /proposals/{id}/revisions`）。
- **时区截止**：截止时间按 `deadline_timezone`（IANA 名称）解释并折算
  UTC 存储；截止时刻本身仍可操作，过后表决中议题拒绝写操作，
  已生效决议地位保持。
- **幂等与审计**：写操作接受 `Idempotency-Key` 头，重试返回首次结果
  （字节一致）；全部写操作（含幂等回放）记入只增审计日志。
- **持久化**：SQLite WAL；议题编号、版本号、审计序号均为单调计数器，
  重启后保持顺序、不复用。

## 运行

```bash
python3 -m service_09252_009 --db data/resolution.db --port 8080
```

数据库路径也可用环境变量 `RESOLUTION_DB` 指定；运行数据不写入源码目录。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/institutions` | 注册院校 |
| POST | `/institutions/{id}/representatives` | 任命代表 |
| POST | `/institutions/{id}/representatives/replace` | 更换代表 |
| POST | `/proposals` | 提交议题（v1，可带截止时区） |
| POST | `/proposals/{id}/conditions` | 附加条件（required/advisory） |
| POST | `/conditions/{id}/satisfy` `/waive` | 确认满足 / 解除条件 |
| POST | `/proposals/{id}/stances` | 表态（favor/oppose/abstain，可改投） |
| POST | `/proposals/{id}/stances/withdraw` | 撤回表态（未生效议题重算） |
| POST | `/proposals/{id}/conflicts` | 登记利益冲突 |
| POST | `/proposals/{id}/signatures` | 签署生效决议 |
| POST | `/proposals/{id}/revisions` | 对已生效决议发起修订（v2+） |
| GET | `/proposals/{id}` | 状态与评估（含未满足条件清单与理由） |
| GET | `/proposals/{id}/rationale?institution_id=` | 查询院校表态理由 |
| GET | `/proposals/{id}/audit` | 审计日志 |

错误统一为 `{"error": {"code", "message"}}`，状态码由错误类型决定
（401 缺身份头 / 403 越权 / 404 不存在 / 409 状态冲突或已截止 /
422 参数校验失败）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：法定人数与弃权计数、利益冲突排除、必备/参考条件、撤回重算与
修订流程、代表更换、时区截止边界、幂等回放、审计顺序、并发投票
（多线程文件库）、SQLite 重启后顺序保持、HTTP 端到端。

## 编译检查

```bash
python3 -m compileall -q service_09252_009 tests
```
