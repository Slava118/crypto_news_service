import argparse
import datetime as dt
import logging
import os

from .editor import DAILY_LIMIT, DEFAULT_MODEL, evaluate
from .feeds import canonical_url, collect
from .history import import_history, load_state, persist, publication_day, verify_channel_head, write_state
from .http import ServiceError
from .telegram import Telegram


POSTS_PER_RUN = 2


def required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ServiceError(f"Missing configuration: {name}")
    return value


def now():
    return dt.datetime.now(dt.UTC)


def daily_count(state, timestamp):
    day = publication_day(timestamp)
    return sum(post["day"] == day for post in state["posts"])


def publish(path, state, story, telegram, previous_send=None):
    if not verify_channel_head(state, telegram, previous_send=previous_send):
        logging.warning("Telegram preview is still catching up to message %d; deferring the next story",
                        previous_send)
        return
    timestamp = now()
    if daily_count(state, timestamp) >= DAILY_LIMIT:
        logging.info("Moscow daily publication cap reached")
        return
    reservation = story.record(0, publication_day(timestamp))
    reservation["reserved_at"] = timestamp.isoformat()
    state["pending"] = reservation
    state["chat_id"] = telegram.chat
    persist(path, state, "Reserve Telegram publication")
    message_id, sent = telegram.send(story)
    latest_id = max((post["id"] for post in state["posts"]), default=0)
    if type(message_id) is not int or message_id <= latest_id:
        raise ServiceError("Telegram returned an invalid or previously recorded message ID")
    state["posts"].append(story.record(message_id, publication_day(sent)))
    state["pending"] = None
    persist(path, state, f"Record Telegram publication {message_id}")
    logging.info("Published Telegram message %d at %s", message_id, sent.isoformat())
    return message_id


def run(path, dry_run, reviewed_sources=None):
    state = load_state(path)
    if state["pending"] is not None:
        raise ServiceError("Unresolved publication reservation; reconcile Telegram before retrying")
    telegram = Telegram(required("TELEGRAM_BOT_TOKEN"), required("TELEGRAM_CHAT_ID"))
    telegram.check()
    verify_channel_head(state, telegram)
    timestamp = now()
    remaining = max(0, DAILY_LIMIT - daily_count(state, timestamp))
    logging.info("Moscow day %s: %d of %d publication slots remain; %d posts in history",
                 publication_day(timestamp), remaining, DAILY_LIMIT, len(state["posts"]))
    if not dry_run and not remaining:
        return
    articles = collect(now())
    if reviewed_sources:
        articles = [article for article in articles if canonical_url(article.url) in reviewed_sources]
        if len(articles) != 2:
            logging.info("Reviewed sources are unavailable in the current feeds; no message sent")
            return
    logging.info("Evaluating %d unique recent articles", len(articles))
    stories = evaluate(articles, state["posts"], required("GEMINI_API_KEY"),
                       os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL)
    logging.info("%d stories passed the editorial and duplicate checks", len(stories))
    if dry_run:
        for story in stories:
            logging.info("Candidate %d/10: %s | %s | %s | %s", story.score,
                         " | ".join(story.classification_lines()), story.title, story.summary,
                         " · ".join(article.url for article in story.articles))
        logging.info("Dry run complete; no Telegram message or history changes")
    else:
        if reviewed_sources:
            stories = [story for story in stories if
                       {canonical_url(article.url) for article in story.articles} == reviewed_sources]
            if not stories:
                logging.info("Reviewed source pair did not qualify in this run; no message sent")
            else:
                publish(path, state, stories[0], telegram)
            return
        previous_send = None
        for story in stories[:min(remaining, POSTS_PER_RUN)]:
            previous_send = publish(path, state, story, telegram, previous_send=previous_send)
            if previous_send is None:
                break


def main():
    parser = argparse.ArgumentParser(description="Publish essential crypto news to Telegram")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--publish", action="store_true", help="allow a verified news send")
    action.add_argument("--dry-run", action="store_true", help="exercise integrations without sending")
    action.add_argument("--import-history", action="store_true", help="seed from the public channel")
    parser.add_argument("--reviewed-sources", help="restrict a live run to two reviewed source URLs")
    parser.add_argument("--history", default=".publication/history.json")
    args = parser.parse_args()
    reviewed_sources = None
    if args.reviewed_sources is not None:
        urls = args.reviewed_sources.split()
        reviewed_sources = {canonical_url(url) for url in urls}
        if not args.publish or len(urls) != 2 or len(reviewed_sources) != 2 or "" in reviewed_sources:
            parser.error("--reviewed-sources requires --publish and two distinct source URLs")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        if args.import_history:
            if os.path.exists(args.history):
                raise ServiceError("History already exists; use a separate path for a new import")
            telegram = Telegram(required("TELEGRAM_BOT_TOKEN"), required("TELEGRAM_CHAT_ID"))
            telegram.check()
            state = import_history(telegram)
            write_state(args.history, state)
            logging.info("Imported %d public Telegram posts", len(state["posts"]))
        else:
            run(args.history, dry_run=not args.publish, reviewed_sources=reviewed_sources)
        return 0
    except ServiceError as exc:
        logging.error("%s", exc)
    except Exception as exc:
        # Avoid tracebacks in scheduled logs because third-party errors can embed request URLs.
        logging.error("Unexpected %s; check whether a publication reservation needs reconciliation",
                      type(exc).__name__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
