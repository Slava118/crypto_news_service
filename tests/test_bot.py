import datetime as dt
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from newsbot.__main__ import daily_count, publish, run
from newsbot.editor import Story, evaluate, validate
from newsbot.feeds import Article, Feed, canonical_url, parse_feed
from newsbot.history import load_state, parse_preview, publication_day, verify_channel_head
from newsbot.http import HTTPStatusError, ServiceError
from newsbot.telegram import Telegram


NOW = dt.datetime(2026, 11, 22, 15, tzinfo=dt.UTC)
ARTICLES = [
    Article("Publisher A", "Ethereum activates a major network upgrade",
            "Ethereum activates a major network upgrade after months of testing.",
            "https://example.net/primo?utm_medium=rss", NOW.isoformat()),
    Article("Publisher B", "Ethereum rolls out protocol upgrade",
            "Ethereum rolls out protocol upgrade with changes to validator operations.",
            "https://example.org/secondo", NOW.isoformat()),
]
ITEM = {"score": 8, "title": "Ethereum activates its latest protocol upgrade",
        "summary": "Ethereum has activated a major upgrade to its protocol.",
        "sentiment": 4, "impact": "long_game", "topics": ["tech", "adoption"],
        "evidence": [{"article": 0, "quote": "activates a major network upgrade"},
                     {"article": 1, "quote": "rolls out protocol upgrade"}]}
STORY = Story(8, ITEM["title"], ITEM["summary"], tuple(ARTICLES), 4, "long_game",
              ("tech", "adoption"))
OLD = {"id": 50, "day": "2026-11-21", "title": "Previous news",
       "summary": "An earlier report.", "urls": ["https://example.net/precedente"]}


