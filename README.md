# Cron Win (Python + Qt)

一个带图形界面的轻量级定时任务工具，功能类似 Linux `cron`：

- 支持 `cron` 表达式配置任务
- 支持任务新增 / 编辑 / 删除 / 启用禁用
- 支持立即手动执行
- 展示上次执行、下次执行、执行状态
- 运行日志实时显示
- 任务保存在本地 `tasks.json`

## 运行（不使用虚拟环境）

推荐方式（双击）：

- 直接运行 `start.bat`

命令行方式：

```bash
pip install -r requirements.txt
python main.py
```

## Cron 表达式示例

- `*/5 * * * *` 每 5 分钟
- `0 * * * *` 每小时整点
- `0 9 * * 1-5` 工作日每天 9 点

## 说明

- 命令通过系统 shell 执行（Windows 下通常是 `cmd`）。
- 为了避免界面卡顿，任务执行在后台线程中进行。
