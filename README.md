# RepoFix · 证据驱动的多 Agent 程序修复与可验证评测

RepoFix 是面向 Python 仓库缺陷修复的多 Agent 实验系统。它把 Bug 描述、公开测试失败回溯和 AST／近似调用关系交给定位与修复工作流，在隔离副本中生成补丁，再用公开测试和独立验收测试检查结果。

**LangGraph 已作为可选执行引擎接入。** 默认仍使用项目原有调度；两种引擎复用相同的 Agent 角色、补丁门槛和评测逻辑。

## 能做什么

- 运行单 Agent、固定双 Agent、公开测试反馈驱动的动态协作，以及独立双候选竞争四种工作流。
- 用 Python AST、失败回溯和近似调用图缩小代码范围；定位 Agent 和修复 Agent 可使用同一底层模型，但提示词、输入和输出职责不同。
- 在临时工作区修改代码，并检查改动路径、补丁可应用性、语法和部分可疑修复模式。
- 在受限 Docker 容器中运行公开 pytest；最终补丁在另一份副本中接受测试。验收测试不进入 Agent 的上下文，也不参与候选选择。
- 保存补丁、测试状态和模型调用记录，并在本地 React 看板查看。Docker 限制降低运行风险，但不应被视为绝对安全边界。
- 通过 `--engine legacy|langgraph` 切换原有调度与 LangGraph 状态图调度；`--workflow` 仍独立控制四种协作模式。

## 四工作流与初步结果

2026-10-22 的同版单轮对照使用 20 道 BugsInPy 任务、`deepseek-v4-pro`、开启思考且 `reasoning_effort=low`；每次请求上限为 20,000 completion Token，单次等待上限为 300 秒。四组均为 20/20 题提交了可评测补丁。

| 工作流 | 公开测试通过 | 隐藏验收通过 | 最终通过 | 模型调用 |
| --- | ---: | ---: | ---: | ---: |
| 单 Agent | 17/20 | 16/17 | 16/20（80%） | 21 |
| 固定双 Agent | 19/20 | 18/19 | 18/20（90%） | 41 |
| 动态协作 | 19/20 | 18/19 | 18/20（90%） | 45 |
| 独立双候选竞争 | 18/20 | 17/18 | 17/20（85%） | 76 |

隐藏验收只对公开测试通过的补丁执行，因此该列分母不是 20。以上只是单批题目、每组一轮的描述性结果，**不能据此宣称统计显著或普遍更优**。运行记录和实验报告保留在本地，不随公开仓库上传。

## 快速开始

需要 Python 3.11+。运行 Docker 后端时，还需要 Docker Desktop 的 Linux 容器引擎。Node.js 只在开发前端时需要。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q

docker build -f docker/Dockerfile -t repofix-pytest:py311 .
docker build -f docker/Dockerfile.py38 -t repofix-pytest:py38 .
docker build -f docker/Dockerfile.thefuck-py38 -t repofix-pytest:thefuck-py38 .

.\.venv\Scripts\python.exe -m repofix.cli check-task --task benchmarks/tasks/pagination-zero-page-size/task.yaml
.\.venv\Scripts\python.exe -m repofix.cli dashboard
```

看板默认地址为 `http://127.0.0.1:8765`。项目自带已构建的前端；修改 React 页面时才需要进入 `frontend/` 执行 `npm install` 和 `npm run dev` 或 `npm run build`。

先用不产生 API 费用的模拟回复检查修复链路：

```powershell
.\.venv\Scripts\python.exe -m repofix.cli repair `
  --task benchmarks/tasks/pagination-zero-page-size/task.yaml `
  --provider mock `
  --mock-response examples/pagination-locator-response.json `
  --mock-repair-response examples/pagination-repair-response.json
```

真实模型需要自行将 `.env.example` 复制为 `.env` 并填入 API Key，然后指定 `--provider api`。`.env`、运行记录和实验生成物均被忽略；请不要把密钥写进命令、源码或提交记录。

### 可选 LangGraph 引擎

安装可选依赖后，可用模拟回复免费试跑；`repair` 和 `experiment` 均接受 `--engine langgraph`，看板的运行表单也可选择执行引擎。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[graph]"
.\.venv\Scripts\python.exe -m repofix.cli repair `
  --task benchmarks/tasks/pagination-zero-page-size/task.yaml `
  --runner host --provider mock --engine langgraph --workflow dynamic `
  --mock-response examples/pagination-locator-response.json `
  --mock-repair-response examples/pagination-repair-sequence.json
```

图节点负责公开测试准备、诊断、候选生成、验证、条件调度与选择；隐藏验收仍在图外由独立评测器执行。实验文件记录 `execution_engine` 和 LangGraph 版本。当前未启用持久化 checkpoint，也未并行生成候选。上表的 2026-10-22 结果全部属于原有引擎，**不是 LangGraph 效果实验**；接入框架本身不代表通过率提升。

## 代码结构

- `src/repofix/analysis/`：AST 索引、回溯定位和调用上下文。
- `src/repofix/agents/`：单 Agent、定位 Agent、修复 Agent 及工作流；提示词在 `prompts/`。
- `src/repofix/repair/`：补丁生成、提交门槛与质量审查。
- `src/repofix/execution/`、`grader.py`：pytest 执行与独立验收。
- `frontend/`、`src/repofix/dashboard/`：本地交互界面与作业 API。
- `benchmarks/`：开发任务和 BugsInPy 真实缺陷任务；每题的 Agent 可见仓库、验收测试和参考材料分目录保存。

公开仓库中可以看到验收测试和参考材料，但运行时工作区不会把它们提供给 Agent。真实缺陷样本包含上游项目代码；相应的许可证文件与 `provenance.json` 随样本保留。

## 后续计划

在相同题集、模型、预算与固定依赖版本下，对原有引擎和 LangGraph 引擎进行正式对照；再评估持久化 checkpoint、恢复能力与调度开销。上述能力目前尚未计入已有实验成绩。
