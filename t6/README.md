# Linux / T6 部署

## Home Assistant 多账号管理

管理服务运行在 T6，HA 自定义集成提供管理员面板和鉴权代理。浏览器通过 HA 登录访问，管理服务令牌保留在 HA 服务端。界面参数使用 ColorOS 17 设计库的 `2026.09.26-coloros17` 规范，资源在 `ha/custom_components/lzu_attendance/www/`。

学校任务名称和北京时间的起止时间从链接对应的详情接口读取，首次尝试时间为任务开始加五分钟，截止时间为学校结束时间。页面显示这些时间，配置接口按识别结果保存。已经安装的管理服务更新后，在每个任务点击“读取任务信息”以保存时间元数据；新任务和从单账号迁入的任务会自动读取。读取失败的任务不会使用缺省时间开始自动调度。仅支持北京时间当天实例；跨日期或开始五分钟后已结束的任务会显示错误。

将根目录 `attendance.py`、`lzu_api.py`、`secure_store.py` 与本目录 `manager.py`、`manager_store.py`、`manager_notifications.py`、`manager_worker.py`、`manager_admin.py`、`lzu-attendance-manager.service` 复制到同一个受保护运行目录。使用该目录的虚拟环境安装 `requirements-manager.txt` 中的依赖；从源码目录安装时执行 `运行目录/.venv/bin/pip install -r t6/requirements-manager.txt`。

在运行目录执行：

```bash
.venv/bin/python manager_admin.py init
```

该命令创建 `manager.sqlite3`、`accounts/` 和 `manager.token`，权限仅限运行账户。首次使用可以直接从管理页面创建用户。迁入现有单账号时，先停用原有两个定时器与服务，运行 `server_runner.py --flush` 核实原通知，再执行 `manager_admin.py import-legacy`。迁入操作读取当前学校任务，保留密码密文、学校会话、历史提交标记和每日重试状态；完成后由管理服务负责调度，旧定时器保持停用。原文件保留在受保护运行目录。

将 `lzu-attendance-manager.service` 安装到 `~/.config/systemd/user/`，核对运行路径和监听地址，执行 `systemctl --user daemon-reload` 与 `systemctl --user enable --now lzu-attendance-manager.service`。该服务端口为8796，应只允许受信任内网访问，不将它直接发布到互联网。部署需要保持用户 linger 已启用。服务使用单个进程，任务执行由独立子进程承担，同一用户的学校操作串行执行。

将 `ha/custom_components/lzu_attendance/` 复制到 HA 的 `/config/custom_components/lzu_attendance/`。创建只允许管理员读取的 `/config/lzu_attendance_secrets.yaml`：

```yaml
url: http://T6内网地址:8796
token: 管理服务manager.token的内容
```

HA 使用 macvtap 虚拟网卡且管理服务位于同一宿主时，需要检查虚拟机是否能够访问宿主。存在网络隔离时，将上面的 URL 指向可同时访问两端的内网转发服务，限定 HA 来源地址和8796端口，并保持管理令牌鉴权。

在 HA `configuration.yaml` 中添加：

```yaml
lzu_attendance: !include lzu_attendance_secrets.yaml
```

检查 HA 配置并重新启动 Core，侧栏出现“签到”。外部访问沿用 HA 的 HTTPS 地址。管理接口和页面入口都要求 HA 管理员身份；静态脚本不包含账号凭据。

QQ通知保留现有 `notify.json` 和 `notify.token`，Robin来源必须是独立的 `lzu-attendance`、scope为`notify`，并部署Robin的签到专用绑定模块。服务对每项任务的成功以及第5、10、20次失败使用固定事件编号；通知状态为`unknown`时停止自动投递。原有本人通过受保护`config.json`中的账号名称识别，继续使用本人QQ；其他用户独立绑定，未绑定时通知暂停。新建用户可选择“绑定Robin提醒”，在QQ私聊发送十分钟有效的一次性`/bind 六位码`。

通知的用户、绑定版本与接收人首次接受时固定；重新绑定不会把旧的未知投递改投新QQ。Robin拒绝其他来源访问签到绑定接口，其他QQ的普通消息、附件和个人业务控制不会进入本人通道。

验证码沿用`config.json`的`otp_service_url`和`otp.token`访问下述公开网关。每个用户和登录操作有独立请求，创建前保存关联记录；公开页面无需HA登录，只接受一次验证码提交。学校认证链及登录查询确认后发送`verified`确认，链接立即返回410并清除代码；失败、取消、过期及进程中断均撤销。HA仅提供专属链接，同一操作不能再从HA验证码接口输入。QQ通知按对应用户路由，临时链接不会发送到其他账号。

日常检查：

```bash
.venv/bin/python manager_admin.py status
systemctl --user status lzu-attendance-manager.service
journalctl --user -u lzu-attendance-manager.service
```

管理服务关闭 HTTP 访问日志，学校操作进程的原始输出不写入服务日志。异常仅显示操作类型与脱敏原因。服务中断后原操作标为失败，提交标记继续保留；对已经发起但结果未知的实例只回查学校，禁止再次提交。界面关闭不会中断服务器调度。

