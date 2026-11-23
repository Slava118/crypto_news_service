# Essential Crypto News

A Python service that publishes short English-language crypto briefs to Telegram. It covers major network upgrades and outages, material security incidents, stablecoins, regulation, and institutional developments.

The [GitHub Actions publisher](https://github.com/Slava118/crypto_news_service/actions/workflows/news.yml) checks feeds at minute 37 of each hour. It sends at most one story per run and five per calendar day in `Europe/Moscow`. GitHub can delay scheduled runs.

## Editorial rules

- Read recent RSS items from CoinDesk, Cointelegraph, Decrypt, and The Block. Gemini ranks stories and looks for independent confirmation of the same central fact.
- Require a score of at least 8/10, two different publishers, and supporting excerpts present in both feeds. Every brief links to both publishers.
- Skip rumors, routine price moves, sponsored stories, and trading advice. Compare each candidate with publication history by URL and overlapping headline and summary terms. A run with no qualifying story sends nothing.

## Sentiment and impact

Posts use the channel's five-position sentiment bar and a separate impact label. The internal 8/10 cutoff measures editorial importance. A neutral story can qualify.

| Bar | Sentiment | Meaning for the affected asset, protocol, or sector |
| --- | --- | --- |
| 🟥⬜⬜⬜⬜ | Strongly negative | Exceptionally severe harm or market decline |
| ⬜🟧⬜⬜⬜ | Negative | Clear adverse effect |
| ⬜⬜🟨⬜⬜ | Neutral | Mixed effects or no clear direction supported by the sources |
| ⬜⬜⬜🟩⬜ | Positive | Clear beneficial effect |
| ⬜⬜⬜⬜🟦 | Strongly positive | Exceptionally large, corroborated benefit |

**Market Mover · now** indicates a documented immediate market effect. **Long Game · months** marks structural developments. **Background** covers other relevant context. Posts include one or two topic tags. Exceptional price moves and flows can qualify when two sources support them. The publisher uses reported figures and time windows from the feeds. Sentiment describes the reported development and provides no price forecast.

## Set up Telegram and GitHub

1. In Telegram, add a dedicated bot to the channel as an administrator with **Post messages** permission. Use a new channel or import its public history before publishing, as described below.
2. In GitHub **Settings → Secrets and variables → Actions**, add repository secrets `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (a public `@username` or numeric `-100…` channel ID), and `GEMINI_API_KEY`. The default model is `gemini-3.5-flash-lite`. You can set the optional `GEMINI_MODEL` Actions variable to another supported Gemini Flash model. Use a Gemini project without billing enabled if you want to stay on the free tier. [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing#gemini-3.5-flash-lite)
3. Keep the `publication-state` branch and allow the workflow token to push to it. Its root `history.json` starts empty for a new channel. For an existing public channel, run `python -m newsbot --import-history --history imported-history.json` locally with the Telegram variables set. Review the import and commit it to `publication-state` as `history.json`. Telegram public previews are used to detect unexpected posts. For private channels, use a new channel dedicated to this bot, since the Bot API cannot backfill or compare the channel's history.
4. In **Actions → Telegram news publisher → Run workflow**, leave **reviewed_sources** blank. The dry run checks Telegram bot permissions and evaluates live feeds. It logs candidates and links without sending messages or saving history.
5. After checking the dry run, set the Actions repository variable `PUBLISH_ENABLED` to `true` to enable scheduled sends. To try a reviewed candidate immediately, run the workflow with its two source URLs separated by a space in **reviewed_sources**. The story will be sent only if exactly that pair still qualifies. Set `PUBLISH_ENABLED` to `false` to pause sends.

## Publication history and recovery

The publisher pushes a reservation to GitHub before making a single Telegram send attempt. On success it records Telegram's message ID and timestamp-derived publication day. An unresolved reservation blocks later sends because Telegram might have accepted a timed-out request.

If a reservation remains pending, inspect Telegram around its `reserved_at` timestamp. If the post exists, add its message ID and Moscow date to the saved title, summary, and URLs in `posts`, then set `pending` to `null` in the same commit. Clear a reservation for a missing post only after confirming that it was never sent. If the public preview contains unexpected posts, merge their records into history. Run a dry run after reconciling the branch.

## Local development

Use Python 3.13. Windows needs the time-zone data in `requirements.txt`.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m newsbot --dry-run --history path/to/history.json
```

Tests run offline. A local dry run uses the same three environment variables as the workflow and a copy of the current publication history.
