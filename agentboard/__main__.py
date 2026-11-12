"""python -m agentboard"""
import argparse
from pathlib import Path
import signal
import sys
import threading
import webbrowser

from .server import make_server
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Agent Board · 本地 Markdown 多 Agent 协作台，无第三方依赖")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=".agentboard")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--lease-seconds", type=int, default=120)
    parser.add_argument("--open", action="store_true", help="在默认浏览器打开并自动登录")
    parser.add_argument("--print-login-url", action="store_true", help="打印带管理令牌的本地登录链接（请勿分享）")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535 or args.lease_seconds < 10:
        parser.error("port 必须在0..65535，lease-seconds 至少10秒")
    store = server = None
    try:
        store = Store(args.data_dir, args.workspace, args.lease_seconds)
        server = make_server(args.host, args.port, store)
        host = f"[{args.host}]" if ":" in args.host else args.host
        url = f"http://{host}:{server.server_port}/"
        print(f"Agent Board 已启动：{url}", flush=True)
        print(f"数据目录：{Path(args.data_dir).resolve()}", flush=True)
        print("首次登录请从 admin.token 复制管理令牌；或使用 --open 自动登录。Ctrl+C 停止。", flush=True)
        login_url = url + "#token=" + store.admin_token
        if args.print_login_url:
            print(login_url, flush=True)
        if args.open:
            webbrowser.open(login_url)
        def stop(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        server.serve_forever(poll_interval=1.0)
        return 0
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    finally:
        if server:
            server.server_close()
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
