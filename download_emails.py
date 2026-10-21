"""Download the emails in one Gmail label to readable text files on this computer.

    uv run python download_emails.py "Label Name"            # download the emails not yet downloaded
    uv run python download_emails.py "Label Name" --dry-run  # show what it would download; change nothing
    uv run python download_emails.py --labels                # list your Gmail labels (to check the name)
    uv run python download_emails.py "Label Name" --eml      # also keep the original message as .eml

Each email is saved as a readable text file in a dated folder under
~/Downloads/download-emails/<label>/:

    2026-10-20 Sender Name - Subject line.txt   headers, then the text with links kept and
                                                the tracking clutter removed

With --eml each email is also saved as it was delivered, next to its .txt:

    2026-10-20 Sender Name - Subject line.eml   the whole message, untouched

The tool only READS your mailbox (the read-only Gmail permission) and never changes it.
A record of what has been downloaded (downloaded.json, next to this file) means running it
again only fetches new mail. Setup and safety notes are in README.md.
"""

import argparse
import base64
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from datetime import date, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# --- settings ------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
# Read-only: this tool cannot change, move or delete anything in Gmail.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = HERE / "credentials.json"  # the OAuth client, copied in by hand
TOKEN_FILE = HERE / "token.json"  # the saved login, created on first run
STATE_FILE = HERE / "downloaded.json"  # what has already been downloaded
DEFAULT_OUT = Path.home() / "Downloads" / "download-emails"
MAX_NAME_LENGTH = 120

Service = object  # the Gmail API client (googleapiclient's Resource), or a test fake


class DownloadEmailsError(Exception):
    """Something to tell the user plainly, without a stack trace."""


# --- logging in ----------------------------------------------------------------------


def get_service() -> Service:
    """A Gmail client. Logs in through the browser the first time, and again whenever the
    saved login has expired (Google expires it after 7 days while the app is in Testing)."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds is None or not creds.valid:
        if creds is not None and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                print(
                    "Your saved login has expired. Opening the browser to approve again…"
                )
                creds = None
        if creds is None or not creds.valid:
            if not CREDENTIALS_FILE.exists():
                raise DownloadEmailsError(
                    f"The Google credentials file is missing: {CREDENTIALS_FILE}\n"
                    "Copy it in (see README.md, step 2), then run this again."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
        TOKEN_FILE.chmod(0o600)  # the saved login is private to you
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --- talking to Gmail ----------------------------------------------------------------


def label_names(service: Service) -> dict[str, str]:
    """Every Gmail label: name -> id."""
    reply = service.users().labels().list(userId="me").execute()  # type: ignore[attr-defined]
    return {label["name"]: label["id"] for label in reply.get("labels", [])}


def find_label_id(service: Service, name: str) -> str:
    """The id of the label called `name` (ignoring case), or a helpful error."""
    labels = label_names(service)
    for existing, label_id in labels.items():
        if existing.casefold() == name.casefold():
            return label_id
    available = ", ".join(sorted(labels)) or "(none)"
    raise DownloadEmailsError(
        f'There is no Gmail label called "{name}". Your labels are: {available}.\n'
        "Use --label to name the right one."
    )


def list_message_ids(service: Service, label_id: str) -> list[str]:
    """The ids of every message with the label, oldest page last (Gmail returns newest
    first). Follows the pages until there are no more."""
    ids: list[str] = []
    token = None
    while True:
        reply = (
            service.users()  # type: ignore[attr-defined]
            .messages()
            .list(userId="me", labelIds=[label_id], pageToken=token, maxResults=500)
            .execute()
        )
        ids += [m["id"] for m in reply.get("messages", [])]
        token = reply.get("nextPageToken")
        if not token:
            return ids


def fetch_raw(service: Service, message_id: str) -> bytes:
    """The whole message, exactly as it was delivered (RFC 822 bytes)."""
    reply = (
        service.users()  # type: ignore[attr-defined]
        .messages()
        .get(userId="me", id=message_id, format="raw")
        .execute()
    )
    return base64.urlsafe_b64decode(reply["raw"] + "=" * (-len(reply["raw"]) % 4))


def fetch_headers(service: Service, message_id: str) -> dict[str, str]:
    """Just the From, Subject and Date of a message (for a dry run: nothing is downloaded)."""
    reply = (
        service.users()  # type: ignore[attr-defined]
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )
    return {h["name"]: h["value"] for h in reply.get("payload", {}).get("headers", [])}


# --- the record of what has been downloaded ------------------------------------------


def load_state(path: Path = STATE_FILE) -> dict[str, dict[str, str]]:
    """message id -> what was saved. Empty when nothing has been downloaded yet."""
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get("downloaded", {})


def state_key(label_id: str, gmail_id: str) -> str:
    """The record's key for one message in one label, so a message that carries two labels
    is downloaded once for each."""
    return f"{label_id}/{gmail_id}"


def save_state(state: dict[str, dict[str, str]], path: Path = STATE_FILE) -> None:
    """Write the record in one go, so an interruption can't leave it half-written."""
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".downloaded-")
    with os.fdopen(handle, "w") as out:
        json.dump({"downloaded": state}, out, indent=2, sort_keys=True)
    os.replace(temp, path)