class EditorTests(unittest.TestCase):
    def test_accepts_and_sorts_only_qualified_stories(self):
        result = {"stories": [dict(ITEM, score=7), ITEM, dict(ITEM, score=9)]}
        accepted = validate(result, ARTICLES, [])
        self.assertEqual([story.score for story in accepted], [9])

    def test_all_sentiment_levels_can_qualify_at_the_same_importance(self):
        for sentiment, label in enumerate(
            ["STRONGLY NEGATIVE", "NEGATIVE", "NEUTRAL", "POSITIVE", "STRONGLY POSITIVE"],
            start=1,
        ):
            with self.subTest(sentiment=sentiment):
                [story] = validate({"stories": [dict(ITEM, sentiment=sentiment)]}, ARTICLES, [])
                self.assertEqual(story.score, 8)
                self.assertEqual(story.record(1, "2026-11-22")["sentiment"], sentiment)
                self.assertEqual(story.classification_lines()[0].split(" ", 2)[2], label)

    def test_rejects_missing_or_invalid_classification(self):
        missing = dict(ITEM)
        del missing["sentiment"]
        invalid = [missing, dict(ITEM, sentiment=0), dict(ITEM, sentiment=6),
                   dict(ITEM, sentiment=True), dict(ITEM, impact="urgent"),
                   dict(ITEM, topics=[]), dict(ITEM, topics=["fake"]),
                   dict(ITEM, topics=["tech", "tech"])]
        self.assertEqual(validate({"stories": invalid}, ARTICLES, []), [])

    def test_figures_require_both_feeds_and_allow_equivalent_notation(self):
        sources = [
            Article("Publisher A", ARTICLES[0].title, "Bitcoin rallied above $61K after falling 7%.",
                    ARTICLES[0].url, NOW.isoformat()),
            Article("Publisher B", ARTICLES[1].title, "The rebound took bitcoin to $61,000.",
                    ARTICLES[1].url, NOW.isoformat()),
        ]
        story = dict(ITEM, title="Bitcoin rebounds to $61,000",
                     summary="Bitcoin recovered to $61K after a decline.",
                     evidence=[{"article": 0, "quote": "bitcoin rallied above $61k"},
                               {"article": 1, "quote": "the rebound took bitcoin to $61,000"}])
        self.assertEqual(len(validate({"stories": [story]}, sources, [])), 1)
        for number in ("7%", "$55,500", "61,000%"):
            with self.subTest(number=number):
                unsupported = dict(story, summary=f"Bitcoin recovered after a decline of {number}.")
                self.assertEqual(validate({"stories": [unsupported]}, sources, []), [])

    def test_rejects_unknown_fabricated_or_single_publisher_evidence(self):
        invalid = [dict(ITEM, evidence=[ITEM["evidence"][0], extra]) for extra in (
            {"article": 4, "quote": "rolls out protocol upgrade"},
            {"article": 1, "quote": "this quote was wholly fabricated"},
            {"article": 0, "quote": "activates a major network upgrade"},
        )]
        self.assertEqual(validate({"stories": invalid}, ARTICLES, []), [])

    def test_rejects_duplicate_across_runs_by_url_or_title(self):
        for old in (dict(OLD, urls=["https://example.net/primo"]),
                    dict(OLD, title=ITEM["title"])):
            self.assertEqual(validate({"stories": [ITEM]}, ARTICLES, [old]), [])

    def test_rejects_event_rewrite_with_different_urls_and_date(self):
        sources = [
            Article("Publisher A", "Solana validators restart the network",
                    "Validators restart the network following a five-hour outage.",
                    "https://example.net/restart", NOW.isoformat()),
            Article("Publisher B", "Solana returns after its outage",
                    "Solana returns after its outage as validators resume block production.",
                    "https://example.org/outage", NOW.isoformat()),
        ]
        repeated = dict(
            ITEM, title="Solana validators restart the network after a five-hour outage",
            summary="Solana validators restarted the network and resumed block production "
                    "after a five-hour outage interrupted transaction processing.",
            evidence=[{"article": 0, "quote": "restart the network following a five-hour outage"},
                      {"article": 1, "quote": "solana returns after its outage"}],
        )
        previous = dict(
            OLD, title="Solana network restarts after a five-hour outage",
            summary="After a five-hour outage interrupted transaction processing on the Solana "
                    "network, validators restarted the chain and resumed block production.",
        )
        result = {"stories": [repeated]}
        self.assertEqual(len(validate(result, sources, [])), 1)
        self.assertEqual(validate(result, sources, [previous]), [])

        unrelated = dict(previous, title="Solana expands its validator documentation",
                         summary="The foundation published new hardware and networking guidelines.")
        self.assertEqual(len(validate(result, sources, [unrelated])), 1)

    def test_rejects_essentially_identical_coverage(self):
        copied = Article("Publisher B", ARTICLES[1].title, ARTICLES[0].description,
                         ARTICLES[1].url, NOW.isoformat())
        self.assertEqual(validate({"stories": [ITEM]}, [ARTICLES[0], copied], []), [])

    @patch("newsbot.editor.time.sleep")
    @patch("newsbot.editor.request_json")
    def test_retries_gemini_503_without_retrying_quota_errors(self, request, sleep):
        response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [
            {"text": json.dumps({"stories": [ITEM]})},
        ]}}]}
        request.side_effect = [HTTPStatusError("Gemini", 503, ""), response]
        self.assertEqual(evaluate(ARTICLES, [], "placeholder"), [STORY])
        sleep.assert_called_once_with(2)
        request.reset_mock(side_effect=True)
        request.side_effect = HTTPStatusError("Gemini", 429, "")
        with self.assertRaisesRegex(HTTPStatusError, "429"):
            evaluate(ARTICLES, [], "placeholder")
        request.assert_called_once()


