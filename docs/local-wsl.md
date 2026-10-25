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

## 配置与选择模型

`.env.local.example` 设置 `PI_PROVIDER=local` 和一个 OpenAI 兼容 API 基址。当前本机 `.env` 使用 `http://127.0.0.1:8080/v1`，模型 ID 留空；生成时会重新读取 `/models`，只有一个 ID 时自动选择。若返回多个 ID，命令会列出选项并要求通过 `.env` 的 `PI_MODEL` 或本次 `--model` 明确选择。显式模型不需要先发现列表。`video-report models` 只读取并列出 `/models`，不发送对话请求。

服务地址、模型 ID、上下文窗口、输出上限和 reasoning 声明都由 `.env` 控制。当前 `.env` 中的上下文窗口 `131072`、输出上限 `32768` 来自 2026-10-03 对当前服务 `/props` 的只读检查（`default_generation_settings.n_ctx` 和 `params.n_predict`）；切换服务或模型后应按服务实际值手动更新，不会根据模型名称推断。`PI_API_KEY=local` 是当前无认证服务使用的占位值；需要认证时替换为服务密钥。Pi 的 `models.json` 仅更新 `local` provider，API key 保持 `$PI_API_KEY` 环境引用，现有其他 provider 与 `auth.json` 保留。生成的 `runs/<run-id>/input.json` 记录已解析的 provider/model，不记录 API key。

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

终态失败仍为 `FAILED`，用 `error_category` 区分 `INPUT_REJECTED`、`DOWNLOAD_FAILED`、`MODEL_MISSING`、`TRANSCRIPTION_FAILED`；`error_code` 给出具体原因。下载错误只根据 yt-dlp 输出识别登录、付费／会员、地区限制和平台风控／人机验证。B站 `Unable to extract initial state` 记录为可能的风控／验证相关页面解析失败；无法识别的输出保留为一般下载失败，不额外请求页面探测。

解决原因后重新运行命令即可重试，完整音频及兼容的 ASR／转写结果会复用。资源超限不会自动删除已有文件或改变七天媒体留存、转写文本保留策略；调整上限对新任务生效。

超限停止后遗留的媒体 `.part` 和 `.part-FragN` 分片临时文件，同样按终态任务的七天策略过期。未到期任务、字幕（含字幕半成品）、诊断文本及其他运行根目录不受这项清理影响。

## 英文视频实测

2026-10-19 使用原有 WSL2 环境，对 YouTube《Me at the zoo》（`jNQXAC9IVRw`，约 19 秒）执行了完整生成。配置为 faster-whisper `small`、CPU / INT8、`--asr-language en` 和当时运行的本地报告模型。

- 下载、音频提取、英文转写、报告生成及 PNG 导出全部完成，最终状态为 `RENDERED`，HTML 和 PNG 均已检查。
- 转写共 4 段，覆盖 0–19 秒；最终报告为中文。总耗时约 10 分 18 秒，其中 ASR 阶段约 11 秒，报告生成约 9 分 55 秒。这是单次耗时，不是性能基准。
- 对照 [V&A 逐字稿](https://www.vam.ac.uk/articles/transcript-me-at-the-zoo)，首句的 `in front of the … elephants` 被识别成 `one of the elephants`，报告随后写成“镜头对准其中一头大象”。流程完成不代表内容完全准确，其他语言和更大模型尚未在这次实测中验证。

本机原始产物保存在 `.local/evals/youtube-english-smoke/runs/da3740fe0abb44718218559ad85da35e/`，包含 `transcript.md`、`asr.json`、`report.html`、`report.png` 和阶段日志。该目录是本地忽略的实测数据，不随仓库分发；常规命令仍默认输出到 `runs/<run-id>/`。
