# 志愿贡献报告封账

本项目维护志愿贡献报告封账的领域约定、角色边界与样例数据，并提供对应的 Python 后端服务，供接口调用和自动化验证统一使用。当前契约覆盖文博中心运营员、志愿者、监护人、场馆负责人，并明确贡献输入摘要、双方签署封账、迟到记录分流、汇总下钻解释等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/report_closing/`：封账后端（纯标准库：SQLite 持久化 + WSGI HTTP 接口）。
- `tools/check_contract.py`：命令行摘要检查。
- `tools/run_server.py`：命令行启动后端服务。
- `tests/`：契约完整性、领域规则与 HTTP 端到端回归测试。

## 后端能力

后端围绕契约的四条不变量实现：

- **贡献输入摘要**：试算报告携带统计口径（纳入状态、核验截止、优秀线、范围）与对纳入事件集合内容敏感的 `input_digest`；输入变则摘要变，输入不变重算摘要不变。
- **双方签署封账**：学校与场馆双方签署（且不得为同一人）后方可封账；封账后报告及汇总不可变，试算阶段重算会作废已有签署。
- **迟到记录分流**：核验时间晚于统计截止的记录不改动已封账报告，只能经更正单（双方确认）或下一版（重开须独立批准人批准）进入账务。
- **汇总下钻解释**：报告保存纳入/排除快照，任何汇总值可下钻到记录级明细与排除原因；导出文件包含分块摘要，重复下载字节一致。

### 状态机

- 服务事件：`草拟 → 待核验 → 已确认`（仅已确认事件进入统计）。
- 报告：`试算 → 已封账 → 已重开`（重开生成下一版试算，旧版只读可查）。
- 更正单：`草拟 → 已确认 → 已归档`。

### 启动服务

```bash
python3 tools/run_server.py --port 8080 --db report_closing.db
# 或：PYTHONPATH=src python3 -m report_closing --port 8080
```

默认使用内存库；`--db` 指定 SQLite 文件即可持久化。

### API 一览

| 方法与路径 | 说明 |
| --- | --- |
| `POST /events` | 登记服务事件（草拟） |
| `POST /events/{id}/submit` | 提交核验（草拟→待核验） |
| `POST /events/{id}/verify` | 核验确认（待核验→已确认，记录核验时间） |
| `GET /events`、`GET /events/{id}` | 事件查询 |
| `POST /reports` | 生成试算报告（含统计口径与输入摘要） |
| `POST /reports/{id}/refresh` | 重算试算报告（作废已有签署） |
| `POST /reports/{id}/sign` | 学校/场馆分别签署 |
| `POST /reports/{id}/close` | 双方签署齐全后封账 |
| `POST /reports/{id}/reopen` | 独立批准人重开，生成下一版试算 |
| `GET /reports/{id}` | 报告详情（口径、摘要、汇总、签署） |
| `GET /reports/{id}/drilldown?theme=&excellent=&included=` | 从汇总值下钻到纳入/排除记录及原因 |
| `GET /reports/{id}/late-records` | 迟到记录及其分流去向（下一版/更正单/未分流） |
| `GET /reports/{id}/export` | 确定性导出（分块摘要，重复下载字节一致） |
| `POST /reports/{id}/corrections` | 针对已封账报告登记更正单 |
| `POST /corrections/{id}/confirm` | 更正单双方确认 |
| `POST /corrections/{id}/archive` | 更正单归档 |
| `GET /reports/{id}/corrections`、`GET /corrections/{id}` | 更正单查询 |

错误响应统一为 `{"error": {"code": ..., "message": ...}}`，业务规则冲突返回 409 并携带规则编码（如 `missing_signatures`、`not_independent_approver`、`scope_locked`）。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
