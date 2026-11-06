# Windows 本地部署（WSL2）

Windows 通过 WSL2 Ubuntu 运行本项目 CLI，保留上游使用的 Linux 进程监督路径。此入口支持生成报告，不提供 Web UI；Windows 原生 Python 运行尚未验证。命令默认针对 `D:\Project\video-report-agent` 对应的 WSL 路径 `/mnt/d/Project/video-report-agent`。

## 安装

需要已启用 WSL2，并安装 `Ubuntu-26.04`。Linux 依赖与缓存放在项目 `.tooling/wsl`，Python 环境单独使用 `.venv-wsl`；现有 Windows `.venv` 和 `.tooling` 不参与 WSL 运行。

在 Ubuntu 中从项目根目录安装项目内工具。uv 固定为 0.12.22，Node.js 固定为 24.14.0；uv 管理 Python 3.12，Pi 固定为本地 npm 包 0.85.0。setup 还会安装 Deno 2.9.8，供 YouTube 的 JavaScript 挑战使用。FFmpeg/FFprobe 使用 WSL Ubuntu 软件包：

```sh
cd /mnt/d/Project/video-report-agent
TOOLS="$PWD/.tooling/wsl"
VENV="$PWD/.venv-wsl"
mkdir -p "$TOOLS/bin" "$TOOLS/node" "$TOOLS/python" "$TOOLS/cache"
curl -LsSf https://astral.sh/uv/0.12.22/install.sh | env UV_INSTALL_DIR="$TOOLS/bin" UV_NO_MODIFY_PATH=1 sh
export UV_PYTHON_INSTALL_DIR="$TOOLS/python"
export UV_CACHE_DIR="$TOOLS/cache/uv"
"$TOOLS/bin/uv" python install 3.12
curl -fL https://nodejs.org/dist/v24.14.0/node-v24.14.0-linux-x64.tar.xz | tar -xJf - -C "$TOOLS/node" --strip-components=1
sudo apt-get update
sudo apt-get install -y ffmpeg
```

Node.js is downloaded from its version-pinned official distribution URL. To verify its checksum before extraction, download `https://nodejs.org/dist/v24.14.0/SHASUMS256.txt` and check the `node-v24.14.0-linux-x64.tar.xz` entry with `sha256sum -c`.

首次使用时，保留已有 `.env`；仅当文件不存在时，从本地配置样例创建：

```sh
test -f .env || cp .env.local.example .env
bash scripts/setup-wsl.sh
sudo "$VENV/bin/python" -m playwright install-deps chromium
```

安装脚本会在 `.venv-wsl` 安装锁定的 Python 依赖和 `local-asr` extra，保留环境内其他已装包；在 `.tooling/wsl/pi` 安装 Pi 0.85.0，在项目缓存下载 Playwright Chromium，并预下载配置的 faster-whisper 权重。`playwright install-deps chromium` 以管理员权限安装 Chromium 所需的系统共享库和字体。使用字幕/OCR 的 `fused` 模式前，运行 `VIDEO_REPORT_WITH_ENHANCEMENT=1 bash scripts/setup-wsl.sh` 安装额外依赖。

### YouTube JavaScript 依赖

项目将 yt-dlp 2026.10.21 与其 default extra 指定的 `yt-dlp-ejs==0.8.0` 配对锁定，只直接安装所需 EJS 包，避免引入整个 default extra。更新 yt-dlp 时一并核对其 PyPI 元数据中的 EJS 版本并更新锁文件。EJS 的 core/lib 解题脚本随 Python 包显式安装，运行时不启用 `--remote-components ejs:github/npm`。

