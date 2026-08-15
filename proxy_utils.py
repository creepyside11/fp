from urllib.parse import quote, unquote, urlsplit, urlunsplit

SUPPORTED_SCHEMES = {"http", "https", "socks4", "socks5", "socks5h"}


class ProxyFormatError(ValueError):
    pass


def normalize_proxy(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ProxyFormatError("Прокси не может быть пустым.")
    if len(raw) > 2048:
        raise ProxyFormatError("Прокси слишком длинный.")

    if "://" not in raw:
        parts = raw.split(":")
        if len(parts) == 4:
            host, port, username, password = parts
            raw = f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{port}"
        elif "@" in raw:
            raw = "http://" + raw
        else:
            if len(parts) == 2:
                raw = f"http://{parts[0]}:{parts[1]}"
            else:
                raise ProxyFormatError(
                    "Неизвестный формат прокси. Используйте host:port или host:port:user:password."
                )

    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as error:
        raise ProxyFormatError("В прокси указан некорректный порт.") from error

    if parsed.scheme.lower() not in SUPPORTED_SCHEMES:
        raise ProxyFormatError("Поддерживаются HTTP, HTTPS, SOCKS4 и SOCKS5 прокси.")
    if not parsed.hostname or port is None:
        raise ProxyFormatError("В прокси должны быть указаны хост и порт.")
    if not 1 <= port <= 65535:
        raise ProxyFormatError("Порт прокси должен быть от 1 до 65535.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ProxyFormatError(
            "Прокси не должен содержать путь, параметры или фрагмент."
        )

    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    auth = ""
    if parsed.username is not None:
        auth = quote(unquote(parsed.username), safe="")
        if parsed.password is not None:
            auth += ":" + quote(unquote(parsed.password), safe="")
        auth += "@"
    netloc = f"{auth}{hostname}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


def proxy_mapping(proxy_url: str) -> dict[str, str]:
    return {"http": proxy_url, "https": proxy_url}
