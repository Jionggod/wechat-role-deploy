# 架构、依赖与落盘结构

## 已验证路线及范围

主机为中文Windows，真实安卓备用机已由用户登录专用微信；电脑使用自己的已登录Codex。选真机是因为该部署的模拟器候选存在界面干扰、兼容或构建成本问题，不代表所有模拟器永远不可用。不要为留档再安装或构建替代模拟器。

来源基线：Python3.12，mobilerun-core 1.6.2、mobilerun-core-local 0.6.0、mobilerun-sdk 5.5.0，scrcpy4.1，Android12。运行环境已验证时不例行升级。不同版本需重新检查API、界面节点和发送确认。

依赖包括Android platform-tools/ADB、Mobilerun Portal及本机已验证的无障碍组合、Pillow（新媒体截图区域）、Codex CLI/app-server、scrcpy。版本以本机记录为准；不打包第三方APK/EXE。无障碍树若不可用，先解决权限/前台/锁屏，不把常态轮询改成高频截图。

## 运行关系

```text
用户授权的历史/原件（只读私有）→ 证据/人格检索 ┐
真实本机时间与天气缓存 → 本地生活时钟 → 双账本 ├→ 对话模型 → 持久发送事务 → 手机
手机唯一绑定会话 → 新收件锚点 → 文本/媒体理解 ┘                   ↓
每晚闭合窗口 → 只读整理 → 沟通复盘（独立Codex会话，不发微信）    确认后记忆
```

- `phone.py`：唯一联系人身份、消息解析、文本输入/发送回读；`screen_power.py`仅控制物理面板。
- `run.py`＋`bridge_state.py`：串行监听、消息游标、主动门禁、发送事务与心跳。
- `model_client.py`：本机Codex stdio、模型验证、最终JSON与回退；`prompt_context.py`去重上下文副本。
- `timeline.py`＋`life_plan.py`＋`local_runtime.py`：版本事件、滚动日程、情绪/天气/常驻时钟。
- `persona_context.py`＋`v3_context.py`：少量同场景原话、生活资料与分层上下文。
- `incoming_media.py`：锚点绑定新收件；`media_catalog.py`/`delivery.py`保留候选与多段事务结构，不能因此声称自动发图已可用。
- `coaching_scenes.py`/`coaching_review.py`：自然生活情境、可撤销压力练习、闭合窗口复盘证据。
- `maintenance.py`：既有停止/不确定性/进程检查，只通过原入口必要恢复；不直接代发。

## 目录与私有边界

建议部署在独立项目目录，运行数据和公开skill分离：

```text
RoleBridge/
  config.json                 私有配置，身份/路径/发送开关
  persona_profile.md          私有角色规则
  *.py, *.ps1                 经本机验证的代码及隐藏启动入口
  data/                       私有工作资料、媒体索引、练习模式
  persona_review/             有时间和来源的历史证据
  runtime/                    状态、锁、停止标志、SQLite和日志
  evidence/                   私有真实验收/发送审计
  虚构记忆/YYYY/MM/DD/         客观模拟事件、实际AI对话与运行轨迹
  沟通复盘/YYYY/MM/DD/         窗口证据、manifest和报告
```

新机部署需重新配置根目录、Python、ADB、scrcpy、persona根目录和skill路径。源代码中的专属话题、证据位置、排班和2026调休日历都是基线，不得直接套作另一个人的事实。模型名也需要本账号可用性验证。

本skill包含脱敏实现参考，不包含私人索引、人格库或登录。缺这些文件时应列出缺项，绝不能用空库造“已有真人记忆”。
