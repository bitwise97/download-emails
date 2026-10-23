"""Tests for download_emails.py. Everything is made up: a fake Gmail and invented emails."""

import base64
import json
import tempfile
import unittest
from datetime import UTC, date, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

import download_emails as de


def make_email(
    subject="Senior Widget Architect at Acme",
    sender="Acme Jobs <jobs@acme.example>",
    day="Tue, 20 Oct 2026 09:30:00 -0700",
    text="Hello,\n\nA role you may like.\n",
    html=None,
):
    message = EmailMessage()
    message["From"], message["To"] = sender, "me@example.test"
    message["Subject"], message["Date"] = subject, day
    message["Message-ID"] = f"<{abs(hash((subject, day)))}@example.test>"
    if text is not None:
        message.set_content(text)
    if html is not None:
        if text is None:
            message.set_content(html, subtype="html")
        else:
            message.add_alternative(html, subtype="html")
    return message.as_bytes()


class FakeGmail:
    """Just enough of the Gmail API client for the tool: labels, a message list, messages."""

    def __init__(self, labels, messages, page_size=2):
        self.label_map = labels  # name -> id
        self.mail = messages  # id -> raw bytes
        self.page_size = page_size
        self.fetched = []

    def users(self):
        return self

    def labels(self):
        outer = self

        class Labels:
            def list(self, userId):
                return Call(
                    {
                        "labels": [
                            {"name": n, "id": i} for n, i in outer.label_map.items()
                        ]
                    }
                )

        return Labels()

    def messages(self):
        outer = self

        class Messages:
            def list(self, userId, labelIds, pageToken=None, maxResults=500):
                ids = list(outer.mail)
                start = int(pageToken or 0)
                page = ids[start : start + outer.page_size]
                reply = {"messages": [{"id": i} for i in page]}
                if start + outer.page_size < len(ids):
                    reply["nextPageToken"] = str(start + outer.page_size)
                return Call(reply)

            def get(self, userId, id, format, metadataHeaders=None):
                outer.fetched.append((id, format))
                raw = outer.mail[id]
                if format == "raw":
                    return Call(
                        {"raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")}
                    )
                parsed = BytesParser(policy=policy.default).parsebytes(raw)
                names = metadataHeaders or []
                return Call(
                    {
                        "payload": {
                            "headers": [
                                {"name": n, "value": str(parsed[n])} for n in names
                            ]
                        }
                    }
                )

        return Messages()


class Call:
    def __init__(self, reply):
        self.reply = reply

    def execute(self):
        return self.reply


class Tools(unittest.TestCase):
    def test_file_names_are_safe_and_short(self):
        self.assertEqual(de.safe_filename('a/b:c*d?"e<f>g|h'), "a b c d e f g h")
        self.assertEqual(de.safe_filename("  ..hello..  "), "hello")
        self.assertLessEqual(len(de.safe_filename("x" * 500)), de.MAX_NAME_LENGTH)

    def test_the_name_is_day_sender_subject(self):
        message = BytesParser(policy=policy.default).parsebytes(make_email())
        self.assertEqual(
            de.base_name(message, date(2000, 1, 1)),
            "2026-10-20 Acme Jobs - Senior Widget Architect at Acme",
        )

    def test_a_message_with_no_date_or_subject_still_gets_a_name(self):
        message = BytesParser(policy=policy.default).parsebytes(
            b"From: x@example.test\n\nbody\n"
        )
        self.assertEqual(
            de.base_name(message, date(2026, 10, 20)), "2026-10-20 x - (no subject)"
        )

    def test_html_becomes_text_with_links_kept_and_tracking_removed(self):
        html = (
            "<html><head><style>p{}</style></head><body><p>New role</p>"
            '<p><a href="https://jobs.example.test/1?utm_source=mail&id=9&utm_medium=x">Apply now</a></p>'
            "<p>\u200c\u200c\u200c</p><script>x()</script></body></html>"
        )
        text = de.html_to_text(html)
        self.assertIn("New role", text)
        self.assertIn("Apply now <https://jobs.example.test/1?id=9>", text)
        self.assertNotIn("utm_", text)
        self.assertNotIn("x()", text)
        self.assertNotIn("p{}", text)

    def test_a_link_whose_text_is_the_address_is_not_repeated(self):
        text = de.html_to_text(
            '<a href="https://x.example.test/a">https://x.example.test/a</a>'
        )
        self.assertEqual(text.count("https://x.example.test/a"), 1)

    def test_the_plain_part_is_preferred_over_the_html_part(self):
        message = BytesParser(policy=policy.default).parsebytes(
            make_email(text="Plain version", html="<p>HTML version</p>")
        )
        self.assertEqual(de.body_text(message), "Plain version")

    def test_an_html_only_email_is_converted(self):
        message = BytesParser(policy=policy.default).parsebytes(
            make_email(text=None, html="<p>Only <b>HTML</b></p>")
        )
        self.assertEqual(de.body_text(message), "Only HTML")

    def test_the_readable_text_has_headers_then_body(self):
        message = BytesParser(policy=policy.default).parsebytes(make_email())
        text = de.readable_text(message, "abc123")
        self.assertTrue(text.startswith("From: Acme Jobs <jobs@acme.example>\n"))
        self.assertIn("Subject: Senior Widget Architect at Acme", text)
        self.assertIn("Gmail id: abc123", text)
        self.assertTrue(text.rstrip().endswith("A role you may like."))


