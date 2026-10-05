# 志愿贡献报告封账

本项目维护志愿贡献报告封账的领域约定、角色边界与样例数据，供后端服务、接口和自动化验证统一使用。当前契约覆盖文博中心运营员、志愿者、监护人、场馆负责人，并明确贡献输入摘要、双方签署封账、迟到记录分流、汇总下钻解释等关键约束。

## 目录

- `domain/contract.json`：领域角色、状态、约束和样例。
- `src/domain_contract/`：契约读取与确定性校验。
- `src/report_closing/`：封账后端（领域模型、业务服务、HTTP API）。
- `tools/check_contract.py`：命令行摘要检查。
- `tests/`：契约完整性回归测试与封账业务规则测试。

## 后端能力

- **事件汇集与核验**：`POST /events` 提交服务事件（待核验），`POST /events/{id}/verify` 核验为已确认或已排除，终态不可改。
- **试算报告**：`POST /reports/trial` 汇集周期内已确认事件，生成带输入摘要（SHA-256）与统计口径说明的试算报告；汇总服务时长、主题覆盖与优秀评价。
- **双方签署封账**：`POST /reports/{id}/sign` 学校与场馆各自签署（绑定签署时的输入摘要），双方签齐后 `POST /reports/{id}/close` 封账，封账后版本不可变。
- **迟到记录分流**：封账后到达的事件通过 `POST /reports/{id}/late-events` 分流——补录进入下一版，或以更正单钉在当前版本。
- **独立批准重开**：`POST /reports/{id}/reopen` 要求批准人独立于双方签署人，重开后方可生成下一版试算。
- **下钻与导出**：`GET /reports/{id}/drilldown` 从汇总值下钻到被纳入/排除的记录及原因；`GET /reports/{id}/export` 输出分块摘要清单，同一版本重复下载结果一致。

启动服务：`PYTHONPATH=src python3 -m report_closing.api`（默认 127.0.0.1:8080）。

## 验证

测试命令：`python3 -m unittest discover -s tests -v`

编译命令：`python3 -m compileall -q src tools tests`

命令行检查：`python3 tools/check_contract.py domain/contract.json`