# --- making files out of a message ---------------------------------------------------

ZERO_WIDTH = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u200e\u200f\u2060\ufeff\u034f\u00ad"), None
)
BLOCK_TAGS = {
    "p", "div", "br", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5",
    "h6", "section", "article", "header", "footer", "blockquote", "hr",
}  # fmt: skip
SKIP_TAGS = {"script", "style", "head", "title"}


def clean_url(url: str) -> str:
    """The link without its utm_* tracking parameters (the rest is left as it is)."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_")
    ]
    return urlunsplit(parts._replace(query=urlencode(kept)))


class _TextExtractor(HTMLParser):
    """HTML -> readable text: paragraphs become lines and a link becomes `text <url>`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            self._href = href if href.startswith(("http://", "https://")) else None
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")
        elif tag == "a" and self._href:
            text = "".join(self._link_text).strip()
            url = clean_url(self._href)
            if url and text != url:
                self.parts.append(f" <{url}>")
            self._href = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._link_text.append(data)


def html_to_text(html: str) -> str:
    """A plain-text version of an HTML email: no markup, links kept, clutter removed."""
    extractor = _TextExtractor()
    extractor.feed(html)
    text = "".join(extractor.parts).translate(ZERO_WIDTH).replace("\u00a0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def body_text(message: EmailMessage) -> str:
    """The text of an email: its plain-text part when it has one, else its HTML part
    turned into text."""
    plain = message.get_body(preferencelist=("plain",))
    if plain is not None:
        text = plain.get_content().translate(ZERO_WIDTH).replace("\u00a0", " ")
        if text.strip():
            return re.sub(r"\n{3,}", "\n\n", text).strip()
    html = message.get_body(preferencelist=("html",))
    return html_to_text(html.get_content()) if html is not None else ""


def readable_text(message: EmailMessage, gmail_id: str) -> str:
    """The .txt version: a few headers, any attachment names, then the text."""
    attachments = [a.get_filename() or "(unnamed)" for a in message.iter_attachments()]
    header = [
        f"From: {message['From'] or ''}",
        f"To: {message['To'] or ''}",
        f"Date: {message['Date'] or ''}",
        f"Subject: {message['Subject'] or ''}",
    ]
    if attachments:
        header.append("Attachments: " + ", ".join(attachments))
    header.append(f"Gmail id: {gmail_id}")
    text = "\n".join(header) + "\n\n" + body_text(message) + "\n"
    return text.replace("\r\n", "\n").replace("\r", "\n")


def safe_filename(text: str, limit: int = MAX_NAME_LENGTH) -> str:
    """Text made safe to use in a file name: no slashes or control characters, tidy spaces,
    cut to `limit` characters."""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", text.translate(ZERO_WIDTH))
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].rstrip(" .")


def sender_name(message: EmailMessage) -> str:
    """Who an email is from, for its file name: the display name, else the address."""
    name, address = parseaddr(str(message["From"] or ""))
    return name or address.split("@")[0] or "unknown sender"


def message_day(message: EmailMessage, fallback: date) -> date:
    """The day an email was sent (in its own time zone), or `fallback` if it has no date."""
    try:
        return parsedate_to_datetime(str(message["Date"])).date()
    except (TypeError, ValueError):
        return fallback


def base_name(message: EmailMessage, fallback_day: date) -> str:
    """The file name (without extension): "2026-10-20 Sender - Subject"."""
    day = message_day(message, fallback_day)
    subject = str(message["Subject"] or "(no subject)")
    return safe_filename(f"{day:%Y-%m-%d} {sender_name(message)} - {subject}")


