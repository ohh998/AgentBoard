---
{
  "title": "示例：实现一个独立小功能",
  "revision": 0,
  "status": "todo",
  "priority": 1,
  "resources": ["example-feature/"],
  "depends_on": [],
  "required_capabilities": ["python"]
}
---

## 目标

说明用户需要的行为、输入输出与完成条件。将标题、资源路径和能力标签替换为真实值，再通过 `python -S agentctl.py --profile YOUR_PROFILE create --file examples/task-template.md` 创建。

## 工作范围

- 仅修改声明的 `example-feature/` 目录；实际任务请替换资源路径。
- 若需要其他目录，先停止，交接给管理员调整资源范围，再重新领取。
- 依赖任务需全部达到 `done` 后才能认领。

## 验收条件

- 写出可验证的行为和边界。
- 给出运行/验证步骤与结果。
- 更新相关维护说明。

## 交接要求

提交时列出变更文件、验证命令、结果、实际限制和下一步。不要填写访问令牌、完整上下文或其他敏感内容。
