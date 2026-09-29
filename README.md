# 微信角色真机部署技能

中文项目名：**微信角色真机部署技能**。GitHub地址标识为`wechat-role-deploy`；GitHub仓库名称目前只接受ASCII字母/数字及`.`、`-`、`_`，不能直接使用汉字，见[官方命名说明](https://docs.github.com/zh/repositories/creating-and-managing-repositories/creating-a-new-repository)。项目简介、技能显示名和使用文档均使用简体中文。

Windows＋真实安卓备用机上的知情AI角色：从USB初始化到局域网运行、物理熄屏、人格检索、连续生活与双账本、收件媒体理解、受控主动聊天，以及每日沟通复盘的部署skill与技术留档。

这是**部署方法和脱敏参考源码**，不是微信官方机器人API，也不是下载即可自动联系他人的成品。私人聊天、原始媒体、人物档案、具体身份、地址、真实运行状态及登录凭据均不在仓库中。参考源码保留原有私有数据接口；必须接入自己的授权资料并完成设备适配，不能把缺资料的模板说成已部署。

## 使用

将[skills/wechat-role-deploy](skills/wechat-role-deploy/SKILL.md)整个目录复制到自己的Codex技能目录，保留其子目录。不要覆盖同名已有skill而不比较。

可在Codex中说：

> 使用 $wechat-role-deploy，检查我的既有部署状态，保持角色、频率和收件人不变。

> 使用 $wechat-role-deploy，在独立目录准备真机局域网部署；先验证，不启用发送。

主要材料：

- [架构与依赖](skills/wechat-role-deploy/references/architecture.md)
- [设备、USB转LAN与熄屏](skills/wechat-role-deploy/references/device-deployment.md)
- [人格、媒体、双账本和私有输入](skills/wechat-role-deploy/references/persona-media-memory.md)
- [运行、模型、主动机会与低token策略](skills/wechat-role-deploy/references/runtime.md)
- [每日复盘与时区教训](skills/wechat-role-deploy/references/review-scheduling.md)
- [验收、运维、回退](skills/wechat-role-deploy/references/verification-recovery.md)
- [版本快照说明](skills/wechat-role-deploy/assets/reference-source/SNAPSHOT.md)

## 验证范围

来源部署v3.5.0曾通过真实USB拔线、LAN重连、联系人身份、Off面板＋Awake系统与进程心跳检查；对应61项定向测试通过。私人验收文件不公开，这些结果不代表其他型号/微信版本已通过。

本仓库另有只读检查器和合成数据测试：

```powershell
python -X utf8 -m unittest discover -s tests -v
python -X utf8 .\skills\wechat-role-deploy\scripts\inspect_runtime.py --root 'D:\RoleBridge'
```

检查器不操作手机、不调用模型、不写部署目录、不输出聊天正文。它只验证落盘状态；需要另查进程和实际设备才能完成端到端验收。

公开存档不打包第三方可执行文件或个人方法库，依赖需从原来源取得并遵守其许可。技能结构参考[OpenAI官方技能文档](https://developers.openai.com/plugins/build/skills)。
