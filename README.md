# 独立源段目录 + 多语言交付仓

Python 3.11，SQLite。**两侧物理分离**：源段目录与交付仓各自持有一个 SQLite
数据库；交付侧通过只读连接（`mode=ro`）真实读取源目录**已发布的源产品**，
不把位置编号当身份，也不缓存源状态。文本全部为合成内容，不做自动翻译或语义评估。

## 快速运行

```bash
# 端到端跑完 materials/753 全部场景（含用只读客户端读新旧包）
python3 run_demo.py

# 仅验证真实包文件
python3 materials/753/package_reader.py data/packages/PACK1.json
python3 materials/753/package_reader.py data/packages/PACK2.json

# 测试（标准库 unittest，无需 pytest）
python3 -m unittest discover -s tests -v

# 离线交付端到端（从实际冻结包导出，经独立设备进程安装并读回）
python3 -m delivery_system.cli demo-offline

# 手工：把某个冻结包导出为离线目录，再用设备 worker 安装/读取
python3 -m delivery_system.cli export-offline data/packages/PACK2.json out/bundle \
    --dictionaries dicts.json
python3 -m delivery_system.offline_worker install --device out/device \
    --bundle out/bundle --capacity 100000 --traffic 100000
python3 -m delivery_system.offline_worker read --device out/device
```

## 目录结构

```
delivery_system/
  models.py              不可变值对象：SourceSegment/SourceProduct/SourceRef/
                         TranslationEntry/CoverChunk/CoverDiagnosis/CoverResult
  source_catalog.py      源段目录（独立 SQLite + 发布快照 + release 历史）
                         + PublishedSourceReader（交付侧只读句柄）
  coverage.py            条目评估 + 精确覆盖区间 DP + 失败诊断
  delivery_repository.py 译文条目、覆盖计划、不可变发布包（独立 SQLite）
  package_io.py          translation-package/1 写入与结构/覆盖校验
  offline_codec.py       短语字典、字面/字典两类指令、最短正文编码与精确解码
  offline_bundle.py      从冻结包导出离线版本（manifest + 可独立加载的对象文件）
  offline_planner.py     设备端选表示：字典共享计费、容量/保护、流量上限
  offline_device.py      设备缓存、原子激活 active.json、断电与坏块语义
  offline_worker.py      独立进程的设备端 CLI（plan/install/read/space/clear-cache）
  offline_demo.py        离线端到端复现（经 worker 子进程真实安装并读回）
  cli.py / __main__.py   命令行
  demo.py                materials/753 端到端复现（调用官方 package_reader.py）
tests/test_delivery.py   20 个测试：覆盖、过期、重组、不可变、规模
tests/test_offline.py    22 个测试：编码、身份、共享计费、容量、断电、坏块、跨进程
```

## 两侧的实质逻辑

### 源段目录（写入侧 + 只读快照）
- `segment_id` 是身份；`position` 只是当前序位，重排只改位置不改身份/修订。
- 每段有独立 `revision`。`revise_segment` 只增修订、不改旧修订号——不存在
  “把旧译文贴上 2 号修订”这一动作的入口。
- 每次结构性变更（建/增/删/改/移）都写一条 `source_releases` 快照，
  `latest_release_id` / `product_at_release` 可回溯历史源状态。
- `PublishedSourceReader` 用 `file:...?mode=ro` 打开，每次调用重新读取，
  交付侧无法写源库；它读到的是**当前已发布**的源产品，因此源更正立即生效。

### 交付仓（选择侧 + 产品侧）
- 译文条目精确存储 `(segment_id, revision)`；`add_translation` 对同 id
  不同内容直接拒绝（幂等重放相同内容除外），无法改标签冒充补译。
- `assess` 把每条当前语言条目判为：
  `current_usable` / `stale_revision` / `non_contiguous` / `unknown_segment` /
  `malformed` / `empty_text` / `other_language`。**只有 `current_usable` 可入选**。
  过期/不连续/重复引用/空文本一律留作历史，不是当前候选。
- `plan_cover` 在**区间**上做可达性动态规划：起点 0 能否沿合法连续区间一步步到达 n。
  这正是不被“并集覆盖”忽悠的关键——重叠条目可能覆盖并集却无法按序不重不漏拼装。
  任意合法完整覆盖即可（不要求唯一/最少条目），按 id 字典序确定性地选一条。
- 生成时强制不变量：选中 `source_ids` 拼接必须精确等于源序；任一块文本非空；
  整体渲染文本非空。**无法交付时绝不产出空文本成功包。**

### 失败诊断（可交付性依据）
- `no_current_cover`：某些段没有任何当前可用译文，给出具体 `uncovered_segment_ids`。
- `no_exact_cover`：并集覆盖全部段，但有序精确覆盖不存在；给出
  `blocked_boundary`（装配卡住的位置）与 `crossing_translation_ids`（跨该边界、
  又不能拆开的条目），以及被排除条目及原因。FR 的 F12/F23/F4 即此例。
