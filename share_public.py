#!/usr/bin/env python3
"""
在本机启动 FastAPI，并通过 Cloudflare Quick Tunnel 生成临时公网 HTTPS 链接，
复制给他人即可在浏览器打开使用（无需同一 WiFi）。

依赖：本机已安装 cloudflared（见 README）。
链接在关闭本脚本后失效；不适合作为长期正式站点。
"""
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

APP_DIR = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", "8000"))
HOST = (os.environ.get("HOST") or "0.0.0.0").strip() or "0.0.0.0"


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(APP_DIR / ".env", override=True)
    except ImportError:
        pass


def _find_cloudflared() -> Optional[str]:
    """排除占位脚本；优先 Homebrew 安装的正式二进制。"""
    candidates = [
        "/opt/homebrew/bin/cloudflared",
        "/usr/local/bin/cloudflared",
        shutil.which("cloudflared"),
    ]
    seen = set()
    for p in candidates:
        if not p or p in seen:
            continue
        seen.add(p)
        try:
            if os.path.isfile(p) and os.access(p, os.X_OK) and os.path.getsize(p) >= 50_000:
                return p
        except OSError:
            continue
    return None


def _port_available(port: int) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.close()
        return True
    except OSError:
        return False


def _wait_listen(timeout_s: float = 45.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.25)
            s.connect(("127.0.0.1", PORT))
            s.close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


def _try_pyngrok() -> Optional[str]:
    token = (os.environ.get("NGROK_AUTHTOKEN") or "").strip()
    if not token:
        return None
    try:
        from pyngrok import conf, ngrok  # type: ignore
    except ImportError:
        print("已设置 NGROK_AUTHTOKEN 但未安装 pyngrok，执行: pip install pyngrok", file=sys.stderr)
        return None
    conf.get_default().auth_token = token
    t = ngrok.connect(PORT, "http")
    return str(getattr(t, "public_url", "") or "")


def main() -> None:
    _load_dotenv()
    global PORT, HOST
    PORT = int(os.environ.get("PORT", str(PORT)))
    HOST = (os.environ.get("HOST") or HOST).strip() or "0.0.0.0"

    os.chdir(APP_DIR)

    if not _port_available(PORT):
        print(
            f"端口 {PORT} 已被占用（常见：本机已在跑 python app.py）。\n"
            f"请先结束占用进程，或换端口启动，例如：\n"
            f"  export PORT=8001\n"
            f"  python3 share_public.py\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # 可选：仅用 ngrok（需 pip install pyngrok + NGROK_AUTHTOKEN）
    use_ngrok_only = (os.environ.get("PUBLIC_USE_NGROK") or "").strip() == "1"

    uvicorn_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "app:app",
        "--host",
        HOST,
        "--port",
        str(PORT),
    ]
    print("正在启动服务…", flush=True)
    uv = subprocess.Popen(uvicorn_cmd, cwd=str(APP_DIR))
    tunnel_proc: Optional[subprocess.Popen] = None
    public_url: Optional[str] = None

    def cleanup(*_: object) -> None:
        nonlocal tunnel_proc
        if tunnel_proc and tunnel_proc.poll() is None:
            try:
                tunnel_proc.terminate()
                tunnel_proc.wait(timeout=5)
            except Exception:
                try:
                    tunnel_proc.kill()
                except Exception:
                    pass
            tunnel_proc = None
        try:
            uv.terminate()
            uv.wait(timeout=8)
        except Exception:
            try:
                uv.kill()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    if not _wait_listen():
        print(f"错误：服务未在 {PORT} 端口就绪。", file=sys.stderr)
        uv.terminate()
        sys.exit(1)

    want_ngrok = use_ngrok_only or bool((os.environ.get("NGROK_AUTHTOKEN") or "").strip())
    if want_ngrok:
        public_url = _try_pyngrok()
        if public_url:
            print("\n" + "=" * 62)
            print("  发给他人打开（ngrok 公网链接）：")
            print(" ", public_url)
            print("=" * 62 + "\n")
            print("按 Ctrl+C 结束服务与隧道。\n", flush=True)
            uv.wait()
            return
        if use_ngrok_only:
            print("未配置 NGROK_AUTHTOKEN 或 ngrok 启动失败，已退出。", file=sys.stderr)
            cleanup()

    cf = _find_cloudflared()
    if not cf:
        print(
            "\n未找到可用的 cloudflared 可执行文件（若 PATH 里只有几字节占位脚本也会被忽略）。\n"
            "请安装官方客户端，例如 macOS（Homebrew）：\n"
            "  brew install cloudflared\n"
            "安装后一般位于 /opt/homebrew/bin/cloudflared 。\n"
            "其它系统见：\n"
            "  https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/\n"
            "也可使用 ngrok: pip install pyngrok，在 .env 设置 NGROK_AUTHTOKEN 后运行本脚本。\n",
            file=sys.stderr,
        )
        print(f"当前仅本机/局域网: http://127.0.0.1:{PORT}\n", flush=True)
        uv.wait()
        return

    print(f"使用 cloudflared: {cf}", flush=True)
    print("正在创建公网隧道（Cloudflare Quick Tunnel）…\n", flush=True)
    proc = subprocess.Popen(
        [cf, "tunnel", "--url", f"http://127.0.0.1:{PORT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    tunnel_proc = proc

    url_re = re.compile(r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com/?")

    def reader() -> None:
        nonlocal public_url
        out = proc.stdout
        if out is None:
            return
        for line in iter(out.readline, ""):
            sys.stdout.write(line)
            sys.stdout.flush()
            if public_url:
                continue
            m = url_re.search(line)
            if m:
                public_url = m.group(0).rstrip("/")
                print("\n" + "=" * 62)
                print("  发给他人打开（临时公网链接，关闭本程序后失效）：")
                print(" ", public_url)
                print("=" * 62 + "\n", flush=True)

    threading.Thread(target=reader, daemon=True).start()

    code = proc.wait()
    if code and not public_url:
        print("cloudflared 已退出。若链接未出现，请检查网络或升级 cloudflared。", file=sys.stderr)
    cleanup()


if __name__ == "__main__":
    main()
