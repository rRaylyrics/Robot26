# Robot26 · EX001 衣物分类与折叠机器人

本仓库是 ENGI-X 2026 具身智能黑客松挑战赛产品创新赛道的小组项目。目标是在标准工作台上，让 EX001 双臂机器人自主完成衣物分类与折叠，并根据执行后的观测决定下一步。[赛事介绍](https://mp.weixin.qq.com/s/Y8lYVp51eSaLL3oOfdKksA)

**第一版源码位于仓库根目录。** 当前程序可以读取任务计划、按步骤启动官方 EX001 推理 Client，并记录执行结果。随附的样品管分拣计划用于演示通用流程。衣物分类判据、双角对折和本地视觉有限状态机见[叠衣 Agent 架构方案](EX001-叠衣小组Agent架构方案.md)，目前处于设计阶段。

## 离线快速开始

在仓库根目录使用 Python 3.9 或更高版本。以下命令只使用 Python 标准库，无需机器人或 API 密钥。

```shell
python agent.py --plan example_plan.json
python -m unittest discover -s tests -v
```

第一条命令打印红、蓝样品管各自的动作指令、成功条件和超时，并将模拟步骤写入 `logs/events.jsonl`。第二条运行计划解析与机器人状态字段校验测试。也可以直接输入以“然后”或分号分隔的步骤，预览规则拆分结果。

```shell
python agent.py --task "把红色背心放入三角标记框；把蓝色背心放入圆形标记框"
```

## 源码入口

| 文件 | 用途 |
|---|---|
| [agent.py](agent.py) | 命令行入口。选择计划来源、顺序执行步骤、处理判定与重试并写日志。 |
| [planner.py](planner.py) | 读取 JSON、按连接词拆分任务，或使用可选的外部规划模型。每个计划包含 1–12 步，每步有指令、完成条件和超时。 |
| [bridge.py](bridge.py) | 在官方 EX001 Client 容器中按步骤启停 `start_ex001.sh`，支持同步与 RTC 推理。 |
| [checker.py](checker.py) | 可选视觉验收。读取 SDK 头部相机图像，请视觉模型评估当前步骤的成功条件。 |
| [wallx_probe.py](wallx_probe.py) | 独立的推理接口检查工具。输入同期三路 JPEG 和实测状态，从 Wall-X 获取预测动作块，仅打印结果。 |
| [example_plan.json](example_plan.json) · [tests/](tests/) | 示例计划和离线单元测试。 |

## 真机与可选服务

默认运行模式只预览计划。真机联调需要在官方 Linux Client 环境中提供 `WALLX_API_KEY`，核对实机 G/H 夹爪与 `--embodiment`，并确认 `bridge.py` 中的服务地址与赛方下发的地址一致。当前代码使用公开 Wall-X 端点。具体参数见 `python agent.py --help`。[官方 EX001 Client 说明](https://x2robot.com/docs/quantax1pro/v1/model-development/wallx-api/client-run) · [官方 API 协议](https://x2robot.com/docs/quantax1pro/v1/model-development/wallx-api/api-usage-instruction)

| 功能 | 配置与行为 |
|---|---|
| 真机联调 | `--real --safety-ack` 开启，启动前需在终端输入 `ARM`。默认使用同步推理和人工判定，重试前需输入 `RETRY`。 |
| 外部规划 | `--task` 配合 `--llm-plan`，通过 `PLANNER_API_URL`、`PLANNER_API_KEY`、`PLANNER_MODEL` 配置兼容的 Chat Completions 服务。即使仅预览计划，也会调用该服务。 |
| 外部视觉判定 | 真机模式下使用 `--checker vision`，需要官方 `x2robot` SDK 与 `VISION_API_URL`、`VISION_API_KEY`、`VISION_MODEL`。连续两次判定通过才推进。 |

正式比赛要求全程自主。第一版保留了用于联调的人工确认与重试流程，规划中的参赛版本将由机载 FSM 和本地观测完成动作验收与恢复。

## 项目资料

- [当前架构图](EX001-当前架构.md)与[叠衣 Agent 架构方案](EX001-叠衣小组Agent架构方案.md)说明系统分工和候选动作。
- 培训摘要、录音稿、现场照片和原子动作表由小组在本地保存。项目文档中指向这些资料的相对链接适用于小组工作区。公开赛事说明见[公众号原文](https://mp.weixin.qq.com/s/Y8lYVp51eSaLL3oOfdKksA)。
- [AGENTS.md](AGENTS.md)记录项目工作约定和官方资料入口。
