# dsh-gcua

GCUA (Generalist Computer-Use Agent) 状态机插件，为 [DeepSeek Harness](https://github.com/deepseek-ai) 而生。
移植自阿里通义 MAI-UI 团队的 [OpenGCUA](https://github.com/Tongyi-MAI/OpenGCUA) 的 harness 哲学：**决策下放给模型，引擎只管执行与记账**。

## 安全边界（必读）

步骤执行是**进程隔离**（每步一个新 Python worker 进程），**不是沙盒**：步骤代码以你自己的用户权限运行，能读写它能触达的一切。能做到的隔离只有两道：

- **环境变量白名单**：宿主只透传 `PATH/SYSTEMROOT/TEMP` 等系统级变量进入 worker——API key 类环境变量不进
- **单写者文件锁**：每目录同一时刻只有一个 worker 能写 `state.json`

凭据文件、浏览器 profile、SSH key——只要文件系统允许，步骤都能读。别把它跑在不想暴露这些的账号下。

## 它是什么

三个工具 + 一段系统提示词，把「观察 → 行动 → 验证」的闭环变成强制流程：

| 工具 | 干什么 |
|---|---|
| `gcua_step` | 把一段完整 Python 代码丢进隔离子进程跑一次（每步新进程；文件与系统副作用保留，变量不留）。批量查询、GUI 自动化、带分支的流水线——一次往返全收 |
| `gcua_status` | 读任务台账（todo ledger）与步级相位轨迹 |
| `gcua_reset` | 开新任务：清台账、stall 历史与 `.gcua/steps`；截图留存作为审计证据 |

Worker 命名空间（每步自动注入，无需 import）：

- `todo` — 台账：`todo.update(id, kind, content, status)`，字段省略即继承；kind ∈ `task|next_step|fact|question`，状态机 `pending → attempted → changed_unverified → claimed_verified`（或 `blocked`）。**规矩：没把条目从旧状态推进新状态，这一步就是白跑；只有拿到独立证据（读回验证、第二次观察）才许记"已验证"**
- `screenshot()` — 截图存 `.gcua/shot-NNN.png`，返回路径。单屏截主屏；多屏自动切全虚拟桌面（`monitors[0]`，与 pyautogui 绝对坐标一致）。只在定验时用，轮询靠文本
- `ps(cmd, timeout)` — PowerShell（UTF-8 输出；`-EncodedCommand` base64 编码装载，无引号转义/注入面）
- `paste_text(text)` — 剪贴板注入 + Ctrl+V，绕过中文 IME
- `pyautogui` / `uia`（pywinauto Desktop；未安装时首次访问抛 `ImportError` 并提示 `pip install pywinauto`，不是静默 `None`）/ 标准库
- `phase(name)` — 步内相位标记（execute / observe / verify…），自动记录进轨迹

内置防呆：**StallDetector**（失败步按同代码+同报错、成功步按同代码+同输出摘要指纹，连出 3 次警告、6 次熔断，窗口 12；熔断只提醒**绝不篡改**该步的真实成败）、**pid 感知文件锁**（接管需锁龄 >420s **且**持有进程已死，慢步骤永不被误判；BUSY 防并发）、**哨兵结果协议**（worker 结果行带 `__GCUA_RESULT__` 前缀，步骤代码往 fd-1 写原始字节也丢不了结果）、state.json 原子写 + 损坏自愈（隔离 `.json.bad`）、超时/中断 `taskkill /T /F` 斩杀整棵进程树并把该步记为 `timeout_unknown`——超时步 ≠ 失败 ≠ 没跑过：点击/写入等副作用可能已落地，`gcua_status` 显式计数，先核再重试；steps 超 200 条截断时留 evicted 计数，历史裁剪不撒谎、reset 语义三路统一（state + steps，截图留审计）、幂等 selftest 跑在临时目录。

## 安装

DSH 内：插件市场/`plugin_manager` 安装本目录；或手动把本目录作为 bundle 目标
`install_bundle target=<本仓库克隆路径>`（patch 已带 `cordis.patch.yml`）。

依赖：Windows + Python 3.10+（`python` 在 PATH）；GUI 车道需 `pip install pyautogui mss pywinauto`。

## 系统提示词注入

注册 `gcua-machine` section（order 850）：台账纪律、批量宏步、0.3–0.5s 文本轮询、截图只留验、UIA 矩形别肉眼估、`get_value()` 不可信用 `window_text()` 等——全部来自真机实战踩坑。

## 出处与致谢

- 设计原型：[OpenGCUA](https://github.com/Tongyi-MAI/OpenGCUA)（TodoLedger / StallDetector / `<intent>+<code>` 步级工作流），Apache-2.0；归属另见 [NOTICE](NOTICE)
- 本仓库实现：Windows/DSH 移植 + cordis 插件封装，Apache-2.0

## License

Apache-2.0
