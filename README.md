# 关键对话学员场景教练（MVP）

一个本地可运行的小系统：学员用 **STAR**（Situation/Task/Action/Result）在 1–5 分钟内填写困难沟通场景，系统即时按《关键对话》框架做诊断、给可执行话术与行动计划，并提供一个可选的 AI 对话区用于追问与演练。

## 运行方式

1) 安装依赖

```bash
cd "/Users/tal/Desktop/关键对话学员反馈"
pip3 install -r requirements.txt
```

2) 启动服务

```bash
python3 app.py
```

3) 打开浏览器

- **本机**：`http://127.0.0.1:8000`
- **同一 WiFi / 局域网其他人**：先看你电脑的局域网 IP（如 macOS「系统设置 → 网络」里类似 `192.168.x.x`），对方浏览器打开 `http://192.168.x.x:8000`。若仍打不开，检查本机防火墙是否放行该端口。

默认服务监听 `0.0.0.0`（局域网可访问）。若只想本机能打开，启动前设置环境变量 `HOST=127.0.0.1`。

### 公网链接（发给他人直接点开，不必同一 WiFi）

用 **Cloudflare Quick Tunnel** 在本机开一个临时 HTTPS 地址（关闭脚本后链接失效，适合演示/试用）。

1. 安装 **真正的** `cloudflared`（需为可执行二进制，不能是几字节的占位文件）  
   - macOS（Homebrew）：`brew install cloudflared`（一般在 `/opt/homebrew/bin/cloudflared`）  
   - 其他系统：见 [Cloudflare 安装文档](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/)

2. 若本机 **8000 端口已被占用**（例如已运行 `python app.py`），先关掉该进程，或换端口：

```bash
export PORT=8001
python3 share_public.py
```

3. 在项目目录执行：

```bash
python3 share_public.py
```

4. 终端里会出现一行 `https://xxxx.trycloudflare.com`，**复制发给对方**即可在浏览器打开。

5. 按 **Ctrl+C** 结束服务与隧道。

**可选（ngrok）**：`pip install pyngrok`，在 `.env` 中设置 `NGROK_AUTHTOKEN`（[ngrok 控制台](https://dashboard.ngrok.com/) 获取），再运行 `share_public.py` 会优先建立 ngrok 隧道。

**说明**：公网隧道依赖第三方网络；国内访问 Cloudflare/ngrok 可能不稳定。下面「云部署」可得到**长期固定**的 `https://…` 地址。

### 长期固定网址（云部署，推荐）

真实域名只能由云平台在**你完成部署后**分配，无法在未部署时凭空生成。本项目已带 `Dockerfile`，可一键上到 **Render** 等托管，获得类似 **`https://你的服务名.onrender.com`** 的固定链接（免费档可能休眠，久未访问首次打开需等待唤醒）。

1. 把代码推到 **GitHub**（勿提交 `.env` / 密钥）。
2. 打开 [render.com](https://render.com) → **New** → **Web Service** → 选仓库。
3. **Environment** 选 **Docker**（使用根目录 `Dockerfile`）。
4. 在 **Environment Variables** 至少配置（与本地 `.env` 一致）：
   - `DOUBAO_API_KEY`
   - `DOUBAO_BASE_URL`（如 `https://ark.cn-beijing.volces.com/api/v3`）
   - `DOUBAO_MODEL`（如 `ep-xxxx`）
5. 部署完成后，控制台里的 **URL** 即为可长期发给他人使用的地址。

也可使用仓库内 `render.yaml` 作为 Blueprint 模板（按需修改 `name`）。**Railway / Fly.io / 国内云托管** 同理：用 Docker 构建，启动命令等价于 `uvicorn app:app --host 0.0.0.0 --port $PORT`，并注入 `PORT` 与上述变量。

**说明**：当前任务状态在**进程内存**中，实例重启会清空；单实例即可。绑定自己的域名可在各平台 **Custom Domain** 里设置。

## 可选：接入 AI 模型（OpenAI 兼容）

不配置也能用（会返回 mock 讨论/建议）。

```bash
export OPENAI_API_KEY="你的key"
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_MODEL="gpt-4.1-mini"
python3 app.py
```

说明：
- `OPENAI_BASE_URL` 支持任意 OpenAI 兼容网关/私有部署地址
- 前端调用的是本地后端 `/api/chat`，避免浏览器 CORS 限制

**生成偏慢 / 长时间无结果**：多半是上游模型排队或单次生成耗时长。已在后端把单次 HTTP 读超时默认调到约 **120s**（见 `.env.example` 中 `AI_READ_TIMEOUT`）。若仍超时，可把 `AI_READ_TIMEOUT` 提到 `180`；经公网隧道访问时建议保持 `DOUBAO_STREAM=0`（默认）。

## 设计原则（问卷 1–5 分钟）

- 必填只保留：对象类型、沟通目标、担心风险、STAR 四段
- 其余问题均为可选补充，用于提高分析质量

配置 API：复制 `.env.example` 为 `.env` 并填写（勿将密钥提交到仓库）。