def free_path(folder: Path, name: str, gmail_id: str) -> str:
    """`name`, or `name [id]` when a file of that name is already there."""
    if not (folder / f"{name}.txt").exists() and not (folder / f"{name}.eml").exists():
        return name
    return f"{name} [{gmail_id[:8]}]"


# --- the command ---------------------------------------------------------------------


def save_message(
    folder: Path, raw: bytes, gmail_id: str, today: date, keep_eml: bool = False
) -> tuple[str, EmailMessage]:
    """Write the .txt for one message (and the .eml too when `keep_eml`) and check what was
    written can be read back. Returns the file stem used and the parsed message. Raises
    DownloadEmailsError if the check fails."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    folder.mkdir(parents=True, exist_ok=True)
    stem = free_path(folder, base_name(message, today), gmail_id)
    data = readable_text(message, gmail_id).encode("utf-8")
    txt = folder / f"{stem}.txt"
    txt.write_bytes(data)
    ok = bool(data) and txt.read_bytes() == data
    if keep_eml:
        eml = folder / f"{stem}.eml"
        eml.write_bytes(raw)
        ok = ok and eml.stat().st_size > 0 and eml.read_bytes() == raw
    if not ok:
        # Don't leave a half-trusted file behind: the next run saves this email afresh.
        txt.unlink(missing_ok=True)
        if keep_eml:
            eml.unlink(missing_ok=True)
        raise DownloadEmailsError(
            f"{stem}: the saved files did not read back correctly."
        )
    return stem, message


def run(
    argv: Sequence[str] | None = None,
    *,
    service: Service | None = None,
    state_path: Path = STATE_FILE,
    out: Callable[[str], None] = print,
    now: Callable[[], datetime] = datetime.now,
) -> int:
    """The command. Returns the exit code (0 when everything asked for was done)."""
    parser = argparse.ArgumentParser(
        prog="download_emails.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "label",
        nargs="?",
        help='the Gmail label to download, such as "Receipts" (see --labels)',
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="where to save (default ~/Downloads/download-emails)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be downloaded; change nothing",
    )
    parser.add_argument(
        "--labels", action="store_true", help="list your Gmail labels and stop"
    )
    parser.add_argument(
        "--eml",
        action="store_true",
        help="also keep each original message as an .eml file next to its .txt",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="download at most this many (0 = all)"
    )
    args = parser.parse_args(argv)
    if not args.labels and not args.label:
        parser.error(
            'name the Gmail label to download, e.g. "Receipts" (--labels lists yours)'
        )

    try:
        service = service or get_service()
        if args.labels:
            for name in sorted(label_names(service), key=str.casefold):
                out(name)
            return 0

        label_id = find_label_id(service, args.label)
        ids = list_message_ids(service, label_id)
        state = load_state(state_path)
        new = [i for i in ids if state_key(label_id, i) not in state]
        if args.limit > 0:
            new = new[: args.limit]
        already = sum(1 for i in ids if state_key(label_id, i) in state)
        out(
            f'"{args.label}" has {len(ids)} email(s); {already} already downloaded, {len(new)} to do.'
        )

        if args.dry_run:
            for gmail_id in new:
                headers = fetch_headers(service, gmail_id)
                out(
                    f"  would download: {headers.get('Date', '?')} | {headers.get('From', '?')} | {headers.get('Subject', '(no subject)')}"
                )
            out("This was a dry run: nothing was downloaded.")
            return 0

        today = now().date()
        folder = args.out.expanduser() / safe_filename(args.label) / f"{today:%Y-%m-%d}"
        saved = failed = 0
        for gmail_id in new:
            try:
                stem, message = save_message(
                    folder,
                    fetch_raw(service, gmail_id),
                    gmail_id,
                    today,
                    keep_eml=args.eml,
                )
            except (DownloadEmailsError, OSError) as error:
                failed += 1
                out(f"  FAILED {gmail_id}: {error}")
                continue
            state[state_key(label_id, gmail_id)] = {
                "label": args.label,
                "file": f"{folder.parent.name}/{today:%Y-%m-%d}/{stem}",
                "message_id": str(message["Message-ID"] or ""),
                "saved_at": now().isoformat(timespec="seconds"),
            }
            save_state(
                state, state_path
            )  # after each one, so a stop part-way loses nothing
            saved += 1
            out(f"  saved: {stem}")
        out(f"Done: {saved} downloaded, {failed} failed. Files are in {folder}")
        return 1 if failed else 0
    except DownloadEmailsError as error:
        out(str(error))
        return 2


if __name__ == "__main__":
    sys.exit(run())
