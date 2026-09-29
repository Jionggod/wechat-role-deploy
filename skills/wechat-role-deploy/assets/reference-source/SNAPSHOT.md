# 脱敏源码快照 v3.5.0

这些文件是已运行部署的实现参考，保留串行事务、LAN重连、物理熄屏、生活时间线、收件媒体、复盘准备和维护逻辑。身份、机器路径和特定LAN地址已机械脱敏，文件清单/大小/SHA256见`source-manifest.json`。

## 不包含与未承诺

没有原始聊天、原件媒体、索引数据库、私人事件、证据卡、人物说明、授权配置、登录文件或第三方程序。这不是从GitHub克隆后即可启用发送的应用包。特别是`persona_context.py`中的主题/证据ID、`life_plan.py`的生活安排、微信资源ID/屏幕几何、原有启动脚本路径和persona适配器的相对目录都是案例基线，需要适配；不是任何人的实时情况或统一标准。

`persona-adapter/`为原人格资料查询/天气与旧生活接口参考，实际应放在自己的`persona_root/scripts/`。它还需要私有world/cards数据和QQ/微信只读索引。不得为让程序启动而伪造历史事实或用空文件冒充已有资料。

部署顺序与需要的数据见[架构](../../references/architecture.md)、[私有输入契约](../../references/persona-media-memory.md)、[验收](../../references/verification-recovery.md)。先复制到独立部署目录，调整自己的输入/路径，再使用`config.example.json`创建私有配置；默认停发，重新确认基线AI说明和观察起点。不要在本快照目录直接运行`run.py run --send`。

参考源码的语法完整性可离线验证；不应在公开仓库验证阶段导入会初始化SQLite/读私有world的模块，也不启动手机或模型。原机真实验收不能替代新机验收。

第三方Mobilerun、scrcpy、Pillow、Codex及其许可不随此包转存；不包含个人skill的全文资料。未知模型额度按本机账号核验，不保证公共可用。
