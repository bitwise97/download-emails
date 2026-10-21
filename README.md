# download-emails

Download the emails in one Gmail label to readable text files on your computer, ready to read, search,
or hand to an AI. It is a small command-line script that **only reads** your mailbox.

```bash
uv run python download_emails.py "Receipts"
```

- Each email is saved as one `.txt` file in a dated folder under `~/Downloads/download-emails/<label>/`,
  named like `2026-10-20 Sender Name - Subject line.txt`. The file has a few headers, then the message text with
  links kept and tracking parameters, invisible characters and HTML clutter removed.
- Add `--eml` to also keep each message exactly as delivered (`.eml`) next to its `.txt`.
- It remembers what it has downloaded, per label, so running it again fetches only new mail.
- It uses Gmail's **read-only** permission. It cannot delete, move, label or send anything.
- Nothing is uploaded anywhere. Mail goes from Gmail to your computer, and the script talks only to Google.

## What you need

- Python 3.13 and [uv](https://docs.astral.sh/uv/) (tested with Python 3.13 on macOS)
- A Gmail account
- Your own Google Cloud project with the Gmail API turned on (the setup below takes about ten minutes, once)

## One-time setup

1. **Get the code and its libraries**

   ```bash
   git clone https://github.com/bitwise97/download-emails.git
   cd download-emails
   uv sync
   ```

2. **Create Google credentials.** In the [Google Cloud console](https://console.cloud.google.com/):
   1. Create a project (or pick one you already have).
   2. **APIs & Services → Library → Gmail API → Enable.**
   3. **Google Auth Platform** (the OAuth consent screen): choose *External*, give the app a name, set your
      email as the support and contact address, and finish the wizard.
   4. Under **Audience**, add your own Gmail address as a **Test user** (needed while the app is in Testing).
   5. **Clients → Create client → Application type: Desktop app.** Download the JSON file it offers.
   6. Save that file as `credentials.json` in this folder and make it private:

      ```bash
      chmod 600 credentials.json
      ```

   `credentials.json` identifies *your* app to Google. It is a secret and is listed in `.gitignore`, so it is
   never committed. Do not share it.

3. **Log in once.** This opens your browser to approve **read-only** access to your own mail:

   ```bash
   uv run python download_emails.py --labels
   ```

   Google warns that the app is unverified, because it is yours and has not been through Google's review.
   Choose *Advanced*, then *Go to … (unsafe)*, then approve. The script prints your Gmail labels and stops,
   and saves your login as `token.json` (private to you).

## Use

```bash
uv run python download_emails.py "Label Name" --dry-run   # show what it would download; change nothing
uv run python download_emails.py "Label Name"             # download the new emails
```

The label name is required (use `--labels` to see yours; case is ignored). Nested labels such as
`Work/Reports` work too and get a folder named `Work Reports`.

| Option | What it does |
|---|---|
| `--dry-run` | List the emails that would be downloaded, with date, sender and subject. Writes nothing. |
| `--labels` | List your Gmail labels and stop. |
| `--eml` | Also keep each original message as an `.eml` file. |
| `--out FOLDER` | Save somewhere else (default `~/Downloads/download-emails`). |
| `--limit N` | Download at most N emails this run. |

Each email is read back after it is written; only then is it recorded as downloaded. If one email fails the
others still download, and the failed one is retried next time.

## Your data and privacy

- The script runs on your computer and talks only to Google's Gmail service.
- `token.json` (your saved login) and `credentials.json` stay in this folder, readable only by you. To log
  out, delete `token.json`. To cut off access entirely, remove the app at
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
- `downloaded.json` records which messages were downloaded (Gmail's message ids, the saved file names and
  the time). Delete it to download everything again.
- Downloaded email is as private as the email itself, so keep it out of public repositories and shared folders.
  The `.gitignore` excludes the credentials, your login, the record, and `.eml` files.

## Notes and limits

- **Weekly login.** While your Google Cloud app is in *Testing*, Google expires the saved login after
  7 days; the script opens the browser again by itself. Publishing the app ("In production" on the consent
  screen) avoids that, but then Google expects a home page and a privacy policy link for the app.
- **Read-only by design.** The script asks for one permission (`gmail.readonly`). It does not delete the
  emails it downloads; clean up your label in Gmail yourself.
- Very large mailboxes are fine (it follows Gmail's pages of results), but a first run may take a while.
- Gmail's API has usage limits; if you hit one, wait and run again, since it resumes where it stopped.

## Tests

```bash
uv run python -m unittest
```

The tests use a fake Gmail and made-up emails. They never touch a real mailbox.

## License

MIT. See [LICENSE](LICENSE).
