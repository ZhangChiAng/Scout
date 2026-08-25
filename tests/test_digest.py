import unittest

from scout.digest import (
    OverviewEntry,
    RelatedLink,
    Section,
    parse_issue,
)

ISSUE_HTML = """<p><img src="https://daily.juya.uk/assets/cover.png" /></p>
<h1>AI 早报 2026-08-24</h1>
<p><a href="https://www.bilibili.com/video/BV1xx411c7mD">视频版</a></p>
<h2>概览</h2>
<h3>要闻</h3>
<ul>
<li>DeepSeek 发布新模型 <a href="https://example.com/news/1">↗</a> <code>#1</code></li>
<li><a href="https://example.com/news/2?utm_source=daily">OpenAI 更新 API</a> <code>#2</code></li>
</ul>
<h3>开发生态</h3>
<ul>
<li>某个框架发布新版 <a href="https://example.com/news/3">↗</a></li>
<li>没有链接的标题</li>
</ul>
<hr>
<h2>正文</h2>
<h3><a href="https://example.com/news/1">DeepSeek 发布新模型</a></h3>
<blockquote>DeepSeek 发布了新一代推理模型。</blockquote>
<p>详情段落：性能提升明显，接口保持不变。</p>
<p><img src="https://example.com/img.png" /></p>
<ul>
<li><a href="https://example.com/paper">论文</a></li>
<li><a href="https://github.com/example/repo">代码仓库</a></li>
</ul>
<h3>OpenAI 更新 API</h3>
<blockquote>OpenAI 更新了 API 接口。</blockquote>
<p><a href="https://example.com/news/2">查看网页全文</a></p>
"""


class DigestTests(unittest.TestCase):
    def test_parses_issue_date_from_h1(self) -> None:
        issue = parse_issue(ISSUE_HTML)
        self.assertEqual(issue.issue_date, "2026-08-24")

    def test_parses_overview_categories_links_and_numbers(self) -> None:
        issue = parse_issue(ISSUE_HTML)
        self.assertEqual(
            issue.overview,
            (
                OverviewEntry(
                    category="要闻",
                    headline="DeepSeek 发布新模型",
                    url="https://example.com/news/1",
                    number="1",
                ),
                OverviewEntry(
                    category="要闻",
                    headline="OpenAI 更新 API",
                    url="https://example.com/news/2?utm_source=daily",
                    number="2",
                ),
                OverviewEntry(
                    category="开发生态",
                    headline="某个框架发布新版",
                    url="https://example.com/news/3",
                    number="",
                ),
                OverviewEntry(
                    category="开发生态",
                    headline="没有链接的标题",
                    url="",
                    number="",
                ),
            ),
        )

    def test_parses_sections_with_summary_detail_and_related_links(self) -> None:
        issue = parse_issue(ISSUE_HTML)
        self.assertEqual(
            issue.sections,
            (
                Section(
                    title="DeepSeek 发布新模型",
                    url="https://example.com/news/1",
                    summary="DeepSeek 发布了新一代推理模型。",
                    detail="详情段落：性能提升明显，接口保持不变。",
                    related_links=(
                        RelatedLink("论文", "https://example.com/paper"),
                        RelatedLink("代码仓库", "https://github.com/example/repo"),
                    ),
                ),
                Section(
                    title="OpenAI 更新 API",
                    url="",
                    summary="OpenAI 更新了 API 接口。",
                    detail="",
                    related_links=(),
                ),
            ),
        )

    def test_trailing_full_page_link_populates_page_url(self) -> None:
        issue = parse_issue(ISSUE_HTML)
        self.assertEqual(issue.page_url, "https://example.com/news/2")

    def test_explicit_page_url_overrides_trailing_link(self) -> None:
        issue = parse_issue(
            ISSUE_HTML, page_url="https://daily.juya.uk/issues/2026-08-24/"
        )
        self.assertEqual(issue.page_url, "https://daily.juya.uk/issues/2026-08-24/")

    def test_cover_image_and_video_link_are_not_articles(self) -> None:
        issue = parse_issue(ISSUE_HTML)
        headlines = [entry.headline for entry in issue.overview]
        self.assertNotIn("视频版", headlines)
        self.assertNotIn("↗", headlines)
        self.assertNotIn("AI 早报 2026-08-24", headlines)
        self.assertTrue(all("#" not in entry.headline for entry in issue.overview))

    def test_bad_html_is_tolerated(self) -> None:
        truncated = ISSUE_HTML[: ISSUE_HTML.index("<h3>开发生态</h3>")]
        issue = parse_issue(truncated)
        self.assertEqual(issue.issue_date, "2026-08-24")
        self.assertEqual(len(issue.overview), 2)
        self.assertEqual(issue.sections, ())

    def test_empty_html_yields_empty_digest(self) -> None:
        issue = parse_issue("")
        self.assertEqual(issue.issue_date, "")
        self.assertEqual(issue.overview, ())
        self.assertEqual(issue.sections, ())
        self.assertEqual(issue.page_url, "")

    def test_malformed_tags_still_yield_headlines(self) -> None:
        broken = (
            "<h1>AI 早报 2026-08-25</h1><h2>概览</h2><h3>要闻</h3>"
            "<ul><li>未闭合标题 <a href='https://example.com/x'>↗</a></ul>"
        )
        issue = parse_issue(broken)
        self.assertEqual(issue.issue_date, "2026-08-25")
        self.assertEqual(
            issue.overview,
            (OverviewEntry("要闻", "未闭合标题", "https://example.com/x", ""),),
        )


if __name__ == "__main__":
    unittest.main()
