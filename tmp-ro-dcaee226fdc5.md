【只读定位】结论：已定位原因

现象：CFT02 老化进入「器件老化」（BURN_IN_CIRCLE_PERIPHERAL）后电流升高。该阶段不是把马达、PPG、ECG、EDA 一次性全部打开，而是在整段时间里保持指示灯快闪、蓝牙广播和三轴加速度计，再按项叠加上马达/六轴/地磁/ECG/EDA。PPG 老化项本身的开关被注释，不会由该项点灯。

代码原因：进入器件老化时强制切到 MODE_ID_FACTORY_TEST_BURN，灯效为红/绿各 200ms 循环，模式超时为 0，阶段结束前不关灯。nrf54_user_init 在老化恢复之后仍启动蓝牙广播，且在 burn_in_main_init 之前已启动 HAL_ACC_ID_TOW，器件老化不停止这两路。器件项默认各开 2s、停 1s、循环 1 次，按项调用停止函数；ACC 停止只停 HAL_ACC_ID_ONE，停不掉已开的 ID_TOW。产测标志为真时健康监测不会自动开/关 PPG；高温老化会先清掉该标志再跑同一套器件老化，并启动透射血氧，对应的 health_monitoring_stop 写在不可达分支里。CFT02 的 tspo2_init 被注释，高温路径的 TSPO2 没有注册处理函数。

位置：
- middleware/factory/burn_in/burn_in_main.h:15 BURN_IN_CIRCLE_PERIPHERAL — 老化状态机中的器件老化阶段
- middleware/factory/burn_in/burn_in_circle_peripheral.c:636-645 burn_in_circle_peripheral_exec_task_cb_handle — 未完成时启动器件项，并 operation_mode_force_launch(MODE_ID_FACTORY_TEST_BURN)；同函数内关灯调用被注释
- middleware/factory/burn_in/burn_in_circle_peripheral.c:199 burn_in_cp_item_step_switch — 仅在全部器件轮次结束时 led_display_control(DISPLAY_STYLE_OFF_ALL)
- middleware/factory/burn_in/burn_in_circle_peripheral.c:628 burn_in_circle_peripheral_exec_task_cb_handle — 次数已满时才关灯并切下一老化阶段
- application/cft02/src/operation_mode_obj_instance.c:515-521 mode_start_func_if_inst_factory_burn — 进入老化模式即 DISPLAY_STYLE_BURN_PERIPHERAL
- application/cft02/src/operation_mode_obj_instance.c:813-814 operation_mode_obj_instances — MODE_ID_FACTORY_TEST_BURN 超时为 0，灯效不会因超时结束
- application/cft02/src/operation_mode_schedule_task.c:137-138 disply_style_burn_peripheral — 红灯 200ms、灭 200ms、绿灯 200ms、灭 200ms 循环
- application/cft02/src/user_init.c:1573-1574 nrf54_user_init — 器件老化前 hal_acc_start_by_id(HAL_ACC_ID_TOW, ACC)，之后无对应 stop
- application/cft02/src/user_init.c:1586 nrf54_user_init — burn_in_main_init 恢复器件老化
- application/cft02/src/user_init.c:1634-1636 nrf54_user_init — 非晶振测试即 ble_if_start_adv，器件老化不关广播
- middleware/factory/burn_in/burn_in_circle_peripheral.c:777-819 burn_in_circle_peripheral_set_defualt — CFT02（CONFIG_FIRMWARE_CFT）默认打开马达、ACC、PPG、Flash、地磁、热通量、ECG（板级 conf）、EDA，各 1 次、开 2s、停 1s
- middleware/factory/burn_in/burn_in_circle_peripheral.c:340-349 burn_in_cp_motor_start_handle / stop — 马达产测开/关成对调用
- sensors/basework/drive/motor/mdh2/Motor_MDH2_rotary.c:1216-1286 Motor_Production_testing_start — 写 GO=1 让马达转；1302-1311 Motor_Production_testing_stop 写 GO=0
- middleware/factory/burn_in/burn_in_circle_peripheral.c:365-381 burn_in_cp_acc_start_handle / stop — 启动/停止的是默认六轴 HAL_ACC_ID_ONE
- middleware/hal/acc/hal_acc.c:323-336 hal_acc_start_freq / hal_acc_stop — 只作用于 HAL_ACC_ID_ONE，停不掉 user_init 里的 ID_TOW
- middleware/factory/burn_in/burn_in_circle_peripheral.c:384-396 burn_in_cp_ppg_green_start_handle / stop — hal_ppg_start/stop 已注释，PPG 项不点灯
- middleware/factory/burn_in/burn_in_circle_peripheral.c:477-486 burn_in_cp_geomagnet_start_handle / stop — 地磁 factory_start 后有 magnetic_if_stop
- middleware/factory/burn_in/burn_in_circle_peripheral.c:522-540 burn_in_cp_ecg_* / burn_in_cp_eda_* — 板级 CONFIG_ECG_PAH8311=y、CONFIG_EDA_PAH8320=y 时各开 2s 再 stop，与前后项串行
- application/cft02/boards/nrf54lm20dk_nrf54lm20a_cpuapp.conf:409-412 — 板级打开 ECG 与 EDA；prj.conf 中 ECG 为 n，以实际合并后的板级配置为准
- middleware/factory/burn_in/burn_in_circle_peripheral.c:492-500 burn_in_cp_core_body_start_handle / stop — start 传入 NULL，stop 为空
- sensors/basework/drive/temperature/OPA2333/OPA2333.c:189-196 hfs_factory_start — 回调为 NULL 时直接 return，热通量定时采样实际没启动
- middleware/health_monitoring/health_monitoring_strategy/health_monitoring.c:451-452 health_monitoring_start — get_factory_test_info() 为真时不调用 ppg/传感器 start
- middleware/health_monitoring/health_monitoring_strategy/health_monitoring.c:613-616 health_monitoring_time_cb — 未绑定时会请求佩戴检测
- middleware/health_monitoring/ppg/wear_mode.c:17-19 wear_start_handle — 佩戴检测开红外 PPG；产测标志为真时被上一处挡住
- middleware/factory/common/factory_process.c:1685-1693 — 高温老化先 set_factory_test_info(false)，再进入器件老化，从而允许自动开 PPG
- middleware/factory/burn_in/burn_in_circle_peripheral.c:696-702 burn_in_start_hightemp — 高温路径启动器件老化并 health_monitoring_start(TSPO2)
- middleware/factory/burn_in/burn_in_circle_peripheral.c:194-206 burn_in_cp_item_step_switch — health_monitoring_stop(TSPO2) 位于 is_execute_burn_hightemp==false 的 else，到不了
- application/cft02/src/user_init.c:1268 health_monitoring_mode_init — tspo2_init 被注释，CFT02 未注册 TSPO2 处理函数

未改代码。