## 单账号调度与通知

北京时间每日21:05开始，失败结束后等待300秒，再进行下一次尝试，每天最多20次。第5、10、20次失败发送QQ通知；学校确认签到成功后通知并结束当天执行。SQLite保存计数与通知内容，重启后恢复；中断的检查先只读查询学校。

通知接口兼容 Robin 的 `POST /api/notifications`：使用独立 Bearer 凭据，提交固定 `event_id`、`title` 与 `text`。服务端需保证相同事件编号和内容返回原状态，`unknown` 不自动重新发送。接收人由通知服务绑定，客户端不指定接收人。

## 准备运行目录

需要 Linux、Python 3.12或3.13、venv 组件和 systemd 用户服务。源文件按下表复制到独立运行目录，例如 `~/.local/share/lzu-attendance`，目录权限设为0700。

| 源文件 | 运行目录内位置 |
| --- | --- |
| 根目录 `attendance.py`、`lzu_api.py`、`secure_store.py`、`requirements.lock.txt` | 保持文件名 |
| 本目录 `server_runner.py`、`sms_provider.py`、`setup_server.py`、`verify_rules.py` | 放在运行目录根部 |
| 本目录 `lzu-attendance.service`、`lzu-attendance.timer`、`lzu-attendance-notify.service`、`lzu-attendance-notify.timer` | 放在运行目录根部 |

在运行目录内创建 `.venv` 并安装 `requirements.lock.txt`。源仓库和运行目录分别维护；更新已运行的程序前先停用两个定时器并停止两个服务。

## 配置与启用

复制 [configure.example.json](configure.example.json) 到仓库外的受保护文件，填写校园卡号、密码、实际住宿位置、通知接口和独立令牌。配置文件权限设为0600。`otp_service_url` 指向下节的HTTPS验证码服务，`otp_token` 与该服务的鉴权令牌一致。

在运行目录执行：

```bash
.venv/bin/python setup_server.py configure < /secure/attendance-config.json
.venv/bin/python attendance.py --check
.venv/bin/python server_runner.py --verify-now
.venv/bin/python setup_server.py enable
.venv/bin/python setup_server.py status
```

`configure` 从标准输入读取配置并在Linux本机重新加密密码。`--check` 只查询学校状态；`--verify-now` 同样不提交签到，会按实际查询结果保存本次检查，并在学校确认已签到时发送成功通知。启用前应完成学校登录和通知核查。启用操作需要通过 `sudo -n` 设置用户 linger，以便退出登录后继续执行。

每分钟的通知补查定时器会使用原事件编号核实网络请求，平台返回 `unknown` 时停止自动投递。日常检查使用 `systemctl --user list-timers --all 'lzu-attendance*'` 与 `journalctl --user -u lzu-attendance.service`。

从已有安装迁移时，同账号可通过配置输入中的 `session.json`、`cookies.txt` 及 `submissions` 映射迁入原会话和提交标记。曾提交但结果未知的实例必须保留提交记录，确认新机器可运行后再停用旧机器。

## 短信验证码服务

学校会话与持久令牌失效时使用账密重登；学校要求短信时，T6立即通过QQ发送十分钟有效的填写链接。填写完成后继续该学校登录会话。验证码加密保存，公开页面不返回验证码，仅持独立令牌的T6可以读取。

验证码服务可与通知服务位于同一台HTTPS服务器，使用独立运行目录和账户：

1. 将 `otp_gateway.py` 放入 `/opt/lzu-attendance-otp`，在该目录建立 `.venv`。从源码目录执行 `pip install -r t6/requirements-otp.txt` 时，指定该虚拟环境的 pip。
2. 创建服务账户 `lzu-attendance` 和数据目录 `/var/lib/lzu-attendance-otp`，数据目录权限0700。使用 `cryptography.fernet.Fernet.generate_key()` 生成 `data.key`，仅服务账户可读写，权限0600。
3. 生成独立高强度接口令牌，通过受保护文件交给T6；服务端将令牌的SHA-256摘要保存到 `/etc/lzu-attendance-otp/token.sha256`，服务账户仅需读取摘要。
4. 参考 `otp.env.example` 创建 `/etc/lzu-attendance-otp/env`，配置实际HTTPS源站地址 `LZU_OTP_ORIGIN`。它用于填写链接和浏览器Origin校验；路径前缀固定为 `/attendance-otp`。
5. 安装 `lzu-attendance-otp.service`，参考 `lzu-attendance-otp.nginx.conf` 将现有HTTPS站点的 `/attendance-otp/` 转发至回环端口8795，关闭该路径的访问日志。
6. 检查服务及反向代理配置，验证 `/attendance-otp/health` 返回 `{"ok":true}`，确认无鉴权请求无法创建或读取验证码，再启用服务。

部署更新后需重新启动验证码服务。学校没有触发短信时，填写页面的健康和鉴权检查不能替代真实短信登录验证。