- `empty_source`：空源拒绝发布空包。

### 发布包（translation-package/1）
固定 `package_id/document_id/language/source_snapshot(segment_id+revision)` 及按输出顺序的
`chunks(translation_id/source_ids/text)`，另加 `source_revisions/rendered_text/
content_sha256/created_at/source_release`。包一旦写入即不可变：同 id 不同内容
抛 `PackageExistsImmutable`；旧包的原文快照与译文 id 永不改写（PACK1 保持 rev1）。

## materials/753 复现结果

| 步骤 | 结果 |
|---|---|
| EN 初始发布 PACK1 | 合法覆盖 `T1+T23+T4`（或 `T12+T3+T4`，二者皆合法） |
| FR 尝试发布 | 拒收：`no_exact_cover`，并集含四段但 F12/F23 在 S2 重叠且不可拆 |
| 更正 S2→rev2 | T12/T23/F12/F23 变 `stale_revision`；EN 缺 S2 (`no_current_cover`) |
| 补 T2_NEW(rev2) | EN 重建 `T1+T2_NEW+T3+T4`，发布 PACK2（S2@2） |
| 读旧包 PACK1 | 仍为 S1@1..S4@1 与旧译文 id/文本 |

`package_reader.py` 被以子进程方式真实读取新旧两个包并校验输出。

## 离线阅读器交付（窄带设备）

出版端与设备端**物理分离**：`export-offline` 从**实际冻结包**的 `chunks` 生成离线
目录；设备则由另一个进程（`offline_worker`）只凭自己的缓存与下载清单安装、读取。
原包、`package_reader.py`、以及不涉及设备的发布/覆盖流程保持不变。

### 短语字典与两类指令（`offline_codec.py`）
- 字典：1–127 个非空、不重复的 UTF-8 字节串，按序；正文每项 1 长度字节 + 实际字节，
  单项 ≤127 字节。至多 4 本字典。
- 字面指令：`0x01..0x7F` 长度字节 + 1–127 原始字节。字典指令：`0x80..0xFE`
  一个字节代表第 `op-0x80` 项。字面头高位置 0、字典头高位置 1，两类永不混淆；
  `0x00`/`0xFF` 非法。
- 对每个块分别求“纯字面”和“每种字典混合”的最短正文，并列可任选；解码保证还原
  **精确 UTF-8 字节**。对象身份 = SHA-256(依赖字典身份 + 正文字节)，因此不同字典下
  即使指令字节相同也**不是**同一对象。

### 设备端选择与预算（`offline_planner.py` / `offline_device.py`）
- 枚举可用字典子集；字典对象**只计一次**（被多块共享），不按块重复计费——按块计费会选错。
- 完全相同的对象全机只存一次；本机已有且校验正确的对象复用、不计流量（含依赖身份）。
- 先满足**容量**：新对象 ∪ 旧版本仍受保护的对象须放得下；未受保护旧缓存可删。
  在可共存的方案里选下载字节最少者，最后核对本次**流量上限**。
- 激活在 `active.json` 上原子替换：所有对象先落盘校验，任一坏块则在切换前中止，
  旧本照常可读；断电于切换前同样保持旧本，已验对象可续用。

### 规格示例（单块 `abcdef`，字典 `abcd,abc,def`，大小 13）
| 设备 | 结果 |
|---|---|
| 已装字典、空间充足 | 字典体 2 字节（`81 82`），流量 2 |
| 空设备、流量上限 7 | 选纯字面 7 字节 |
| 已装字典、原文 `abcdeg`、上限 2 | 拒收（无两字典指令拼法；字典体实为 4，字面 7） |

### 原子更新放不下（`demo-offline` 场景）
真实发布 `OLD1..OLD4 → PACK_OLD`、改修订后 `NEW1..NEW4 → PACK_NEW`；四块共享短语
`COMMONPHR`，一本 10 字节字典被四块共享，故“共享一次”方案 46 字节 < 纯字面 68 字节，
而“每块各算一次字典”会误选字面。旧本占用 46 字节。容量取 46（=目标书自身）时：
目标书单独放得下，但旧本受保护 + 新书 = 92 > 46，**原子更新放不下而拒收，旧本继续可读**；
换到新设备或扩容设备后安装成功，读取端恢复完整新译文（`demo-offline` 全 45 项校验通过）。

## 规模与不扩平台

测试覆盖 100 源段 / 5 语言 / ≥500 译文（含大量重叠诱饵），验证每语言精确覆盖、
诱饵从不入选、更正后按缺口隔离、补译后恢复。覆盖是 O(n·k) 区间 DP，
足以支撑题目规模；未引入翻译、语义评分、账号等平台能力。