class Command(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.state = self.root / "downloaded.json"
        self.out_dir = self.root / "downloads"
        self.lines: list[str] = []
        self.now = lambda: datetime(2026, 10, 21, 8, 0, 0, tzinfo=UTC)

    def run_cmd(self, service, *args, label="Alerts"):
        argv = ["--out", str(self.out_dir), *args]
        if "--labels" not in args:
            argv.append(label)
        code = de.run(
            argv,
            service=service,
            state_path=self.state,
            out=self.lines.append,
            now=self.now,
        )
        return code, "\n".join(self.lines)

    def gmail(self, count=3):
        messages = {
            f"id{n}": make_email(
                subject=f"Role number {n}", day=f"Tue, 20 Oct 2026 0{n}:00:00 -0700"
            )
            for n in range(1, count + 1)
        }
        return FakeGmail({"INBOX": "INBOX", "Alerts": "Label_1"}, messages)

    def files(self, suffix):
        return sorted(p.name for p in self.out_dir.rglob(f"*{suffix}"))

    def test_an_email_with_windows_line_endings_is_saved_with_clean_ones(self):
        # Many mailers send the text part quoted-printable with CRLF line endings, which
        # stay in the text after decoding.
        raw = (
            b"From: Alerts <alerts@example.test>\r\n"
            b"Subject: Windows line endings\r\n"
            b"Date: Tue, 20 Oct 2026 09:30:00 -0700\r\n"
            b"Message-ID: <crlf@example.test>\r\n"
            b"MIME-Version: 1.0\r\n"
            b'Content-Type: text/plain; charset="utf-8"\r\n'
            b"Content-Transfer-Encoding: quoted-printable\r\n"
            b"\r\n"
            b"First line=\r\n"
            b" continues here\r\n"
            b"Second line\r\n"
        )
        service = FakeGmail({"Alerts": "L"}, {"id1": raw})
        code, text = self.run_cmd(service)
        self.assertEqual(code, 0, text)  # not reported as a failed save
        saved = next(self.out_dir.rglob("*.txt")).read_bytes()
        self.assertNotIn(b"\r", saved)
        self.assertIn(b"First line continues here\nSecond line", saved)

    def test_it_downloads_every_message_as_a_txt_in_a_dated_folder(self):
        code, _ = self.run_cmd(self.gmail(3))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.files(".txt")), 3)
        self.assertEqual(self.files(".eml"), [])  # the original is only kept on request
        self.assertTrue((self.out_dir / "Alerts" / "2026-10-21").is_dir())
        txt = next(self.out_dir.rglob("*Role number 1*.txt"))
        self.assertIn("Subject: Role number 1", txt.read_text(encoding="utf-8"))

    def test_the_eml_option_also_keeps_the_original_message(self):
        self.run_cmd(self.gmail(2), "--eml")
        self.assertEqual(len(self.files(".txt")), 2)
        self.assertEqual(len(self.files(".eml")), 2)
        eml = next(self.out_dir.rglob("*Role number 1*.eml"))
        self.assertEqual(
            eml.read_bytes(),
            make_email(subject="Role number 1", day="Tue, 20 Oct 2026 01:00:00 -0700"),
        )

    def test_the_pages_of_message_ids_are_all_followed(self):
        _, text = self.run_cmd(self.gmail(5))  # the fake pages by 2
        self.assertEqual(len(self.files(".txt")), 5)
        self.assertIn("has 5 email(s)", text)

    def test_running_again_downloads_only_new_mail(self):
        service = self.gmail(2)
        self.run_cmd(service)
        service.mail["id3"] = make_email(
            subject="A new one", day="Tue, 20 Oct 2026 03:00:00 -0700"
        )
        self.lines.clear()
        code, text = self.run_cmd(service)
        self.assertEqual(code, 0)
        self.assertIn("2 already downloaded, 1 to do", text)
        self.assertEqual(len(self.files(".txt")), 3)
        self.assertEqual(len(json.loads(self.state.read_text())["downloaded"]), 3)

    def test_a_dry_run_changes_nothing_and_fetches_only_headers(self):
        service = self.gmail(2)
        code, text = self.run_cmd(service, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would download", text)
        self.assertIn("Role number 1", text)
        self.assertFalse(self.out_dir.exists())
        self.assertFalse(self.state.exists())
        self.assertTrue(all(fmt == "metadata" for _, fmt in service.fetched))

    def test_the_limit_caps_the_number_downloaded(self):
        self.run_cmd(self.gmail(4), "--limit", "2")
        self.assertEqual(len(self.files(".txt")), 2)

    def test_two_emails_with_the_same_name_are_both_kept(self):
        same = make_email(subject="Same", day="Tue, 20 Oct 2026 01:00:00 -0700")
        service = FakeGmail({"Alerts": "L"}, {"idA1111111": same, "idB2222222": same})
        self.run_cmd(service)
        self.assertEqual(len(self.files(".txt")), 2)

    def test_a_missing_label_lists_the_labels_there_are(self):
        service = FakeGmail({"Work": "1", "Receipts": "2"}, {})
        code, text = self.run_cmd(service)
        self.assertEqual(code, 2)
        self.assertIn("Receipts, Work", text)
        self.assertFalse(self.state.exists())

    def test_the_label_name_ignores_case(self):
        code, _ = self.run_cmd(self.gmail(1), label="alerts")
        self.assertEqual(code, 0)

    def test_the_labels_option_lists_them_and_stops(self):
        service = self.gmail(1)
        code, text = self.run_cmd(service, "--labels")
        self.assertEqual(code, 0)
        self.assertIn("Alerts", text)
        self.assertEqual(service.fetched, [])

    def test_a_failure_on_one_message_does_not_stop_the_rest_or_record_it(self):
        service = self.gmail(3)
        original = de.save_message

        def flaky(folder, raw, gmail_id, today, keep_eml=False):
            if gmail_id == "id2":
                raise de.DownloadEmailsError("disk trouble")
            return original(folder, raw, gmail_id, today, keep_eml)

        de.save_message = flaky
        self.addCleanup(setattr, de, "save_message", original)
        code, text = self.run_cmd(service)
        self.assertEqual(code, 1)
        self.assertIn("FAILED id2", text)
        self.assertEqual(
            sorted(json.loads(self.state.read_text())["downloaded"]),
            ["Label_1/id1", "Label_1/id3"],
        )
        # Run again once it works: only the failed one is fetched.
        de.save_message = original
        self.lines.clear()
        code, _ = self.run_cmd(service)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.files(".txt")), 3)

    def test_a_save_that_does_not_read_back_leaves_no_files_behind(self):
        from unittest import mock

        folder = self.root / "out"
        with mock.patch.object(
            Path, "read_bytes", return_value=b"not what was written"
        ), self.assertRaises(de.DownloadEmailsError):
            de.save_message(
                folder, make_email(), "id1", date(2026, 10, 21), keep_eml=True
            )
        self.assertEqual(list(folder.iterdir()), [])

    def test_the_record_is_never_left_half_written(self):
        de.save_state({"a": {"file": "x"}}, self.state)
        self.assertEqual(
            json.loads(self.state.read_text()), {"downloaded": {"a": {"file": "x"}}}
        )
        self.assertEqual(
            [p.name for p in self.root.iterdir() if p.name.startswith(".downloaded-")],
            [],
        )

    def test_a_label_must_be_named_unless_listing_labels(self):
        import contextlib
        import io

        with (
            contextlib.redirect_stderr(io.StringIO()) as error,
            self.assertRaises(SystemExit) as stopped,
        ):
            de.run(["--out", str(self.out_dir)], service=self.gmail(1))
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("name the Gmail label", error.getvalue())

    def test_a_message_in_two_labels_is_downloaded_for_each(self):
        service = FakeGmail(
            {"Alerts": "Label_1", "Receipts": "Label_2"},
            {"id1": make_email(subject="Shared")},
        )
        self.run_cmd(service, label="Alerts")
        self.run_cmd(service, label="Receipts")
        self.assertEqual(len(list((self.out_dir / "Alerts").rglob("*.txt"))), 1)
        self.assertEqual(len(list((self.out_dir / "Receipts").rglob("*.txt"))), 1)
        self.lines.clear()
        _, text = self.run_cmd(service, label="Alerts")
        self.assertIn("1 already downloaded, 0 to do", text)

    def test_a_nested_label_gets_a_safe_folder_name(self):
        service = FakeGmail({"Parent/Child": "L"}, {"id1": make_email()})
        self.run_cmd(service, label="Parent/Child")
        self.assertTrue((self.out_dir / "Parent Child").is_dir())

    def test_nothing_is_ever_deleted_or_changed_in_gmail(self):
        # The tool asks Gmail for the read-only permission and has no way to change mail.
        self.assertEqual(de.SCOPES, ["https://www.googleapis.com/auth/gmail.readonly"])

    def test_a_second_run_downloads_nothing_and_says_how_to_start_over(self):
        service = self.gmail(3)
        self.run_cmd(service)
        self.lines.clear()
        code, text = self.run_cmd(service)
        self.assertEqual(code, 0)
        self.assertIn("3 already downloaded, 0 to do", text)
        self.assertIn("--all", text)

    def test_all_downloads_everything_again_after_the_files_were_deleted(self):
        service = self.gmail(3)
        self.run_cmd(service)
        for path in self.out_dir.rglob("*.txt"):
            path.unlink()
        self.assertEqual(self.files(".txt"), [])
        self.lines.clear()
        code, text = self.run_cmd(service, "--all")
        self.assertEqual(code, 0, text)
        self.assertEqual(len(self.files(".txt")), 3)
        self.assertIn("downloading 3 of them again (3 had been downloaded before)", text)
        self.assertIn("Done: 3 downloaded, 0 failed", text)

    def test_all_leaves_out_an_email_removed_from_the_label(self):
        service = self.gmail(3)
        self.run_cmd(service)
        for path in self.out_dir.rglob("*.txt"):
            path.unlink()
        del service.mail["id2"]  # the user removed one in Gmail
        self.lines.clear()
        code, text = self.run_cmd(service, "--all")
        self.assertEqual(code, 0, text)
        names = self.files(".txt")
        self.assertEqual(len(names), 2)
        self.assertFalse(any("Role number 2" in n for n in names))

    def test_all_only_affects_the_label_it_was_given(self):
        service = self.gmail(2)
        service.label_map["Other"] = "Label_2"
        self.run_cmd(service)  # Alerts
        self.run_cmd(service, label="Other")
        self.lines.clear()
        self.run_cmd(service, "--all")  # Alerts again
        self.lines.clear()
        _, text = self.run_cmd(service, label="Other")
        self.assertIn("2 already downloaded, 0 to do", text)

    def test_all_with_a_dry_run_changes_nothing(self):
        service = self.gmail(2)
        self.run_cmd(service)
        before = self.state.read_text()
        self.lines.clear()
        code, text = self.run_cmd(service, "--all", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(text.count("would download"), 2)
        self.assertEqual(self.state.read_text(), before)

    def test_all_with_a_limit_downloads_at_most_that_many(self):
        service = self.gmail(3)
        self.run_cmd(service)
        for path in self.out_dir.rglob("*.txt"):
            path.unlink()
        self.lines.clear()
        self.run_cmd(service, "--all", "--limit", "2")
        self.assertEqual(len(self.files(".txt")), 2)


if __name__ == "__main__":
    unittest.main()
