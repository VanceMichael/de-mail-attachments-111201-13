# 合作邮件附件·隔离采集

面向合作资料接收人员的 Python 采集组件。读取本地合成 MIME 邮件，自动解包附件与
嵌套压缩包，按供应方业务键与有效载荷摘要结构化入库，并保留从邮件到每一行数据的
完整来源链。输入样本全部由 `fixtures/messages.json` 在本地构造，不连接真实邮箱，
example.invalid 域名只表示来源身份。

## 安装与运行

使用 Python 3.11，依赖仅为标准库，SQLite 随解释器提供，无需安装第三方包。
从项目根目录执行。

```bash
python3 -m compileall -q collector
python3 -m unittest discover -s tests -t .
python3 -m collector mailbox --messages fixtures/messages.json --db var/mail.sqlite3
```

`mailbox` 输出每封邮件的处理结论；重复运行同一归档即“重放”，已入库的行、
来源链、拒绝与隔离记录都不会翻倍。

## 处理报告状态

每封邮件给出且仅给出一个结论，可据此判断是整封不能读、某个附件被拒，还是数据已收下：

| 状态 | 含义 |
| --- | --- |
| `complete` | 有效数据已收下，且随附可信清单（manifest）的文件、行数、摘要全部满足 |
| `partial_delivery` | 有数据收下，但清单未全部满足，或邮件未附清单无法核验完整性 |
| `rejected_message` | 整封邮件不能读（畸形 MIME、不支持的传输编码、分段不全），原件进隔离区 |
| `rejected_attachment` | 邮件可读，但某个附件/压缩成员被安全策略拒绝，无有效数据入库 |
| `no_data` | 邮件可读，但没有可识别的 CSV 数据附件 |

每条拒绝都带 `scope`（message/archive/member/manifest）与可定位的 `location`
（邮件 → 部件序号 → 压缩成员路径），并在 `rejections`、`quarantine` 表留痕。

## 常用命令

```bash
# 处理合成邮件归档并打印报告
python3 -m collector mailbox --messages fixtures/messages.json --db var/mail.sqlite3

# 查看最近一次运行的交付判定与拒绝明细
python3 -m collector report --db var/mail.sqlite3

# 回溯某个业务行的修订历史与来源链（消息→部件→成员→行号）
python3 -m collector lineage --row-key '供应方:example.invalid::id=20' --db var/mail.sqlite3

# 既有 JSON 采集与回环演示（保持兼容）
python3 -m collector import --input fixtures/sample.json --db var/records.sqlite3
python3 -m collector show --db var/records.sqlite3
python3 -m collector demo
python3 -m collector serve --db var/records.sqlite3 --port 8080
```

## 身份、判重与更正

- 消息身份以原始字节的 SHA-256 摘要为准，不依赖文件名或 Message-ID；同一封邮件
  被转发多次、文件名被改动，都识别为同一证据。
- 数据行按“供应方业务键（vendor + 行内主键列）+ 有效载荷规范化摘要”判重，
  转发件中的重复内容只登记新的来源，不产生新数据行。
- 带 `Supersedes` 头的更正件会把同业务键的旧版本标记为 `superseded`，新版本成为
  `current`；旧证据与其来源链原样保留，不被覆盖。

## 解包安全边界

所有解包只在内存中进行，不向文件系统写出任何成员，也不执行附件。硬性上限由
`fixtures/messages.json` 的 `limits` 给出：嵌套层数、成员数量、累计展开字节、
单文件字节。越界路径（`..`、绝对路径）、符号链接/设备节点、加密压缩包一律拒绝，
原因定位到具体成员。

## 组件与数据

| 模块 | 职责 |
| --- | --- |
| `collector/synth.py` | 把 fixtures 描述构造成确定、可重放的真实 MIME 字节（含分段、嵌套 ZIP、加密标志） |
| `collector/mailparse.py` | MIME 解析、`message/partial` 重组、嵌套转发递归、坏部件识别 |
| `collector/extract.py` | 内存安全解包与层数/成员/字节预算控制 |
| `collector/records.py` | CSV 结构化、业务键、载荷摘要、可信清单核验 |
| `collector/evidence.py` | SQLite 证据库：消息、部件、成员、数据行、来源链、替代关系、拒绝、隔离 |
| `collector/pipeline.py` | 串联各阶段并产出处理报告 |

数据库表：`messages`、`parts`、`members` 构成来源链骨架；`observations` 保留
数据行的当前/被替代版本；`observation_sources` 关联行与具体成员行号；`rejections`、
`quarantine` 保留拒绝与坏数据；`deliveries`、`runs` 记录每次运行的交付判定。

## 可复现运行

宿主机或容器均不需要网络。仓库内不含真实账号或人员资料；不要将生产凭据写入样本
或日志。需要长期保留证据时，请备份整个数据库。
