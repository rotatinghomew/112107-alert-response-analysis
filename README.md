# 居家监测告警的响应时效与设备可用性分析

以下资料为合成记录，不含实际个人信息。文件以 UTF-8 编码，普通字段含日期或本地时间（东八区），分号表示同一字段的多值。空值表示在该记录中未提供，不自动当零。表内的凭据编号用于核对记录归属。

## 数据来源

- `data/bindings.csv`：同一位置的设备换装；绑定记录采用权威本地时间，结束点不含。
- `data/clock_checks.csv`：将 offset_sec 加到来源时间得到本地中心时刻，margin_sec 是两侧界限；区间针对来源时钟。
- `data/calls.csv`：ref 是源端转接凭证，来源为空的 person 必须从绑定确认。
- `data/receipts.csv`：签收包含原始归属引用和时间来源。
- `data/outage_notices.csv`：故障工作单和发布给运营台的停服范围；仅指定通道受影响。
- `data/manual_review.csv`：人工核对回函；有的证据在月结后才到。
- `data/sla.csv`：时间单位秒，抵扣单位分；应纳入且必须上门的类别由合同载明。
- `data/signed_months.csv`：历史已发月单。`cutoff` 是月单当时收录凭据的截至时间，金额为分。
- `data/signed_obligations.csv`：已发月单按原归属记录的义务明细；`case_credit` 是旧台账已计入事件上限、尚未扣减全站月度上限的金额。旧台账归属或金额不一定正确。

## 环境及检查

使用 Python 3.11.2 标准库，无需网络或额外安装。在项目目录可执行 `python3 -m unittest discover -s tests -v` 核对输入文件、表头与记录形状。各来源字段及记录数量见 `data-contract.json`。目前提供资料读取检查，不生成业务分析结果。
