import tempfile
import unittest
from pathlib import Path

from scout.config import ConfigError
from scout.llm import (
    MODEL_TIMEOUT_SECONDS,
    LLMError,
    ModelConfig,
    build_client,
    load_models_config,
    load_optional_model_config,
    resolve_api_key,
)

VALID_MODELS = """[[models]]
model = "example-responses-model"
protocol = "openai_responses"
base_url = "https://api.example.com/v1"
api_key_env = "SCOUT_LLM_API_KEY"
"""


class LLMTests(unittest.TestCase):
    def test_example_model_config_is_strict_and_loadable(self) -> None:
        config = load_models_config(Path(__file__).parents[1] / "models.example.toml")
        self.assertEqual(config.protocol, "openai_responses")
        self.assertEqual(config.api_key_env, "SCOUT_LLM_API_KEY")
        self.assertEqual(config.base_url, "https://api.example.com/v1")

    def test_model_config_requires_one_model_and_exact_fields(self) -> None:
        cases = [
            "models = []\n",
            """[[models]]
model = "one"
protocol = "openai_responses"
base_url = "https://api.example.com/v1"
api_key_env = "SCOUT_LLM_API_KEY"
extra = true
""",
            """[[models]]
model = "one"
protocol = "chat_completions"
base_url = "https://api.example.com/v1"
api_key_env = "SCOUT_LLM_API_KEY"
""",
            """[[models]]
model = "one"
protocol = "openai_responses"
base_url = "https://api.example.com/v1"
api_key_env = "SIGNALFEED_LLM_API_KEY"
""",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.toml"
            for content in cases:
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ConfigError):
                        load_models_config(path)

    def test_model_config_errors_do_not_echo_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.toml"
            path.write_text(
                """[[models]]
model = "one"
protocol = "openai_responses"
base_url = "https://user:super-secret@example.com/v1"
api_key_env = "SCOUT_LLM_API_KEY"
""",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError) as raised:
                load_models_config(path)
            self.assertNotIn("super-secret", str(raised.exception))

    def test_build_client_passes_fixed_settings(self) -> None:
        seen: dict[str, object] = {}

        def factory(**kwargs: object) -> object:
            seen.update(kwargs)
            return object()

        config = load_models_config(Path(__file__).parents[1] / "models.example.toml")
        client = build_client(config, "test-key", client_factory=factory)

        self.assertIsNotNone(client)
        self.assertEqual(seen["api_key"], "test-key")
        self.assertEqual(seen["base_url"], "https://api.example.com/v1")
        self.assertEqual(seen["timeout"], MODEL_TIMEOUT_SECONDS)
        self.assertEqual(seen["max_retries"], 0)

    def test_build_client_failure_does_not_echo_key(self) -> None:
        def factory(**kwargs: object) -> object:
            raise RuntimeError("super-secret-key-must-not-leak")

        config = ModelConfig(
            "model",
            "openai_responses",
            "https://api.example.com/v1",
            "SCOUT_LLM_API_KEY",
        )
        with self.assertRaises(LLMError) as raised:
            build_client(
                config, "super-secret-key-must-not-leak", client_factory=factory
            )
        self.assertNotIn("super-secret-key-must-not-leak", str(raised.exception))

    def test_optional_loading_skips_when_file_or_key_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.toml"
            self.assertIsNone(
                load_optional_model_config(
                    missing, environ={"SCOUT_LLM_API_KEY": "present"}
                )
            )
            path = Path(directory) / "models.toml"
            path.write_text(VALID_MODELS, encoding="utf-8")
            self.assertIsNone(
                load_optional_model_config(path, environ={"OTHER_KEY": "x"})
            )

    def test_optional_loading_validates_when_both_are_present(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.toml"
            path.write_text(VALID_MODELS, encoding="utf-8")
            config = load_optional_model_config(
                path, environ={"SCOUT_LLM_API_KEY": "sk-test"}
            )
            self.assertIsNotNone(config)
            assert config is not None
            self.assertEqual(config.api_key_env, "SCOUT_LLM_API_KEY")

            path.write_text("models = []\n", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_optional_model_config(
                    path, environ={"SCOUT_LLM_API_KEY": "sk-test"}
                )

    def test_resolve_api_key_requires_present_value(self) -> None:
        config = ModelConfig(
            "model",
            "openai_responses",
            "https://api.example.com/v1",
            "SCOUT_LLM_API_KEY",
        )
        self.assertEqual(
            resolve_api_key(config, environ={"SCOUT_LLM_API_KEY": "sk-value"}),
            "sk-value",
        )
        with self.assertRaisesRegex(ConfigError, "SCOUT_LLM_API_KEY"):
            resolve_api_key(config, environ={})


if __name__ == "__main__":
    unittest.main()
