"""TOML configuration loading and validation."""

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when the application configuration is invalid."""


@dataclass(frozen=True, slots=True)
class SourceConfig:
    name: str
    url: str
    window_size: int = 7
    collector: str = "rss"
    max_age_days: int | None = None

    def __post_init__(self) -> None:
        """Validate programmatic construction as strictly as TOML loading."""

        name = _nonempty_string(self.name, "sources.name")
        url = _source_url(self.url, "sources.url")
        window_size = _positive_int(self.window_size, "sources.window_size")
        collector = _enum_value(self.collector, "sources.collector", {"rss"})
        if self.max_age_days is not None:
            max_age_days = _positive_int(self.max_age_days, "sources.max_age_days")
            object.__setattr__(self, "max_age_days", max_age_days)

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "window_size", window_size)
        object.__setattr__(self, "collector", collector)


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    timeout_seconds: float
    max_bytes: int
    user_agent: str


@dataclass(frozen=True, slots=True)
class FeishuConfig:
    max_payload_bytes: int


@dataclass(frozen=True, slots=True)
class FeishuDeliveryConfig:
    app_id: str
    app_secret: str
    receive_id_type: str
    receive_id: str


@dataclass(frozen=True, slots=True)
class FeishuAppCredentials:
    app_id: str
    app_secret: str


@dataclass(frozen=True, slots=True, init=False)
class AppConfig:
    sources: tuple[SourceConfig, ...]
    network: NetworkConfig
    feishu: FeishuConfig

    def __init__(
        self,
        sources: tuple[SourceConfig, ...],
        network: NetworkConfig,
        feishu: FeishuConfig,
    ) -> None:
        """Build an application config from a non-empty source tuple."""

        if not isinstance(sources, tuple) or not sources:
            raise ConfigError("sources must contain at least one source")
        if not all(isinstance(entry, SourceConfig) for entry in sources):
            raise ConfigError("sources must contain only SourceConfig values")
        if network is None or feishu is None:
            raise ConfigError("network and feishu configurations are required")

        _validate_unique_source_names(sources)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "network", network)
        object.__setattr__(self, "feishu", feishu)


def load_config(path: str | Path = "config.toml") -> AppConfig:
    config_path = Path(path)
    try:
        with config_path.open("rb") as file:
            raw = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(
            f"cannot load config {config_path}: {type(exc).__name__}"
        ) from exc

    try:
        unsupported = set(raw) - {"sources", "network", "feishu"}
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ConfigError(f"config contains unsupported keys: {names}")
        network_raw = raw["network"]
        feishu_raw = raw["feishu"]
        sources = _load_sources(raw)
        network = NetworkConfig(
            timeout_seconds=_positive_number(
                network_raw["timeout_seconds"], "network.timeout_seconds"
            ),
            max_bytes=_positive_int(network_raw["max_bytes"], "network.max_bytes"),
            user_agent=_nonempty_string(
                network_raw["user_agent"], "network.user_agent"
            ),
        )
        if set(feishu_raw) != {"max_payload_bytes"}:
            raise ConfigError("feishu must contain only max_payload_bytes")
        feishu = FeishuConfig(
            max_payload_bytes=_positive_int(
                feishu_raw["max_payload_bytes"], "feishu.max_payload_bytes"
            ),
        )
    except KeyError as exc:
        raise ConfigError(f"missing config key: {exc.args[0]}") from exc
    except TypeError as exc:
        raise ConfigError(f"invalid config structure: {exc}") from exc

    if feishu.max_payload_bytes > 30 * 1024:
        raise ConfigError("feishu.max_payload_bytes must not exceed 30720")
    return AppConfig(sources, network, feishu)


def load_dotenv(
    path: str | Path = ".env",
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> None:
    """Load a small, non-interpolating dotenv file without overriding the process."""

    destination = os.environ if environ is None else environ
    env_path = Path(path)
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ConfigError(
            f"cannot load environment file: {type(exc).__name__}"
        ) from exc

    for line_number, original in enumerate(lines, start=1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or not key.isidentifier():
            raise ConfigError(f"invalid environment entry on line {line_number}")
        if key in destination:
            continue
        destination[key] = _dotenv_value(raw_value.strip(), line_number)


def resolve_feishu_delivery(
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> FeishuDeliveryConfig:
    source = os.environ if environ is None else environ
    names = (
        "FEISHU_APP_ID",
        "FEISHU_APP_SECRET",
        "FEISHU_RECEIVE_ID_TYPE",
        "FEISHU_RECEIVE_ID",
    )
    values = {name: source.get(name, "").strip() for name in names}
    missing = [name for name in names if not values[name]]
    if missing:
        raise ConfigError(
            f"missing required Feishu environment variables: {', '.join(missing)}"
        )

    allowed_id_types = {"chat_id", "open_id", "union_id", "user_id", "email"}
    receive_id_type = values["FEISHU_RECEIVE_ID_TYPE"]
    if receive_id_type not in allowed_id_types:
        allowed = ", ".join(sorted(allowed_id_types))
        raise ConfigError(f"FEISHU_RECEIVE_ID_TYPE must be one of: {allowed}")

    return FeishuDeliveryConfig(
        app_id=values["FEISHU_APP_ID"],
        app_secret=values["FEISHU_APP_SECRET"],
        receive_id_type=receive_id_type,
        receive_id=values["FEISHU_RECEIVE_ID"],
    )


def resolve_feishu_app_credentials(
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> FeishuAppCredentials:
    """Resolve only the credentials required by the feedback listener."""

    source = os.environ if environ is None else environ
    app_id = source.get("FEISHU_APP_ID", "").strip()
    app_secret = source.get("FEISHU_APP_SECRET", "").strip()
    missing = [
        name
        for name, value in (
            ("FEISHU_APP_ID", app_id),
            ("FEISHU_APP_SECRET", app_secret),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            f"missing required Feishu environment variables: {', '.join(missing)}"
        )
    return FeishuAppCredentials(app_id=app_id, app_secret=app_secret)


def _load_sources(raw: dict[str, object]) -> tuple[SourceConfig, ...]:
    if "source" in raw:
        raise ConfigError(
            "the legacy [source] table is no longer supported; use [[sources]]"
        )
    if "sources" not in raw:
        raise ConfigError("missing config key: sources")

    source_array = raw["sources"]
    if not isinstance(source_array, list) or not source_array:
        raise ConfigError("sources must be a non-empty array of tables")
    sources: list[SourceConfig] = []
    required = {"name", "url", "collector", "window_size"}
    supported = required | {"max_age_days"}
    for index, source_raw in enumerate(source_array, start=1):
        prefix = f"sources[{index}]"
        if not isinstance(source_raw, dict):
            raise ConfigError(f"{prefix} must be a table")
        missing = required - set(source_raw)
        if missing:
            names = ", ".join(sorted(missing))
            raise ConfigError(f"{prefix} is missing required fields: {names}")
        unsupported = set(source_raw) - supported
        if unsupported:
            names = ", ".join(sorted(unsupported))
            raise ConfigError(f"{prefix} contains unsupported fields: {names}")
        sources.append(
            SourceConfig(
                name=_nonempty_string(source_raw["name"], f"{prefix}.name"),
                url=_source_url(source_raw["url"], f"{prefix}.url"),
                window_size=_positive_int(
                    source_raw["window_size"], f"{prefix}.window_size"
                ),
                collector=_enum_value(
                    source_raw["collector"],
                    f"{prefix}.collector",
                    {"rss"},
                ),
                max_age_days=(
                    _positive_int(source_raw["max_age_days"], f"{prefix}.max_age_days")
                    if "max_age_days" in source_raw
                    else None
                ),
            )
        )
    result = tuple(sources)
    _validate_unique_source_names(result)
    return result


def _validate_unique_source_names(sources: tuple[SourceConfig, ...]) -> None:
    seen: set[str] = set()
    for source in sources:
        normalized = source.name.casefold()
        if normalized in seen:
            raise ConfigError(f"source names must be unique: {source.name}")
        seen.add(normalized)


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _http_url(value: object, name: str) -> str:
    text = _nonempty_string(value, name)
    if not text.startswith(("https://", "http://")):
        raise ConfigError(f"{name} must be an HTTP(S) URL")
    return text


def _source_url(value: object, name: str) -> str:
    text = _http_url(value, name)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{name} is not a valid URL") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        and not 1 <= port <= 65535
    ):
        raise ConfigError(f"{name} must contain a host and no credentials")
    return text


def _safe_endpoint(value: object, name: str) -> str:
    text = _http_url(value, name)
    parsed = urlsplit(text)
    if not parsed.hostname or parsed.username or parsed.password:
        raise ConfigError(f"{name} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"{name} must not contain a query or fragment")
    return text.rstrip("/")


def _dotenv_value(value: str, line_number: int) -> str:
    if not value:
        return ""
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigError(
                f"invalid quoted environment value on line {line_number}"
            ) from exc
        if not isinstance(parsed, str):
            raise ConfigError(f"invalid environment value on line {line_number}")
        return parsed
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ConfigError(f"invalid quoted environment value on line {line_number}")
        return value[1:-1]
    return value.split(" #", 1)[0].rstrip()


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer")
    return value


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigError(f"{name} must be a positive number")
    return float(value)


def _enum_value(value: object, name: str, allowed: set[str]) -> str:
    text = _nonempty_string(value, name)
    if text not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ConfigError(f"{name} must be one of: {choices}")
    return text
