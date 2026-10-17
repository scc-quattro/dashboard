# 数据接入

配置文件和项目报告均为 UTF-8 JSON。修改报告后，后端缓存最多保留 10 秒，前端按 `refresh_seconds` 刷新；手动刷新也受缓存影响。Trace 的配置变化需要重启服务。

## 看板配置

| 字段 | 说明 |
| --- | --- |
| `schema_version` | 固定为 `1` |
| `demo` | `true` 时显示演示数据横幅；替换完示例后再设为 `false` |
| `title` / `subtitle` | 页面标题和副标题 |
| `timezone` | IANA 时区，例如 `Asia/Shanghai` 或 `America/New_York` |
| `refresh_seconds` | 5–3600 的整数，默认 60 |
| `phase_labels` | 1–8 个阶段名称 |
| `projects` | 1–100 个项目，每项包含唯一 `id`、`name`、`kind`、`cluster`、`description`、`path` 和可选 `report` |
| `traces` | 默认关闭；同时需要 `enabled: true` 和启动参数 `--enable-traces` |

项目 `id` 使用字母、数字、下划线或连字符，最长 64 字符。`path` 相对配置目录，可使用绝对路径或 `~`；`report` 默认 `dashboard.json`，必须位于项目目录内。前端不会得到本机绝对目录。

## 最小项目报告

```json
{
  "stage": "环境准备",
  "tone": "neutral",
  "summary": "正在准备第一次完整基线。",
  "baseline_verified": false,
  "phase_states": ["done", "active", "pending", "pending"],
  "primary": {"text": "待验证", "unit": "", "label": "完整基线"},
  "secondary": "",
  "correctness": "待验证",
  "sources": [],
  "next_steps": ["保存一次完整运行及验收记录。"],
  "limitations": [],
  "jobs": []
}
```

`phase_states` 必须与配置的阶段数量一致，只接受 `done`、`active`、`pending`。
`tone` 接受 `neutral`、`success`、`warning`。数值指标用 `primary.value`、`unit`、`label` 和 `digits`（0–6）代替 `text`。

## 绑定证据与性能图

在项目目录保存例如 `evidence.json`，并计算摘要：

```bash
python3 -c "import hashlib; from pathlib import Path; print(hashlib.sha256(Path('evidence.json').read_bytes()).hexdigest())"
```

在报告中加入：

```json
{
  "baseline_verified": true,
  "sources": [{"path": "evidence.json", "label": "完整验收记录", "sha256": "替换为上一步生成的64位摘要"}],
  "chart": {
    "title": "固定工作量耗时",
    "unit": "s",
    "direction": "lower",
    "digits": 2,
    "insight": "同一配置下的独立复测",
    "note": "说明工作量、资源与计时边界。",
    "labels": ["样例 A", "样例 B"],
    "series": [
      {"name": "基线", "values": [12.0, 18.0]},
      {"name": "候选", "values": [10.5, 16.2]}
    ]
  }
}
```

以上片段需合并到完整报告。`direction: higher` 表示越高越好；其他值使用越低越好。每系列数值数量必须等于 labels 数量，只接受非负有限数字。图表最多 100 个比较项、8 个系列。

只有报告声明 `baseline_verified: true`、存在至少一个证据来源且所有来源 SHA256 匹配时，才展示验收图表与上界。文件缺失、摘要不符或报告无效会显示待核验，并隐藏该项目的性能数值。未声明验收的报告仍可展示阶段及其标注的主指标。

报告 JSON 最大 2 MB；每个证据文件最大 10 MB。报告或证据通过 `..` 或符号链接逃出项目目录会被拒绝。不要把密钥和私人消息放进项目报告；内容不会自动脱敏。

## 可选字段

- `jobs`：每条包含 `id`、`label`、`version`、`state`、`elapsed` 和带时区的 `observed_at`。只展示最近观测，五分钟以前标为陈旧；不会查询调度器。未知状态展示为“未观测”。
- `bounds`：包含 `status`（`reference` / `conditional` / `unknown`）、`kind`、`current`、`headline`、`ceiling`、`gap`、`meaning`，以及 `derivation`、`assumptions`、`next_measurements` 字符串数组。可选 `model.reference_percent` 仅用来展示已计算的参考占比。
- `learning.versions`：每项包含 `version`、`status`、`title`、`result`、`before`、`after`、`bottleneck`、`change`、`validate`、`lesson`、`boundary`、`source`。多个项目都有复盘时可在页面切换。

`examples/projects/` 提供完整的虚构示例。接入其他系统时，让自己的采集程序生成相同报告结构即可；看板不内置任何真实队伍、硬件平台、飞书表格或自动同步定时器。
