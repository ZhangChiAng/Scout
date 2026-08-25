import tempfile
import unittest
from pathlib import Path

from scout.config import (
    ConfigError,
    load_config,
    load_dotenv,
    resolve_feishu_delivery,
)


class ConfigTests(unittest.TestCase):
    def test_repository_config_loads(self) -> None:
        config = load_config(Path(__file__).parents[1] / "config.toml")
        self.assertEqual(len(config.sources), 1)
        source = config.sources[0]
        self.assertEqual(source.name, "juya-ai-daily")
        self.assertEqual(source.url, "https://daily.juya.uk/rss.xml")
        self.assertEqual(source.collector, "rss")
        self.assertEqual(source.window_size, 7)
        self.assertEqual(source.max_age_days, 3)
        self.assertEqual(config.network.timeout_seconds, 15)
        self.assertEqual(config.network.max_bytes, 5 * 1024 * 1024)
        self.assertEqual(config.network.user_agent, "Scout/0.1")
        self.assertEqual(config.feishu.max_payload_bytes, 28 * 1024)

    def test_source_arrays_validate_names_collector_and_numbers(self) -> None:
        repository = (Path(__file__).parents[1] / "config.toml").read_text(
            encoding="utf-8"
        )
        cases = {
            "duplicate": repository.replace(
                "max_age_days = 3",
                'max_age_days = 3\n\n[[sources]]\nname = "JUYA-AI-DAILY"\nurl = "https://daily.juya.uk/rss.xml"\ncollector = "rss"\nwindow_size = 7\nmax_age_days = 3',
                1,
            ),
            "collector": repository.replace(
                'collector = "rss"', 'collector = "llm_parser"', 1
            ),
            "empty name": repository.replace('name = "juya-ai-daily"', 'name = ""', 1),
            "bad url": repository.replace(
                'url = "https://daily.juya.uk/rss.xml"',
                'url = "ftp://daily.juya.uk/rss.xml"',
                1,
            ),
            "credential url": repository.replace(
                'url = "https://daily.juya.uk/rss.xml"',
                'url = "https://user:pass@daily.juya.uk/rss.xml"',
                1,
            ),
            "zero window": repository.replace("window_size = 7", "window_size = 0", 1),
            "zero max age": repository.replace(
                "max_age_days = 3", "max_age_days = 0", 1
            ),
            "non-integer max age": repository.replace(
                "max_age_days = 3", 'max_age_days = "3"', 1
            ),
            "unsupported field": repository.replace(
                "window_size = 7", 'window_size = 7\nallowed_hosts = ["example.com"]', 1
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for name, content in cases.items():
                with self.subTest(name=name):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_config(path)

    def test_missing_config_is_a_config_error(self) -> None:
        with self.assertRaises(ConfigError):
            load_config("definitely-missing.toml")

    def test_feishu_rejects_unknown_fields(self) -> None:
        repository_config = Path(__file__).parents[1] / "config.toml"
        content = repository_config.read_text(encoding="utf-8").replace(
            "max_payload_bytes = 28672",
            'max_payload_bytes = 1024\ntitle = 42\nsummary_max_chars = "unused"',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_legacy_source_table_and_filter_table_are_rejected(self) -> None:
        legacy_source = """[source]
name = "juya-ai-daily"
url = "https://daily.juya.uk/rss.xml"

[network]
timeout_seconds = 15
max_bytes = 1000
user_agent = "test"

[feishu]
max_payload_bytes = 1024
"""
        repository = Path(__file__).parents[1] / "config.toml"
        filter_table = (
            repository.read_text(encoding="utf-8")
            + """
[filter]
fields = ["title"]
keywords = ["model"]
"""
        )
        for name, content in (
            ("legacy source", legacy_source),
            ("filter", filter_table),
        ):
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "config.toml"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    load_config(path)

    def test_environment_file_does_not_override_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                'SCOUT_LLM_API_KEY="file-key"\nSCOUT_DB_PATH=data/test.sqlite3\n',
                encoding="utf-8",
            )
            environ = {"SCOUT_LLM_API_KEY": "process-key"}
            load_dotenv(path, environ=environ)
            self.assertEqual(environ["SCOUT_LLM_API_KEY"], "process-key")
            self.assertEqual(environ["SCOUT_DB_PATH"], "data/test.sqlite3")

    def test_repository_environment_example_is_loadable(self) -> None:
        environ: dict[str, str] = {}
        load_dotenv(Path(__file__).parents[1] / ".env.example", environ=environ)
        self.assertEqual(
            set(environ),
            {
                "SCOUT_LLM_API_KEY",
                "FEISHU_APP_ID",
                "FEISHU_APP_SECRET",
                "FEISHU_RECEIVE_ID_TYPE",
                "FEISHU_RECEIVE_ID",
                "SCOUT_DB_PATH",
            },
        )
        self.assertEqual(environ["SCOUT_DB_PATH"], "data/scout.sqlite3")

    def test_feishu_delivery_environment_loads_all_fields(self) -> None:
        environ = {
            "FEISHU_APP_ID": " cli_test ",
            "FEISHU_APP_SECRET": " app-secret ",
            "FEISHU_RECEIVE_ID_TYPE": " chat_id ",
            "FEISHU_RECEIVE_ID": " oc_test ",
        }
        delivery = resolve_feishu_delivery(environ=environ)
        self.assertEqual(delivery.app_id, "cli_test")
        self.assertEqual(delivery.app_secret, "app-secret")
        self.assertEqual(delivery.receive_id_type, "chat_id")
        self.assertEqual(delivery.receive_id, "oc_test")

    def test_feishu_delivery_reports_all_missing_fields_without_values(self) -> None:
        with self.assertRaises(ConfigError) as raised:
            resolve_feishu_delivery(environ={})
        message = str(raised.exception)
        for name in (
            "FEISHU_APP_ID",
            "FEISHU_APP_SECRET",
            "FEISHU_RECEIVE_ID_TYPE",
            "FEISHU_RECEIVE_ID",
        ):
            self.assertIn(name, message)

    def test_feishu_delivery_id_type_is_restricted_and_sanitized(self) -> None:
        allowed = ("chat_id", "open_id", "union_id", "user_id", "email")
        for receive_id_type in allowed:
            with self.subTest(receive_id_type=receive_id_type):
                delivery = resolve_feishu_delivery(
                    environ={
                        "FEISHU_APP_ID": "cli_test",
                        "FEISHU_APP_SECRET": "super-secret",
                        "FEISHU_RECEIVE_ID_TYPE": receive_id_type,
                        "FEISHU_RECEIVE_ID": "private-recipient",
                    }
                )
                self.assertEqual(delivery.receive_id_type, receive_id_type)

        with self.assertRaises(ConfigError) as raised:
            resolve_feishu_delivery(
                environ={
                    "FEISHU_APP_ID": "cli_test",
                    "FEISHU_APP_SECRET": "super-secret",
                    "FEISHU_RECEIVE_ID_TYPE": "secret-invalid-type",
                    "FEISHU_RECEIVE_ID": "private-recipient",
                }
            )
        message = str(raised.exception)
        self.assertNotIn("super-secret", message)
        self.assertNotIn("private-recipient", message)
        self.assertNotIn("secret-invalid-type", message)

    def test_feishu_payload_limit_accepts_30_kib_boundary(self) -> None:
        repository_config = Path(__file__).parents[1] / "config.toml"
        original = repository_config.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                original.replace(
                    "max_payload_bytes = 28672", "max_payload_bytes = 30720"
                ),
                encoding="utf-8",
            )
            self.assertEqual(load_config(path).feishu.max_payload_bytes, 30 * 1024)

            path.write_text(
                original.replace(
                    "max_payload_bytes = 28672", "max_payload_bytes = 30721"
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "30720"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
