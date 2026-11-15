# Linux / T6 部署

## 调度与通知

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
