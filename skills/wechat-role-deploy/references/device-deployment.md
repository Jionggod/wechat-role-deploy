# 真机、局域网与熄屏

## 初次设备绑定

1. 先检查现有停止标志及发送事务，再枚举ADB设备。没有设备时不能编造序列号。
2. 用户在手机启用开发者选项/USB调试，并在设备上完成授权。主机核对唯一目标的`ro.serialno`、安卓版本、Wi-Fi地址；保存硬件身份和连接地址为两个字段。
3. 微信登录/验证码/安全锁屏由用户处理。只服务明确授权的联系人；备注可重名，发送前必须进入资料页核对微信号，确认单聊而非群聊。
4. 正常控制经`from mobilerun_core import Mobilerun`，`connect(target, backend="local-android-adb", portal_mode="required")`；检查capabilities后使用。不要直接导入SDK内部driver。资源ID和左右气泡几何是版本适配，不是固定真理。
5. 保存原无障碍、输入法、供电唤醒设置后才改动。来源部署验证过SelectToSpeak与Portal组合、触摸探索为0；不得覆盖用户其他无障碍服务。

## USB初始化到LAN

官方参考：[Android ADB无线连接](https://developer.android.com/tools/adb#wireless)。Android11+也可使用系统无线调试配对；下面是已验证的USB初始化方式，不是永久TLS配对。

```powershell
# 以下变量必须用实测/用户授权值；示例地址仅作说明。
$adbExe = 'C:\Android\sdk\platform-tools\adb.exe'
$hardwareSerial = 'YOUR_DEVICE_SERIAL'
$lanEndpoint = '192.0.2.10:5555'
& $adbExe -s $hardwareSerial shell getprop ro.serialno
& $adbExe -s $hardwareSerial shell ip -4 addr show wlan0
& $adbExe -s $hardwareSerial tcpip 5555
& $adbExe connect $lanEndpoint
& $adbExe -s $lanEndpoint shell getprop ro.serialno
& $adbExe devices -l
```

执行前正常暂停手机监听并保留队列。本机生活时钟可以继续；维护看到停止标志不重启。Wi-Fi端`ro.serialno`必须等于已绑定硬件；随后仅新增/更新`adb_target`，Phone、Mobilerun和scrcpy统一选择该地址。`device_serial`不能被IP覆盖。

连接恢复有界：读取ADB传输ID，缺失时只连接已核对的固定目标一次；传输变化后重新验证硬件、建立SDK通道、核对微信联系人。中途操作发现变化即中止，只能到下一轮安全边界恢复；不重放点击发送。ADB服务重启可能重用传输ID，因此已实际观测断线的恢复不能只凭ID相同跳过重建。

只撤销本次迁移创建并核对归属的停止标志。启动后核对新PID/心跳与熄屏，再请用户物理拔线（无法软件代替此物理输入）。拔线后设备列表应无USB序列号、LAN身份正确、监听心跳推进。未拔线不能声称脱线验收通过。

只用于可信局域网，不做公网端口映射或扫网。手机重启可能关闭TCP调试，DHCP改址可能让固定地址失效；重新核验，必要时短接USB。电脑/网络离线不可能保证微信实时应答。

## 物理熄屏，不是深睡或锁定

[scrcpy屏幕控制](https://github.com/Genymobile/scrcpy/blob/master/doc/device.md)提供控制通道。来源版本参数：

```text
scrcpy --serial=<当前连接目标> --no-video --no-audio --no-window --no-clipboard-autosync --turn-screen-off --no-power-on
```

隐藏启动，无视频/音频/窗口/剪贴板同步；日志使用文件而非可能被后台进程继承的PIPE。scrcpy由监听进程持有，每次读取UI前检查子进程，约60秒核对面板，正常停止清理助手、恢复已保存设置。

需要同时证明SurfaceFlinger面板`Off`和dumpsys power `Awake`。备用机可接墙充，旧USB连接不再必须；不要把黑屏等同系统休眠。无安全锁屏且状态明确时，Mobilerun专用`wakeup`键恢复，前后复核锁屏，再回到绑定会话。不能用可能反向休眠的power切换键。锁屏安全/未知状态阻塞，不能绕过。

未经休眠或重连恢复，不应抢占用户手动切换到的其他应用。电话/权限弹窗等使界面不明时保留状态，不猜坐标发送。