class FeedAndHistoryTests(unittest.TestCase):
    def test_rss_filters_stale_and_other_domains(self):
        def item(url, date):
            return (f"<item><title>Headline</title><description>Summary</description>"
                    f"<link>{url}</link><pubDate>{date}</pubDate></item>")

        current = "Sun, 22 Nov 2026 14:00:00 GMT"
        data = ("<rss><channel>" + item("https://example.net/news", current)
                + item("https://example.org/news", current)
                + item("https://example.net/old", "Thu, 19 Nov 2026 18:00:00 GMT")
                + "</channel></rss>")
        result = parse_feed(data, Feed("Publisher A", "", "example.net"), NOW)
        self.assertEqual([article.url for article in result], ["https://example.net/news"])
        self.assertEqual(canonical_url("http://www.example.net/news/?utm_source=x&b=1#top"),
                         "https://example.net/news?b=1")

    def test_moscow_calendar_rolls_over_at_21_utc(self):
        for month in (7, 11):
            with self.subTest(month=month):
                self.assertEqual(publication_day(NOW.replace(month=month, hour=20)),
                                 f"2026-{month:02d}-22")
                self.assertEqual(publication_day(NOW.replace(month=month, hour=21)),
                                 f"2026-{month:02d}-23")

    def test_extracts_legacy_title_date_and_source(self):
        data = ("<div data-post=\"cryptonews_example/50\">"
                "<div class=\"tgme_widget_message_text js-message_text\">🟢 INFO<br/>"
                "📌 <b>Headline</b><br/>Summary<br/>"
                "<a href=\"https://example.net/news?utm_source=x\">Source</a></div>"
                "<time datetime=\"2026-11-21T23:30:00+00:00\"></time>").encode()
        self.assertEqual(parse_preview(data, "cryptonews_example"), [
            {"id": 50, "day": "2026-11-22", "title": "Headline",
             "summary": "🟢 INFO 📌 Headline Summary Source", "urls": ["https://example.net/news"]},
        ])

    def test_unknown_channel_post_stops_publishing(self):
        with patch("newsbot.history.preview", return_value=[OLD, dict(OLD, id=51)]):
            with self.assertRaisesRegex(ServiceError, "disagree"):
                verify_channel_head({"chat_id": -10042, "posts": [OLD]},
                                    Mock(chat=-10042, username="cryptonews_example"))

    def test_preview_can_catch_up_to_post_sent_in_same_run(self):
        next_post = dict(OLD, id=51)
        state = {"chat_id": -10042, "posts": [OLD, next_post]}
        target = Mock(chat=-10042, username="cryptonews_example")
        with (patch("newsbot.history.preview", side_effect=[[OLD], [OLD, next_post]]),
              patch("newsbot.history.time.sleep") as sleep):
            self.assertTrue(verify_channel_head(state, target, previous_send=51))
        sleep.assert_called_once_with(3)

    def test_preview_can_defer_when_it_still_shows_the_previous_post(self):
        state = {"chat_id": -10042, "posts": [OLD, dict(OLD, id=51)]}
        target = Mock(chat=-10042, username="cryptonews_example")
        with (patch("newsbot.history.preview", return_value=[OLD]),
              patch("newsbot.history.time.sleep") as sleep):
            self.assertFalse(verify_channel_head(state, target, previous_send=51))
            with self.assertRaisesRegex(ServiceError, "disagree"):
                verify_channel_head(state, target)
        self.assertEqual(sum(call.args[0] for call in sleep.call_args_list), 55)

    def test_preview_retry_stops_on_unknown_post(self):
        state = {"chat_id": -10042, "posts": [OLD, dict(OLD, id=51)]}
        target = Mock(chat=-10042, username="cryptonews_example")
        with (patch("newsbot.history.preview", return_value=[OLD, dict(OLD, id=52)]),
              patch("newsbot.history.time.sleep") as sleep):
            with self.assertRaisesRegex(ServiceError, "disagree"):
                verify_channel_head(state, target, previous_send=51)
        sleep.assert_not_called()

    def test_empty_public_channel_can_start_and_rejects_unrecorded_posts(self):
        target = Mock(chat=-10042, username="cryptonews_example")
        state = {"chat_id": None, "posts": []}
        with patch("newsbot.history.preview", side_effect=[[], [OLD]]):
            verify_channel_head(state, target)
            with self.assertRaisesRegex(ServiceError, "disagree"):
                verify_channel_head(state, target)

    def test_private_channel_requires_matching_history(self):
        target = Mock(chat=-10042, username=None)
        with patch("newsbot.history.preview") as preview:
            verify_channel_head({"chat_id": -10042, "posts": [OLD]}, target)
            with self.assertRaisesRegex(ServiceError, "different Telegram channel"):
                verify_channel_head({"chat_id": -10099, "posts": [OLD]}, target)
        preview.assert_not_called()

    def test_invalid_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "history.json"
            path.write_text(json.dumps({"version": 1, "chat_id": -10042, "posts": [OLD, OLD], "pending": None}))
            with self.assertRaisesRegex(ServiceError, "missing or invalid"):
                load_state(path)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.state = {"version": 1, "chat_id": -10042, "posts": [OLD], "pending": None}

    @patch("newsbot.__main__.verify_channel_head")
    @patch("newsbot.__main__.now", return_value=NOW)
    @patch("newsbot.__main__.persist")
    def test_first_post_binds_history_before_sending(self, save, clock, head):
        state = {"version": 1, "chat_id": None, "posts": [], "pending": None}
        telegram = Mock(chat=-10042)

        def send(story):
            self.assertEqual(state["chat_id"], -10042)
            self.assertIsNotNone(state["pending"])
            save.assert_called_once()
            return 1, NOW

        telegram.send.side_effect = send
        publish("some-path", state, STORY, telegram)
        self.assertEqual(state["posts"], [STORY.record(1, publication_day(NOW))])
        self.assertIsNone(state["pending"])

    @patch("newsbot.__main__.verify_channel_head")
    @patch("newsbot.__main__.now", return_value=NOW)
    @patch("newsbot.__main__.persist")
    def test_send_waits_for_durable_reservation(self, save, clock, head):
        steps = []
        save.side_effect = lambda _, state, msg: steps.append(("git", state["pending"] is not None))
        telegram = Mock(chat=-10042)
        telegram.send.side_effect = lambda _: (steps.append(("send", True)) or (51, NOW))
        publish("some-path", self.state, STORY, telegram)
        self.assertEqual(steps, [("git", True), ("send", True), ("git", False)])
        self.assertEqual(self.state["posts"][-1]["id"], 51)

    @patch("newsbot.__main__.verify_channel_head")
    @patch("newsbot.__main__.now", return_value=NOW)
    @patch("newsbot.__main__.persist", side_effect=ServiceError("git failed"))
    def test_failed_reservation_prevents_send(self, save, clock, head):
        telegram = Mock(chat=-10042)
        with self.assertRaises(ServiceError):
            publish("some-path", self.state, STORY, telegram)
        telegram.send.assert_not_called()

    @patch("newsbot.__main__.verify_channel_head")
    @patch("newsbot.__main__.now", return_value=NOW)
    @patch("newsbot.__main__.persist")
    def test_ambiguous_send_leaves_reservation(self, save, clock, head):
        telegram = Mock(chat=-10042)
        telegram.send.side_effect = ServiceError("network timeout")
        with self.assertRaises(ServiceError):
            publish("some-path", self.state, STORY, telegram)
        self.assertIsNotNone(self.state["pending"])
        save.assert_called_once()
        with patch("newsbot.__main__.load_state", return_value=self.state):
            with self.assertRaisesRegex(ServiceError, "Unresolved"):
                run("some-path", dry_run=False)

    @patch("newsbot.__main__.verify_channel_head")
    @patch("newsbot.__main__.now", return_value=NOW)
    @patch("newsbot.__main__.persist")
    def test_daily_cap_prevents_send(self, save, clock, head):
        self.state["posts"] = [dict(OLD, day=publication_day(NOW), id=i) for i in range(50, 55)]
        telegram = Mock(chat=-10042)
        self.assertEqual(daily_count(self.state, NOW), 5)
        publish("some-path", self.state, STORY, telegram)
        telegram.send.assert_not_called()
        save.assert_not_called()

    @patch("newsbot.__main__.required", return_value="placeholder")
    @patch("newsbot.__main__.verify_channel_head")
    @patch("newsbot.__main__.collect", return_value=ARTICLES)
    @patch("newsbot.__main__.evaluate", return_value=[STORY])
    @patch("newsbot.__main__.Telegram")
    @patch("newsbot.__main__.publish")
    def test_dry_run_never_sends_or_writes(self, publish_mock, telegram, evaluate, collect, head, key):
        with patch("newsbot.__main__.load_state", return_value=self.state):
            run("some-path", dry_run=True)
        telegram.return_value.check.assert_called_once()
        telegram.return_value.send.assert_not_called()
        publish_mock.assert_not_called()
        evaluate.assert_called_once()

    @patch("newsbot.__main__.required", return_value="placeholder")
    @patch("newsbot.__main__.now", return_value=NOW)
    @patch("newsbot.__main__.verify_channel_head")
    @patch("newsbot.__main__.collect", return_value=ARTICLES)
    @patch("newsbot.__main__.evaluate")
    @patch("newsbot.__main__.Telegram")
    @patch("newsbot.__main__.persist")
    def test_automatic_run_sends_up_to_two_within_daily_cap(
        self, save, telegram_class, editor, collect, head, clock, key
    ):
        stories = [STORY, replace(STORY, title="Second story"), replace(STORY, title="Third story")]
        editor.return_value = stories
        telegram = telegram_class.return_value
        telegram.chat = -10042
        for already_sent, preview_current, expected in [(0, True, 2), (4, True, 1), (0, False, 1)]:
            with self.subTest(already_sent=already_sent, preview_current=preview_current):
                state = dict(self.state, posts=[
                    dict(OLD, day=publication_day(NOW), id=index + 1)
                    for index in range(already_sent)
                ])
                telegram.send.reset_mock()
                telegram.send.side_effect = [(51, NOW), (52, NOW)]
                save.reset_mock()
                head.reset_mock()
                head.side_effect = [True, True, preview_current]
                with patch("newsbot.__main__.load_state", return_value=state):
                    run("some-path", dry_run=False)
                self.assertEqual([call.args[0] for call in telegram.send.call_args_list],
                                 stories[:expected])
                self.assertEqual(save.call_count, 2 * expected)
                self.assertIsNone(state["pending"])
                self.assertEqual(daily_count(state, NOW), already_sent + expected)
                if expected == 2:
                    self.assertEqual(head.call_args.kwargs, {"previous_send": 51})

    @patch("newsbot.__main__.required", return_value="placeholder")
    @patch("newsbot.__main__.verify_channel_head")
    @patch("newsbot.__main__.collect", return_value=ARTICLES)
    @patch("newsbot.__main__.evaluate", return_value=[STORY])
    @patch("newsbot.__main__.Telegram")
    @patch("newsbot.__main__.publish")
    def test_reviewed_run_requires_exact_source_pair(self, send, telegram, editor, feeds, head, key):
        source_urls = {canonical_url(article.url) for article in ARTICLES}
        feeds.return_value = ARTICLES + [Article("Publisher C", "Other", "Other",
                                                 "https://example.com/terzo", NOW.isoformat())]
        with patch("newsbot.__main__.load_state", return_value=self.state):
            run("some-path", dry_run=False, reviewed_sources={"https://example.net/other",
                                                              "https://example.org/secondo"})
            send.assert_not_called()
            editor.assert_not_called()
            run("some-path", dry_run=False, reviewed_sources=source_urls)
        self.assertEqual(editor.call_args.args[0], ARTICLES)
        send.assert_called_once_with("some-path", self.state, STORY, telegram.return_value)

    def test_telegram_preflight_checks_posting_rights(self):
        telegram = Telegram("placeholder", "@cryptonews_example")
        with patch.object(telegram, "call", side_effect=[
            {"id": 42}, {"id": -10042, "type": "channel", "username": "cryptonews_example"},
            {"status": "administrator", "can_post_messages": False},
        ]):
            with self.assertRaisesRegex(ServiceError, "posting rights"):
                telegram.check()

    def test_telegram_preflight_resolves_private_channel(self):
        telegram = Telegram("placeholder", "-10042")
        with patch.object(telegram, "call", side_effect=[
            {"id": 42}, {"id": -10042, "type": "channel"},
            {"status": "administrator", "can_post_messages": True},
        ]):
            telegram.check()
        self.assertEqual(telegram.chat, -10042)
        self.assertIsNone(telegram.username)

    def test_telegram_renders_classification_and_both_sources(self):
        telegram = Telegram("placeholder", "-10042")
        telegram.chat = -10042
        result = {"chat": {"id": -10042}, "message_id": 51, "date": int(NOW.timestamp())}
        with patch.object(telegram, "call", return_value=result) as send:
            self.assertEqual(telegram.send(STORY), (51, NOW))
        self.assertEqual(send.call_args.args, ("sendMessage",))
        text = send.call_args.kwargs["text"]
        self.assertTrue(text.startswith("🎯 ⬜⬜⬜🟩⬜ POSITIVE\n🔵 LONG GAME · 🕰 months\n"))
        self.assertIn("#tech #adoption\n\n📌 <b>Ethereum", text)
        for article in ARTICLES:
            self.assertIn(f'<a href="{article.url}">{article.publisher}</a>', text)


if __name__ == "__main__":
    unittest.main()
