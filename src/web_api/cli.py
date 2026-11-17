from __future__ import annotations

from ipaddress import ip_address
from os import environ
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from terminal_app.thinking import ThinkingLevel, normalize_thinking_level
from web_api.app import create_app
from web_api.runtime import WebRuntime, WebRuntimeConfig

cli_app = typer.Typer(add_completion=False, no_args_is_help=False)


def _is_loopback_host(host: str) -> bool:
    cleaned = host.strip().strip("[]").lower()
    if cleaned == "localhost":
        return True
    try:
        return ip_address(cleaned).is_loopback
    except ValueError:
        return False


def _split_csv_env(name: str) -> list[str]:
    raw = environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_allowed_origins(host: str, port: int, extra_origins: list[str] | None = None) -> list[str]:
    origins: list[str] = []
    if host in {"127.0.0.1", "localhost"}:
        origins.append(f"http://127.0.0.1:{port}")
        origins.append(f"http://localhost:{port}")
    elif ":" in host and not host.startswith("["):
        origins.append(f"http://[{host}]:{port}")
    else:
        origins.append(f"http://{host}:{port}")
    for item in _split_csv_env("OCTO_WEB_ALLOWED_ORIGINS"):
        if item not in origins:
            origins.append(item)
    for item in extra_origins or ():
        if item not in origins:
            origins.append(item)
    return origins


def build_trusted_proxies(extra_proxies: list[str] | None = None) -> list[str]:
    proxies = ["127.0.0.1", "::1"]
    for item in _split_csv_env("OCTO_WEB_TRUSTED_PROXIES"):
        if item not in proxies:
            proxies.append(item)
    for item in extra_proxies or ():
        if item not in proxies:
            proxies.append(item)
    return proxies


def default_static_dir() -> Path:
    env_dir = environ.get("OCTO_WEB_STATIC_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    return Path(__file__).resolve().parents[2] / "web" / "out"


@cli_app.command()
def serve(
    host: Annotated[str, typer.Option("--host", envvar="OCTO_WEB_HOST", help="监听地址")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", envvar="OCTO_WEB_PORT", help="监听端口")] = 18300,
    cwd: Annotated[Path | None, typer.Option("--cwd", envvar="OCTO_WEB_CWD", help="工具工作目录")] = None,
    session: Annotated[str | None, typer.Option("--session", envvar="OCTO_WEB_SESSION", help="启动时恢复的会话 ID")] = None,
    provider: Annotated[str | None, typer.Option("--provider", envvar="OCTO_WEB_PROVIDER", help="模型提供商")] = None,
    model: Annotated[str | None, typer.Option("--model", envvar="OCTO_WEB_MODEL", help="模型 ID")] = None,
    thinking: Annotated[str | None, typer.Option("--thinking", envvar="OCTO_WEB_THINKING", help="推理强度")] = None,
    static_dir: Annotated[Path | None, typer.Option("--static-dir", envvar="OCTO_WEB_STATIC_DIR", help="静态导出目录")] = None,
    allowed_origin: Annotated[list[str] | None, typer.Option("--allowed-origin", help="允许的外部 Origin，可重复指定")] = None,
    trusted_proxy: Annotated[list[str] | None, typer.Option("--trusted-proxy", help="可信代理 IP，可重复指定")] = None,
    graceful_timeout: Annotated[int, typer.Option("--graceful-timeout", envvar="OCTO_WEB_GRACEFUL_TIMEOUT", help="优雅停机超时秒数")] = 15,
    allow_non_loopback: Annotated[bool, typer.Option("--allow-non-loopback", envvar="OCTO_WEB_ALLOW_NON_LOOPBACK", help="允许绑定非回环地址（仅在独立隔离网络且上游代理已鉴权时使用）")] = False,
) -> None:
    if not _is_loopback_host(host):
        if not allow_non_loopback:
            typer.echo(
                "拒绝直接绑定非回环地址：应用层无内置 Basic Auth，请保持 --host 127.0.0.1 并经 Caddy 等反向代理认证；"
                "若处于受控隔离内网并确需放开，请显式传入 --allow-non-loopback。",
                err=True,
            )
            raise typer.Exit(2)
        typer.echo("警告：当前以非回环地址监听，请确保上游代理已启用 HTTPS 与访问认证。", err=True)
    resolved_cwd = (cwd or Path.cwd()).expanduser().resolve()
    if not resolved_cwd.is_dir():
        typer.echo(f"工作目录不存在: {resolved_cwd}", err=True)
        raise typer.Exit(1)
    resolved_static = (static_dir or default_static_dir()).expanduser().resolve()
    if not (resolved_static / "index.html").is_file():
        typer.echo(f"未找到前端静态页面 ({resolved_static / 'index.html'})，请先在 web 目录执行 npm run build", err=True)
        raise typer.Exit(1)

    thinking_override: ThinkingLevel | None = None
    if thinking is not None:
        try:
            thinking_override = normalize_thinking_level(thinking)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--thinking") from exc

    runtime_config = WebRuntimeConfig(
        cwd=resolved_cwd,
        session_id=session,
        provider_name=provider,
        model=model,
        thinking_level_override=thinking_override,
    )
    origins = build_allowed_origins(host, port, allowed_origin)
    proxies = build_trusted_proxies(trusted_proxy)
    fastapi_app = create_app(
        lambda: WebRuntime(runtime_config),
        static_dir=resolved_static,
        allowed_origins=origins,
        trusted_proxies=proxies,
    )
    uvicorn.run(
        fastapi_app,
        host=host,
        port=port,
        workers=1,
        reload=False,
        proxy_headers=False,
        timeout_graceful_shutdown=graceful_timeout,
    )


def main() -> None:
    cli_app()


if __name__ == "__main__":
    main()
