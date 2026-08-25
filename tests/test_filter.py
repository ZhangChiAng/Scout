import unittest

from scout.filter import KeywordFilter
from tests.helpers import news_item


class KeywordFilterTests(unittest.TestCase):
    def test_matches_selected_title_and_content_case_insensitively(self) -> None:
        keyword_filter = KeywordFilter(("GPT", "API"), ("title", "content"))
        self.assertTrue(
            keyword_filter.matches(news_item(title="New gPt model", content=""))
        )
        self.assertTrue(
            keyword_filter.matches(news_item(title="Other", content="An api update"))
        )
        self.assertFalse(
            keyword_filter.matches(news_item(title="Other", content="Nothing relevant"))
        )

    def test_short_keywords_use_ascii_boundaries(self) -> None:
        keyword_filter = KeywordFilter(("API", "IDE", "AI"), ("title",))
        self.assertFalse(
            keyword_filter.matches(news_item(title="rapid evidence chair"))
        )
        self.assertTrue(keyword_filter.matches(news_item(title="API, IDE and AI")))
        self.assertTrue(keyword_filter.matches(news_item(title="发布API能力")))

    def test_only_configured_fields_are_searched(self) -> None:
        keyword_filter = KeywordFilter(("API",), ("title",))
        self.assertFalse(
            keyword_filter.matches(news_item(title="Other", content="API"))
        )

    def test_configured_vendor_and_bilingual_topic_keywords_match(self) -> None:
        keywords = (
            "Claude",
            "Anthropic",
            "DeepSeek",
            "Kimi",
            "GLM",
            "MiniMax",
            "model",
            "agent",
            "reasoning",
            "context",
            "模型",
            "智能体",
            "推理",
            "上下文",
        )

        keyword_filter = KeywordFilter(keywords, ("title", "content"))
        matching_text = (
            "Claude gets a platform update",
            "Anthropic publishes an engineering note",
            "DeepSeek updates its API",
            "Kimi ships a research preview",
            "GLM adds tool use",
            "MiniMax announces a release",
            "A new foundation model is available",
            "An autonomous agent runtime",
            "Better reasoning at inference time",
            "A longer context window",
            "发布新的基础模型",
            "智能体编排能力升级",
            "提升复杂推理效率",
            "扩展长上下文窗口",
        )
        for text in matching_text:
            with self.subTest(text=text):
                self.assertTrue(keyword_filter.matches(news_item(title=text)))


if __name__ == "__main__":
    unittest.main()
