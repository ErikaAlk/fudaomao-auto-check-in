# 项目约束

先读 README.md 与 t6/README.md。

- `attendance.py` 是改版签到入口；`run.py` 保留上游实现。学校接口与登录链在 `lzu_api.py`。
- 任务名称精确匹配，只选择北京时间当天的实例。范围校验与提交使用同一份由 GCJ-02 转换的 BD-09 坐标。
- 签到提交前保存实例标记。结果未知时先回查学校，禁止重复提交；学校未确认成功时禁止发送成功通知。
- T6 运行目录独立于源码目录。部署时将根目录模块与 t6 下的运行模块、用户单元放在同一运行目录；运行状态不能进入 Git。
- 每日北京时间21:05开始，失败结束后等待300秒，最多20次。第5、10、20次失败和成功使用固定事件编号通知；通知 unknown 不重新投递。
- 验证码属于实际学校登录会话，十分钟有效，只允许首次填写。令牌、验证码和私人链接禁止输出到日志。
- Windows 密码使用 DPAPI；Linux 密码使用 Fernet。迁移密码需重新加密，不能直接复用另一系统的密文。
- 验证按改动选择最小范围。环境检查使用 `python verify_environment.py`；Linux调度规则检查在独立运行目录执行 `python verify_rules.py`，不会向学校提交或发送通知。
- 上游远端为 upstream，个人fork为origin。修改通过个人fork中的分支与PR维护。
- 多账号入口为 `t6/manager.py` 与 `ha/custom_components/lzu_attendance/`；HA 管理员鉴权代理访问 T6，管理令牌不能进入前端。
- 各用户学校会话和各任务日志分别隔离。worker 在导入 `attendance`、`lzu_api` 前设置 `LZU_SESSION_DIR`、`LZU_DATA_DIR`、`LZU_TARGET_NAME` 和 `LZU_SIGN_WID`，禁止通过同一进程修改全局变量切换账号。
- 原单账号定时器与管理服务不能同时负责同一账号。迁入必须保留提交标记和每日重试状态，完成后停用旧定时器。