`setup-wsl.sh` 在 Python 环境就绪后调用 `scripts/install-deno.sh`。该步骤从 [Deno 官方 v2.9.8 发布](https://github.com/denoland/deno/releases/tag/v2.9.8)下载 Linux x86_64／aarch64 压缩包和对应 `.sha256sum`，用 `sha256sum --check` 校验后只提取 `deno`，核对版本，再安装到 `.tooling/wsl/bin/deno`。已有相同版本时跳过；失败时保留原可执行文件。此步骤不需要 sudo，不写系统目录。`run-wsl.sh` 将该目录加入 PATH，并关闭 Deno 的更新检查。

已有环境只补装 YouTube 依赖时，可在项目根目录执行以下步骤，保留现有 extra 和已装包；这组命令不调用 ASR 模型预下载脚本：

```sh
export UV_PROJECT_ENVIRONMENT="$PWD/.venv-wsl"
export UV_PYTHON_INSTALL_DIR="$PWD/.tooling/wsl/python"
export UV_CACHE_DIR="$PWD/.tooling/wsl/cache/uv"
.tooling/wsl/bin/uv sync --locked --inexact --extra local-asr
bash scripts/install-deno.sh
bash scripts/run-wsl.sh doctor
```

YouTube 元数据检查可使用 `PATH="$PWD/.tooling/wsl/bin:$PATH" .venv-wsl/bin/yt-dlp -v --ignore-config --skip-download --no-playlist --list-formats "https://www.youtube.com/watch?v=jNQXAC9IVRw"`，确认日志识别到 Deno、EJS 并列出音频格式；该命令不下载媒体。

### B 站页面解析兼容

项目保持 `yt-dlp==2026.10.21` 与 EJS 的锁定配对，并在项目 Worker 内安装局部 Bilibili 兼容补丁。网页省略 `__INITIAL_STATE__` 时，补丁使用 yt-dlp 已有的 WBI 签名能力读取平台详情接口，校验视频身份及分 P 数据后沿用原格式、字幕和权限处理。明确的页面风控挑战仍失败；接口拒绝、登录、会员及地区限制不会被忽略。元数据预查询与正式下载使用相同入口，不需要浏览器 Cookie、临时 `PYTHONPATH` 或另装 fork。

实现参考 [yt-dlp PR #17898](https://github.com/yt-dlp/yt-dlp/pull/17898) 的详情回退思路（Unlicense，参考修订 `e88263195a928487cd1802f1a626931f738db5e3`）。2026-10-27 核对时该 PR 尚未合并，官方 2026.10.26 仍有相同缺失状态报错，故没有仅为此问题升级依赖。将来升级 yt-dlp 时，应先验证缺失页面状态、正常页面、指定分 P 与访问错误，再移除已由上游覆盖的兼容补丁。直接运行环境内的 `yt-dlp` 不会加载项目补丁；通过 `video-report.cmd`、`scripts/run-wsl.sh` 或项目 `probe_bilibili_video` 使用该修复。

2026-10-27 使用 `BV1MrnR6xEus`（1741.264 秒）核验了项目元数据预查询、真实下载及 `video-report.cmd transcribe` 到 `TRANSCRIPT_READY` 的 HIP 转写，生成 858 个片段。首次下载因线路较慢达到原有 120 秒预算；保留分片经项目下载入口续传并验证完整后，最终 CLI 复用已完成媒体，重新执行 ASR。此次验证未使用临时 fork、`PYTHONPATH` 或修改下载预算，因此不证明该线路下从零下载能在 120 秒内完成。首次运行以 `FAILED` 结束；音频已删除，验证临时材料仍待清理，文字稿保留。

## 配置与选择模型

`.env.local.example` 设置 `PI_PROVIDER=local` 和一个 OpenAI 兼容 API 基址。当前本机 `.env` 使用 `http://127.0.0.1:8080/v1`，模型 ID 留空；生成时会重新读取 `/models`，只有一个 ID 时自动选择。若返回多个 ID，命令会列出选项并要求通过 `.env` 的 `PI_MODEL` 或本次 `--model` 明确选择。显式模型不需要先发现列表。`video-report models` 只读取并列出 `/models`，不发送对话请求。

服务地址、模型 ID、上下文窗口、输出上限和 reasoning 声明都由 `.env` 控制。当前 `.env` 中的上下文窗口 `131072`、输出上限 `32768` 来自 2026-10-03 对当前服务 `/props` 的只读检查（`default_generation_settings.n_ctx` 和 `params.n_predict`）；切换服务或模型后应按服务实际值手动更新，不会根据模型名称推断。`PI_API_KEY=local` 是当前无认证服务使用的占位值；需要认证时替换为服务密钥。Pi 的 `models.json` 仅更新 `local` provider，API key 保持 `$PI_API_KEY` 环境引用，现有其他 provider 与 `auth.json` 保留。生成的 `runs/<run-id>/input.json` 记录已解析的 provider/model，不记录 API key。

单次生成可用 `--provider` 和 `--thinking` 改用 Pi 中其他已配置的 provider（`config/pi/models.json` 中的 provider，或 Pi 内置 provider）：

```powershell
.\video-report.cmd generate "https://www.bilibili.com/video/BV..." --provider zhipu --model "模型 ID" --thinking high
```

`--provider` 覆盖本次的 `PI_PROVIDER`，并忽略 `PI_MODEL`（它属于环境里的 provider）；非 `local` provider 必须同时给 `--model`，`--provider local` 未给模型时仍按上文读取 `/models`。`--thinking` 取 Pi 支持的 `off`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`，未指定时为 `low`。两项都不给时行为不变。选择写入 `input.json` 的 `model_selection`，由 `PiRunner` 转为 Pi 的 `--provider`、`--model`、`--thinking`。凭证仍由该 provider 的 Pi 配置提供：`PI_API_KEY` 只发给 `PI_PROVIDER` 指定的 provider，其他 provider 需要 Pi `auth.json` 或 `models.json` 中的环境引用。`transcribe` 接受同样的参数但不使用。

## AMD GPU 转写（HIP）

`whisper-cpp` 使用 WSL 内的 ROCm/HIP 和本地 ggml 权重。需要已有可用的 AMD WSL 驱动、ROCm、`git`、CMake、Make 与 C/C++ 编译工具；`rocminfo` 应列出目标 GPU。安装脚本不安装系统驱动、不使用 Windows 原生 TheRock，也不需要报告模型服务。

在 WSL 的项目根目录执行：

```sh
# 默认编译 RX 7900 XTX 的 gfx1100；其他 GPU 按实际架构设置 AMDGPU_TARGETS。
bash scripts/setup-whisper-cpp-hip.sh
bash scripts/run-wsl.sh transcribe 'https://www.youtube.com/watch?v=VIDEO_ID' \
  --asr-backend whisper-cpp --asr-model large-v3-turbo --asr-language zh
```

Windows 可使用相同参数：

```powershell
.\video-report.cmd transcribe "https://www.youtube.com/watch?v=VIDEO_ID" --asr-backend whisper-cpp --asr-model large-v3-turbo --asr-language zh
```

安装固定 [whisper.cpp v1.9.5](https://github.com/ggml-org/whisper.cpp/tree/v1.9.5)（MIT）源码到 `.tooling/wsl/whisper.cpp`，默认 `BUILD_JOBS=4`，通过 `GGML_HIP=ON` 编译 `whisper-cli`；WSL 构建关闭 HIP VMM，使用常规 HIP 内存分配。`ROCM_PATH` 可选择 ROCm 根目录（默认 `/opt/rocm`）。large-v3-turbo 权重约 1.6 GB，来自官方项目引用的 [ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp)，固定仓库修订，下载到临时文件后才替换目标。已有源码修订不符或被修改时，脚本报错并保留原内容。

`large-v3-turbo` 对应 `models/whisper-cpp/ggml-large-v3-turbo.bin`。其他模型需预先准备兼容的 ggml 文件，并通过 `--asr-model /绝对路径/ggml-model.bin` 选择；相对路径按项目根目录解析。`WHISPER_CPP_BINARY` 可指定其他兼容 HIP `whisper-cli`，相对路径同样按项目根目录解析。运行阶段不下载权重；仍会读取 `ASR_MODEL`，因此临时切换后端时应显式传 `--asr-model`，避免沿用 CPU 后端的 `small`。

后端启用 GPU 0 和 Flash Attention，要求日志确认 `ROCm0` GPU 后端初始化，CPU 回退会报错。原始诊断保存在每次运行目录的 `whisper-cpp.log`；`asr.json` 保留解析结果与规范片段，时间戳相对完整音频。`transcribe` 成功后状态为 `TRANSCRIPT_READY`，可读取 `transcript.md`，不调用报告模型。现有 30 分钟任务期限与取消机制同时终止 HIP 子进程；直接调用底层 Python ASR 接口时，由调用方负责其运行期限。

whisper.cpp turbo 偶尔在段正文中输出不完整 UTF-8 字节（[上游问题 #3760](https://github.com/ggml-org/whisper.cpp/issues/3760)）。适配器只在段 `text` 内将非法字节序列替换成可见的 `�`，不猜测缺失文字；`asr.json` 的 `raw_result.utf8_repairs` 记录原始段号、时间范围、正文字符位置、原始 JSON 字节范围及十六进制字节。位置从零开始，范围末端不包含在内。完整原始输出另存 `whisper-cpp.raw.json`，解析失败时也保留；`raw_result` 为解析后的内容，已处理的正文包含同样的 `�` 标记。发生替换时，原始字节还以 `raw_output_base64` 嵌入 ASR 文档，随 ASR 和完整转写缓存持久保存；复用时恢复同名 raw 文件，不依赖生产运行目录仍然存在。取消清理保留已完成 ASR 对应的 raw 文件与日志。未发生替换时不嵌入原始字节，raw 文件仅作原运行目录的诊断材料。字段名、元数据、JSON 结构或不成对 Unicode 转义损坏仍报错。

可执行文件与权重的路径、大小和修改时间保存在运行参数中，并进入 ASR/文字稿缓存身份；创建任务后文件变化时，推理会报错，需重新创建任务。模型应保存在不可变路径，不要在保留大小及修改时间的情况下原地替换内容。修改二进制依赖库或 ROCm 版本后需重建 CLI，避免沿用旧缓存身份。`doctor` 只检查本地文件，不证明 GPU 推理可用。

本地已验证 Ubuntu-26.04 WSL、ROCm 10.1、RX 7900 XTX（gfx1100）与上述固定 whisper.cpp/turbo 模型的真实 GPU 转写，并核对 GPU 权重分配和时间戳；缓存复用及异常字节保留由相关回归测试覆盖。这不是 Windows 原生 HIP 验证。文字稿仍为机器识别，专名和交易术语需按用途校对。六条 YouTube 验证记录位于忽略的 `.local/evals/hip-transcripts-20261026/`，音频已按保留要求删除，转写与诊断记录继续保留。

## 日常命令

使用本地报告模型时，先启动 `PI_BASE_URL` 指向的模型服务。在 Windows PowerShell 中进入项目目录执行；如果项目放在其他位置，请替换路径：

```powershell
cd D:\Project\video-report-agent
.\video-report.cmd --help
.\video-report.cmd models
.\video-report.cmd doctor

# 中文 B 站视频
.\video-report.cmd generate "https://www.bilibili.com/video/BV..." --asr-backend faster-whisper --asr-language zh

# 英文 YouTube 视频
.\video-report.cmd generate "https://www.youtube.com/watch?v=jNQXAC9IVRw" --asr-backend faster-whisper --asr-language en
```

也可从任意目录使用完整路径，例如 `& "D:\Project\video-report-agent\video-report.cmd" --help`。脚本会进入项目并使用 Ubuntu-26.04，将 `.venv-wsl/bin` 加入子进程 `PATH`。每次 `generate` 处理一个视频，无需单独启动 Web UI。生成命令可追加 `--model "实际模型 ID"` 选择报告模型，或追加 `--report-mode brief` 生成精简速览；默认模式是 `standard`。

WSL 内也可在任意目录运行 `bash /mnt/d/Project/video-report-agent/scripts/run-wsl.sh doctor`。默认产物在 `runs/<run-id>/`，包括 `transcript.md`、来源记录、`report.html` 和可能生成的 `report.png`。

`doctor` 检查 Python、uv、Node/npm、Pi、yt-dlp、FFmpeg/FFprobe、Playwright Chromium 启动、本地 `/models` 可用性及本地 ASR 权重，不发送模型生成请求。它还离线检查 Deno 最低版本、EJS 版本与当前 yt-dlp 的配对要求，以及本地 core/lib 脚本是否可读且非空。启用 faster-whisper 时，它还会将一个临时合成短 WAV 交给 faster-whisper 的真实音频解码器，验证 PyAV API 兼容性；这一步不加载模型权重，也不做语音识别。`local-asr` extra 将 PyAV 限定在 `>=11,<19`：faster-whisper 1.2.1 仍会向 `av.open` 传 `metadata_errors`，而 PyAV 19 已删除此参数。ASR 缓存检查只读取当前 faster-whisper 版本解析到的 main revision，并确认 `config.json`、`model.bin` 和 `tokenizer.json` 均非空；不会实例化模型或联网下载。若当前 provider 不是 `local`，本地模型发现会标记为跳过。报告生成仍需用户自行判断内容准确性；启动检查和 mock 服务验证不代表真实 LLM 报告质量。

### 用订阅 CLI 生成报告

已完成转写的任务可以不经 Pi，直接用已登录的 Claude Code、Codex 或 OpenCode 生成报告，消耗各自的订阅或套餐额度，不需要 API key。在 Windows PowerShell 中用项目 `.venv` 运行：

```powershell
.\.venv\Scripts\python.exe scripts\cli_agent_report.py <run-id 或 run 目录> --backend claude
.\.venv\Scripts\python.exe scripts\cli_agent_report.py <run-id> --backend codex
.\.venv\Scripts\python.exe scripts\cli_agent_report.py <run-id> --backend opencode
.\.venv\Scripts\python.exe scripts\cli_agent_report.py <run-id> --backend claude --model opus --effort high
```

不传 `--model`、`--effort` 时按后端取默认值：Claude 为 `sonnet` / `medium`，Codex 为 `gpt-6.1-sol` / `low`，OpenCode 为 `opencode-go/deepseek-v4.1-flash` / `max`；显式传入的值优先。`--effort` 对 Claude 是 `--effort`，对 Codex 是 `model_reasoning_effort`，对 OpenCode 是模型 variant（即 `-m provider/model#variant`）。OpenCode 的 variant 只能用一种写法给出：`--model` 已带 `#variant` 时不能再传 `--effort`，也不会追加默认 variant。`manifest.json` 和输出目录名记录实际生效的模型与强度。`--timeout` 默认 1800 秒，`--out` 可指定新的输出目录。

脚本不修改 `runs/` 下的文件。默认输出在 `.local/evals/cli-agent-reports/<run-id>/<backend>-<model>-<effort>-<时间>/`：`workspace/` 是 CLI 的工作目录，内容与关闭独立图示、术语修正和画面辅助理解时的 Pi 隔离模式相同（转写、精简后的 `input.json`、简介元数据，以及只含本次模式文件和模板的 Skill），提示词与附加系统提示也由 `pi.py` 中 Pi 使用的同一函数按这些功能关闭生成；`cli.stdout.log`、`cli.stderr.log` 保存 CLI 原始输出；`manifest.json` 记录 backend、模型、强度、实际命令行、起止时间、耗时、退出码和检查结果。

各 CLI 的限制方式：

- Claude Code：`-p --safe-mode --restricted`，不加载 CLAUDE.md、Skills、插件、hooks 和 MCP；只开放 Read、Write、Edit、Bash。Read、Write、Edit 限定在工作目录，`acceptEdits` 自动接受工作目录内的编辑。Bash 没有沙箱，并通过 `--allowedTools Bash` 自动放行，可以读写工作目录以外的文件，只由附加系统提示约束；这与 Pi local 隔离模式的边界相同。Claude Code 还会把较大的工具输出写到 `~/.claude/projects/` 下。
- Codex：`exec --ignore-user-config -s workspace-write -C <workspace> --ephemeral`，附加系统提示经 `developer_instructions` 传入，`project_doc_max_bytes=0` 不加载仓库 AGENTS.md。用户全局 `~/.codex/AGENTS.md` 是否仍被加载未核实。
- OpenCode：`run --standalone --auto`，用私有 `XDG_CONFIG_HOME` 替换用户全局配置，附加系统提示作为 instructions 文件，`external_directory` 权限设为拒绝，并禁用项目配置与 Claude Code 兼容加载。OpenCode 自己的临时目录（`%TEMP%\opencode`）不受此限制，实测中模型在那里写过浏览器截图。

子进程环境会移除所有以 `_API_KEY` 结尾的变量，以及 `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL`、`OPENAI_BASE_URL`、`CLAUDE_CODE_USE_BEDROCK`、`CLAUDE_CODE_USE_VERTEX` 等会把 CLI 切到 API 计费或其他端点的变量。按 Ctrl-C 中断时，脚本先结束 CLI 进程树，再写入 `status` 为 `interrupted` 的 `manifest.json`。CLI 退出码非 0、超时、没有 `report.html`、HTML 不完整、`{{VIDEO_DESCRIPTION}}` 不是恰好一次、没有 `unit-NNNNNN` 引用或引用了 `transcript.md` 中不存在的 ID 时，脚本以非零退出并保留目录；通过后按原任务的 `download/source.info.json` 填入视频简介。

这条路线绕过 Pi：没有 `agent_settled` / `stopReason` 完成判定、`pi.events.jsonl`、模型恢复和用量统计，也不写回任务状态或数据库，只用于本地评测和生成。独立图示、术语修正和画面辅助理解依赖 Pi 扩展工具或视觉预处理结果，原任务开启了这些功能时，本路线仍按关闭处理，暂存的 `input.json` 也记为关闭。

提示词允许在只有 Bash 时运行 `"$VIDEO_REPORT_PYTHON" -m video_report_agent.inspect_report` 检查报告；脚本把该变量设为运行脚本的 Python，并为子进程设置 `PYTHONUTF8=1`，避免检查器在 Windows 默认代码页下读取中文转写失败。检查器截图依赖 Playwright Chromium；项目 Windows `.venv` 未安装浏览器时，检查器返回 `status: error` 而不产生截图，可在 `.venv` 中运行 `playwright install chromium` 补装。检查只覆盖结构与来源 ID 是否存在，不评价内容是否忠实于原文。

## 本地语音识别

默认使用 faster-whisper `small`，固定运行在 CPU/int8，不占用报告模型所用 GPU。模型 ID 可设为 faster-whisper 支持的模型名，也可指向本地 CTranslate2 模型目录；权重默认缓存在 `.tooling/wsl/cache/asr`。`generate` 和 `transcribe` 仅使用本地权重（`local_files_only=True`），不会隐式下载模型。修改 `.env` 的 `ASR_MODEL` 后，显式运行 `wsl.exe -d Ubuntu-26.04 --exec /mnt/d/Project/video-report-agent/.venv-wsl/bin/python /mnt/d/Project/video-report-agent/scripts/cache_faster_whisper_model.py` 下载；首次安装也可运行 `scripts/setup-wsl.sh`。权重缺失时返回 `ASR_MODEL_MISSING`，已有完整音频可在重试时复用。下载音频、生成转写和报告仍顺序执行；字幕文件是 `fused` 模式的融合输入，不会绕过 ASR。

中文和英文使用同一套 faster-whisper 多语言模型。语言选择优先级为：本次 `--asr-language` → 环境变量 / `.env` 的 `ASR_LANGUAGE` → 默认 `zh`。faster-whisper 和 MLX 适配器支持 `auto`（`--asr-language auto` 或 `.env` 的 `ASR_LANGUAGE=auto`），转写时自动检测语言；paraformer 仍只接受 `zh`。语言参数不控制报告输出语言。

单次尝试更大的转写模型可追加 `--asr-model medium` 或 `--asr-model large-v3`，需先显式缓存相应权重，不会修改默认配置；`--model` 选择的是报告模型。更大的模型通常需要更多内存和 CPU 时间，不保证每句话都正确。本次英文实测仅使用 `small`。

### 只转写单条视频

```powershell
.\video-report.cmd transcribe "https://www.youtube.com/watch?v=jNQXAC9IVRw" --asr-backend faster-whisper --asr-language auto
```

此命令不调用报告模型。`asr.json` 保留原始时间戳、模型、检测语言和识别结果；`canonical-transcript.jsonl` 保存规范时间单元；`transcript.md` 提供时间戳正文、模型、语言及文字稿状态；`status.json` 保存整体处理状态和失败原因。请求语言 `auto` 与实际检测语言分别保留在输入配置和 ASR 产物中。

构造 faster-whisper 引擎前，先以仅本地方式解析模型目录，并按 doctor 相同的标准检查非空 `config.json`、`model.bin` 和 `tokenizer.json`。本地目录或缓存快照缺少任一文件时返回 `ASR_MODEL_MISSING`，不进入引擎或 tokenizer 的远程获取回退；本地自定义目录需补齐这些文件，或改用已显式缓存的完整模型。

faster-whisper 默认启用 VAD（`ASR_VAD_FILTER=true`），过滤无语音片段，减少背景音乐或静音导致的幻觉。VAD 不能保证识别完全准确。该布尔值保存在 `input.json` 的 `asr_parameters.vad_filter` 中，参与 ASR／完整转写缓存身份；设置 `false` 会使用不同缓存。

### 资源上限与失败重试

两份配置示例均提供以下正整数选项，零和负数无效：

| 配置 | 默认值 | 含义 |
|---|---|---|
| `MEDIA_MAX_DURATION_SECONDS` | `18000` | 单条视频最多 5 小时 |
| `MEDIA_MAX_FILE_BYTES` | `1073741824` | 单个下载文件最多 1 GiB |
| `MEDIA_MAX_DISK_BYTES` | `10737418240` | 所选运行根目录内常规文件逻辑字节总量最多 10 GiB |

上限在建任务时保存到 `input.json.media_limits`。磁盘统计不跟随符号链接，硬链接按文件身份去重；默认的 `.tooling` 模型缓存位于运行根目录之外，不计入。下载前使用时长、可用空间和已知文件大小限制；下载中约每秒检查一次实际文件及运行目录用量，转换音频前预估 PCM 空间，转换后再次检查。它是应用层检查，下载缓冲或转换估算偏差可能暂时超过阈值，不是操作系统硬配额。小型状态／诊断记录仍会写入，已完成的转写可直接复用。

终态失败仍为 `FAILED`，用 `error_category` 区分 `INPUT_REJECTED`、`DOWNLOAD_FAILED`、`MODEL_MISSING`、`TRANSCRIPTION_FAILED`；`error_code` 给出具体原因。下载错误只根据 yt-dlp 输出识别登录、付费／会员、地区限制和平台风控／人机验证。B站缺少页面状态先按上述兼容路径处理；仍出现 `Unable to extract initial state` 时记录为可能的风控／验证相关页面解析失败。无法识别的输出保留为一般下载失败，不在失败分类时额外请求页面探测。

解决原因后重新运行命令即可重试，完整音频及兼容的 ASR／转写结果会复用。资源超限不会自动删除已有文件或改变七天媒体留存、转写文本保留策略；调整上限对新任务生效。

超限停止后遗留的媒体 `.part` 和 `.part-FragN` 分片临时文件，同样按终态任务的七天策略过期。未到期任务、字幕（含字幕半成品）、诊断文本及其他运行根目录不受这项清理影响。

## 英文视频实测

2026-10-19 使用原有 WSL2 环境，对 YouTube《Me at the zoo》（`jNQXAC9IVRw`，约 19 秒）执行了完整生成。配置为 faster-whisper `small`、CPU / INT8、`--asr-language en` 和当时运行的本地报告模型。

- 下载、音频提取、英文转写、报告生成及 PNG 导出全部完成，最终状态为 `RENDERED`，HTML 和 PNG 均已检查。
- 转写共 4 段，覆盖 0–19 秒；最终报告为中文。总耗时约 10 分 18 秒，其中 ASR 阶段约 11 秒，报告生成约 9 分 55 秒。这是单次耗时，不是性能基准。
- 对照 [V&A 逐字稿](https://www.vam.ac.uk/articles/transcript-me-at-the-zoo)，首句的 `in front of the … elephants` 被识别成 `one of the elephants`，报告随后写成“镜头对准其中一头大象”。流程完成不代表内容完全准确，其他语言和更大模型尚未在这次实测中验证。

本机原始产物保存在 `.local/evals/youtube-english-smoke/runs/da3740fe0abb44718218559ad85da35e/`，包含 `transcript.md`、`asr.json`、`report.html`、`report.png` 和阶段日志。该目录是本地忽略的实测数据，不随仓库分发；常规命令仍默认输出到 `runs/<run-id>/`。
