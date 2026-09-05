"""Prove the alerting plumbing works before any alerting logic exists.

Three things have to be true before pipeline A is worth writing, and all three
are credentials-and-network problems rather than logic problems:

    1. the service account can read Firestore    (favourites live there)
    2. the service account can write Firestore   (the ledger will live there)
    3. the bot can post to the chat              (nothing is worth computing
                                                  if it cannot be delivered)

This checks all three and reports the result to Telegram, so a green run is
visible on the phone rather than only in the Actions log.

Nothing here reads a secret from anywhere but the environment. The service
account key arrives as a file path in GOOGLE_APPLICATION_CREDENTIALS, written
by the workflow from a repository secret and never committed.

The write test deliberately uses its own throwaway document under
``alerts_selfcheck``. It never touches ``favproducts_*`` or ``settings_*``:
those are a real shopping list two people use, and a self-check has no business
writing to them.
"""

import datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from google.cloud import firestore

ROOM = os.environ.get("ALERTS_ROOM", "our-groceries")
SELFCHECK_COLLECTION = "alerts_selfcheck"


def telegram(text):
    """Post to the chat. Returns None on success, else a reason."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is not set"

    body = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=30) as r:
            if r.status != 200:
                return f"Telegram returned HTTP {r.status}"
    except urllib.error.HTTPError as exc:
        # str(exc) embeds the request URL, and the token is in the URL - so it
        # is never printed. The JSON body is Telegram's own diagnosis
        # ("chat not found", "Unauthorized") and carries no secret, so that is
        # what gets reported. Without it every failure looks identical.
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = "(no response body)"
        return f"HTTP {exc.code} — {detail}"
    except Exception as exc:
        return f"request failed: {type(exc).__name__}"
    return None


def diagnose_chat():
    """Work out *why* the chat was not found, and say what to do about it.

    "chat not found" has two quite different causes and the fix differs: either
    the bot has never been spoken to - a bot cannot open a conversation, the
    human has to send the first message - or the configured id belongs to some
    other chat.

    ``getUpdates`` distinguishes them. Printing the ids it finds is safe: an id
    equal to the configured secret is masked by Actions anyway, and an id that
    is *not* masked is exactly the one that should have been configured.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        with urllib.request.urlopen(url, timeout=30) as r:
            payload = json.load(r)
    except Exception as exc:
        return f"   could not read getUpdates to diagnose ({type(exc).__name__})"

    chats = {}
    for update in payload.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if chat:
                chats[chat.get("id")] = chat

    if not chats:
        return ("   The bot has no conversations at all. A bot cannot start one -\n"
                "   open t.me/groceries_notifier_bot, press Start, send it any\n"
                "   message, then re-run this check.")

    lines = [f"   The bot knows {len(chats)} chat(s), and TELEGRAM_CHAT_ID matches none of them:"]
    for cid, chat in chats.items():
        who = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
        lines.append(f"     id={cid}  type={chat.get('type')}  {who}")
    lines.append("   Set TELEGRAM_CHAT_ID to the id above (group ids are negative).")
    return "\n".join(lines)


def main():
    checks = []
    ok = True

    # ---- 1. read -------------------------------------------------------
    try:
        db = firestore.Client()
        favs = list(db.collection(f"favproducts_{ROOM}").stream())
        stores = db.collection(f"settings_{ROOM}").document("prefs").get()
        n_stores = len((stores.to_dict() or {}).get("favouriteStores", [])) if stores.exists else 0
        with_barcode = sum(1 for d in favs if (d.to_dict() or {}).get("barcode"))
        checks.append(f"✅ Firestore read — {len(favs)} favourites "
                      f"({with_barcode} with a barcode), {n_stores} branches")
    except Exception as exc:
        ok = False
        checks.append(f"❌ Firestore read failed — {type(exc).__name__}: {exc}")
        # Without a client there is nothing further to test on that side.
        db = None

    # ---- 2. write ------------------------------------------------------
    if db is not None:
        try:
            ref = db.collection(SELFCHECK_COLLECTION).document("last_run")
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
            ref.set({"at": stamp, "by": "alerts_selfcheck"})
            written = (ref.get().to_dict() or {}).get("at")
            if written != stamp:
                raise RuntimeError("value read back did not match what was written")
            checks.append("✅ Firestore write — round-tripped a document")
        except Exception as exc:
            ok = False
            checks.append(f"❌ Firestore write failed — {type(exc).__name__}: {exc}")

    # ---- 3. deliver ----------------------------------------------------
    head = "🔔 <b>Alert plumbing self-check</b>" if ok else "⚠️ <b>Alert plumbing self-check</b>"
    message = head + "\n\n" + "\n".join(checks)
    problem = telegram(message)
    if problem:
        # Telegram is the only check that cannot report its own failure.
        print(f"❌ Telegram delivery failed — {problem}", file=sys.stderr)
        if "chat not found" in problem.lower():
            print(diagnose_chat(), file=sys.stderr)
        for line in checks:
            print(line, file=sys.stderr)
        return 1

    print("✅ Telegram delivery — message sent")
    for line in checks:
        print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
