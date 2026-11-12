【只读定位】结论：无法定位

现象：双敲手势不灵敏，手势关闹钟不灵敏。cft02 日常加速度在 BMA510（HAL_ACC_ID_TOW），BMA510 驱动没有双敲识别。闹钟开始震动时才把 BHI385（HAL_ACC_ID_ONE）切到 ACC|GYRO|MULTI_TAP。识别参数写在 BHI385 启动里，闹钟回调本身不再判断是否双敲。

代码原因：不能证明“不灵敏”就是某一个阈值。头文件没有给出 tap_peak_thres、max_gesture_dur、shock、quiet 的物理单位，仓库里也没有把某次双敲的峰值/间隔对上这些寄存器。mode=2 的注释写 Sensitive，宏却把 2 定义成 ROBUST，和厂商示例注释冲突，不能单凭注释断定芯片按最钝档工作。应用层没有把已上报的敲击丢掉。

位置：
- sensors/basework/drive/gsensor/bhi385/bhi385_handle.c:913 bhi385_imp_start — mode=2，注释写 Sensitive mode
- sensors/basework/drive/gsensor/bhi385/include/bhi385_multi_tap_param_defs.h:73 FILTER_MODE — SENSITIVE=0，NORMAL=1，ROBUST=2，与上一行注释冲突
- sensors/basework/drive/gsensor/bhi385/bhi385_handle.c:914 bhi385_imp_start — tap_peak_thres=320（10bit 峰值下限）
- sensors/basework/drive/gsensor/bhi385/bhi385_handle.c:915 bhi385_imp_start — max_gesture_dur=16（第一击后匹配双敲的最长时间）
- sensors/basework/drive/gsensor/bhi385/bhi385_handle.c:918-924 bhi385_imp_start — #if 0 保留 shock=6、quiet=8；实际编译走 #else，tap_shock_settling_dur=15、min_quite_dur_between_taps=15，均为 4bit 上限。字段语义是冲击稳定等待和两次敲击之间的最小静默，调到上限会收紧双敲时间窗。同文件 807-813 的 test_bhi385_*（阈值 256、时长 10、shock 3、quiet 4）未被赋值使用
- sensors/basework/drive/gsensor/bhi385/bhi385_handle.c:909 bhi385_imp_start — axis_sel=2，只认 Z 轴
- sensors/basework/drive/gsensor/bhi385/bhi385_handle.c:386-401 parse_multi_tap — data_size!=2 直接 return，否则把敲击结果交给回调，这里没有第二道阈值
- middleware/health_monitoring/sport/multi_sport.c:1530-1548 gsensor_switch_working_mode_sch — app_alarm_get_gryo_flag 非 0 时停 BMA510，启动 BHI385 的 MULTI_TAP
- application/cft02/src/interactive_and_reminder.c:181-217 app_alarm_time_up_notity_cb_handler — 置 alarm_open_gryo_flag 并异步切传感器
- application/cft02/src/interactive_and_reminder.c:68-110 gesture_cb_handler — GESTURE_TYPE_DOUBLECLICK 判断和 app_gerture_control_get_is_close_alarm 判断都被注释，只要 has_alarm_vibrate 就停震。回调类型是 acc_get_rawdata_cb_t，参数未再过滤敲击类型
- application/cft02/src/interactive_and_reminder.c:284 interactive_and_reminder_init — 把上述回调注册到 HAL_ACC_ID_ONE 的 multi-tap
- application/cft02/src/user_init.c:1573-1574 user_init — 开机停 BHI385，日常只开 BMA510 加速度

候选（按优先级，均未证实为现场原因）：
1. 914-924 的峰值阈值加上 shock/quiet 拉到 15，再叠加闹钟马达震动，静默窗不容易满足。这是最像“不灵敏”的参数，但没有单位和实测峰值，不能定因。
2. mode=2 若芯片按 ROBUST 解释，会比 SENSITIVE 更难触发；若按示例注释当灵敏档，则不是原因。
3. axis_sel=2 只认 Z，敲击方向不对会被丢掉。
4. bhi385_handle.c:826 在 ACC 与 GYRO 都已打开时直接 return，不再配置 MULTI_TAP。闹钟首次从关闭的 BHI385 启动时走不到这里。
5. parse_multi_tap 的 data_size!=2 若与固件事件长度不一致，会把敲击全部丢掉，表现应是完全无响应，而不是偶发不灵。

未改代码。
