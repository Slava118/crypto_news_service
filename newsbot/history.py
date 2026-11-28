import datetime as dt
import json
import re
import subprocess
import time
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

from .feeds import canonical_url
from .http import ServiceError, fetch

MOSCOW = ZoneInfo("Europe/Moscow")
STATE_BRANCH = "publication-state"


def publication_day(timestamp):
    return timestamp.astimezone(MOSCOW).date().isoformat()


class MessageText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.urls = []

    def handle_data(self, data):
        self.text.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.text.append("\n")
        if tag == "a":
            url = canonical_url(dict(attrs).get("href", ""))
            if url and not url.startswith(("https://t.me/", "https://telegram.me/")):
                self.urls.append(url)


def parse_preview(data, username):
    pattern = rf'\bdata-post="{re.escape(username)}/(\d+)"'
    parts = re.split(pattern, data.decode("utf-8"), flags=re.IGNORECASE)
    posts = []
    for message_id, body in zip(parts[1::2], parts[2::2]):
        timestamp = re.search(r'<time\s+datetime="([^"]+)"', body)
        if not timestamp:
            raise ServiceError("Telegram preview post has no timestamp")
        sent = dt.datetime.fromisoformat(timestamp[1])
        match = re.search(r'<div class="tgme_widget_message_text\b[^>]*>(.*?)</div>',
                          body, re.DOTALL)
        content = MessageText()
        if match:
            content.feed(match[1])
        lines = [" ".join(line.split()) for line in "".join(content.text).splitlines()]
        lines = [line for line in lines if line]
        headline = next((line.lstrip("📌 ").strip() for line in lines if line.startswith("📌")),
                        lines[0] if lines else "")
        posts.append({"id": int(message_id), "day": publication_day(sent), "title": headline[:240],
                      "summary": " ".join(lines)[:480], "urls": sorted(set(content.urls))})
    return posts


def preview(username, before=None):
    query = f"?before={before}" if before is not None else ""
    return parse_preview(fetch(f"https://t.me/s/{username}{query}", service="Telegram preview",
                               headers={"Cache-Control": "no-cache"}), username)


def import_history(telegram):
    if not telegram.username:
        raise ServiceError("History import requires a public Telegram channel")
    posts = {}
    before = None
    while True:
        page = preview(telegram.username, before)
        if not page:
            if not posts:
                raise ServiceError("Telegram preview did not return any existing channel posts")
            return {"version": 1, "chat_id": telegram.chat, "pending": None,
                    "posts": sorted(posts.values(), key=lambda post: post["id"])}
        oldest = min(post["id"] for post in page)
        if before is not None and oldest >= before:
            raise ServiceError("Telegram preview pagination did not advance")
        posts.update((post["id"], post) for post in page if before is None or post["id"] < before)
        before = oldest


def verify_channel_head(state, telegram, previous_send=None):
    """Return False when the public preview still trails a send from this run."""
    if state["chat_id"] is not None and state["chat_id"] != telegram.chat:
        raise ServiceError("Publication history belongs to a different Telegram channel")
    if not telegram.username:
        return True
    known = {post["id"] for post in state["posts"]}
    latest = max(known, default=0)
    previous_head = max(known - {previous_send}, default=0)
    delays = (0, 3, 7, 15, 30) if previous_send == latest else (0,)
    for delay in delays:
        if delay:
            time.sleep(delay)
        public = {post["id"] for post in preview(telegram.username)}
        public_head = max(public, default=0)
        if public - known:
            break
        if public_head == latest:
            return True
        if previous_send != latest or public_head != previous_head:
            break
    else:
        return False
    raise ServiceError("Telegram preview and publication history disagree; reconcile before sending")


def load_state(path):
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
        posts = state["posts"]
        ids = set()
        chat_id = state["chat_id"]
        if (state["version"] != 1 or "pending" not in state or not isinstance(posts, list)
                or (chat_id is not None and (type(chat_id) is not int or chat_id >= 0))
                or (chat_id is None and (posts or state["pending"] is not None))):
            raise ValueError
        for post in posts:
            if (type(post["id"]) is not int or post["id"] < 1 or post["id"] in ids
                    or not isinstance(post["title"], str) or not isinstance(post["summary"], str)
                    or not isinstance(post["urls"], list)
                    or any(not isinstance(url, str) or not canonical_url(url) for url in post["urls"])):
                raise ValueError
            dt.date.fromisoformat(post["day"])
            ids.add(post["id"])
        return state
    except (OSError, ValueError, KeyError, TypeError):
        raise ServiceError("Publication history is missing or invalid") from None


def write_state(path, state):
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def persist(path, state, message):
    path = Path(path).resolve()
    try:
        root = git(path.parent, "rev-parse", "--show-toplevel")
        if (Path(root).resolve() != path.parent or path.name != "history.json"
                or git(path.parent, "branch", "--show-current") != STATE_BRANCH):
            raise ServiceError("History must be checked out on the publication-state branch")
        write_state(path, state)
        git(path.parent, "add", "--", path.name)
        git(path.parent, "commit", "-m", message)
        git(path.parent, "push", "origin", f"HEAD:refs/heads/{STATE_BRANCH}")
    except (OSError, subprocess.CalledProcessError):
        raise ServiceError("Could not persist publication history to GitHub") from None


def git(directory, *args):
    return subprocess.check_output(["git", *args], cwd=directory, stderr=subprocess.DEVNULL,
                                   text=True).strip()
