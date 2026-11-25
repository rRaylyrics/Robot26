# Robot26 · EX001 衣物分类与折叠机器人

本仓库是 ENGI-X 2026 具身智能黑客松挑战赛产品创新赛道的小组项目。目标是在标准工作台上，让 EX001 双臂机器人自主完成衣物分类与折叠，并根据执行后的观测决定下一步。[赛事介绍](https://mp.weixin.qq.com/s/Y8lYVp51eSaLL3oOfdKksA)

## 第二版 · Qwen 动态任务 Agent

[第二版入口](action/agent.py)使用两层 FSM。任务规划每轮选择原句直发、一个库内动作或一个自定义动作；单动作 FSM 根据 Qwen 的多模态反馈验收、补观测或修订指令。通过验收后再处理余令。[第二版架构与数据流](EX001-小组Agent架构方案V2.md)

在仓库根目录使用 Python 3.10 或更高版本运行以下离线命令。配置检查和单元测试均可在没有 API 密钥、厂商 SDK 或机器人的环境中运行。

```shell
python -m action.agent
python -m unittest discover -s tests -v
```

仓库提供空数据模板。将 `data/` 复制成自己的运行配置目录，在 `config/` 填写组员审核的预设提示词、动作库与任务约束，在 `task/progress.json` 的 `original` 填写原始任务，也可通过 `--task` 提供。衣物预期件数可填入 `scene/garments.json`。库存、当前尝试、图像和运行日志由程序写入所选目录的 `runs/<运行编号>/`，与预设模板分开。图像模板使用空路径清单。

| 真机配置 | 用途 |
|---|---|
| `QWEN_API_KEY`、`QWEN_BASE_URL` | 千问 AI 平台按量付费密钥及 OpenAI 兼容地址。在项目根目录的 .env 中填写。 |
| `QWEN_TEXT_MODEL`、`QWEN_VISION_MODEL` | 规划、优化和多图监督的模型。默认均为 `qwen3.8-flash`，按账号已开通的型号配置。 |
| `WALLX_MODEL_ADDRESS`、`WALLX_API_KEY` | 赛方 Wall-X 地址和密钥，供官方 Client 执行动作。 |
| `--server`、`--embodiment` | EX001 SDK 地址和实机 G/H 夹爪型号。适配器依赖官方 Linux Client、兼容的 `x2robot` SDK 和 Pillow，读取头部 RGB 与左右腕部 JPEG/PNG。仅提供 H.265 的构型需要另接带时间戳的同步解码器。 |

完成配置后，真机入口形式为 `python -m action.agent --real --data <配置目录> --server <主机:端口> --embodiment <实机型号>`。显式启用真机模式会将现场图像及状态提交给配置的模型，并允许机械臂运动。联调时应核对相机编码、各路时间戳、停稳和保持夹持的实际表现。 [千问平台 API Key 与接口地址](https://platform.qianwenai.com/docs/api-reference/preparation/api-key) · [视觉模型说明](https://platform.qianwenai.com/docs/models/qwen3-8-flash)

## 项目资料

- [第二版架构方案](EX001-小组Agent架构方案V2.md)说明当前系统分工；[早期叠衣方案](EX001-叠衣小组Agent架构方案.md)保留候选动作与物理判据。
- 培训摘要、录音稿、现场照片和原子动作表由小组在本地保存。项目文档中指向这些资料的相对链接适用于小组工作区。公开赛事说明见[公众号原文](https://mp.weixin.qq.com/s/Y8lYVp51eSaLL3oOfdKksA)。
- [AGENTS.md](AGENTS.md)记录项目工作约定和官方资料入口。
