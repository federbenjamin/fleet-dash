"""Durable receipts for native actions (invariant 76).

An `/api/act` call that reaches Claude's terminal is not repeatable: a lost
response cannot be distinguished from a lost request, so invariant 66 makes Fleet
say "delivery uncertain" and stop. That verdict was correct but empty — nothing
durable recorded what actually happened, so a phone that dropped its connection
mid-answer could never learn the outcome, not after a reload and not after a
daemon restart.

A receipt is that record. The browser mints a `client_request_id` BEFORE the
request (the whole point is surviving the loss of the response), the server binds
one receipt to it, and the outcome is written the moment `act()` knows it. The
same id replayed later returns the recorded outcome instead of writing a second
set of keys.

This is deliberately NOT the async worker the design document proposed. Measured
on staging 2026-07-24, a full `noop` act costs a median 3.0 ms through tmux
(1.7 ms min, n=12) against 296.1 ms through the applet, so the plan's "return in
~10 ms and poll a receipt" buys nothing once the terminal switch lands. Durability
was always the other half of that plan, and it stands on its own.
"""
import sqlite3
import time
import uuid


class ReceiptOps:

    # Operator decision 2026-07-24: receipts are internal and pruned after 24h.
    # They exist to answer "did my tap land?", a question with a short life.
    ACT_RECEIPT_TTL_SECONDS = 86_400
    ACT_RECEIPT_PRUNE_INTERVAL = 3_600
    # Every action that can move Claude's native surface. Read-only probes
    # (`ping`, `noop`, `focus`) and provider-side queue operations are excluded:
    # replaying them is harmless, so a receipt would only add write load.
    ACT_RECEIPT_TYPES = frozenset({
        "option", "multiq", "permission", "dismiss", "elicitation",
        "dismiss_then_send", "send_message", "text", "image_text",
        "handoff_text", "relay", "interrupt", "close",
        "session_settings", "permission_mode",
    })
    ACT_RECEIPT_STATES = ("running", "delivered", "failed", "uncertain")

    @staticmethod
    def _valid_client_request_id(value):
        """Client-supplied and therefore bounded before it reaches SQL."""
        text = str(value or "")
        return text if 8 <= len(text) <= 128 and all(
            character.isalnum() or character in "-_:." for character in text) else None

    def _receipt_db(self):
        db = self.ledger_reader()
        db.execute("""CREATE TABLE IF NOT EXISTS act_receipts(
            receipt_id TEXT PRIMARY KEY,
            client_request_id TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL, action_type TEXT NOT NULL,
            state TEXT NOT NULL, code TEXT, error TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        db.execute("""CREATE INDEX IF NOT EXISTS act_receipts_created
            ON act_receipts(created_at)""")
        db.commit()
        return db

    @staticmethod
    def _receipt_row(row):
        return {"receipt_id": row[0], "client_request_id": row[1],
                "session_id": row[2], "action_type": row[3], "state": row[4],
                "code": row[5] or "", "error": row[6] or "",
                "created_at": row[7], "updated_at": row[8]}

    def begin_act_receipt(self, action):
        """Claim a receipt for this action, or replay the one already recorded.

        Returns `(receipt_id, replay)`. A non-None `replay` is the answer to
        return WITHOUT touching the terminal: either the recorded outcome of an
        identical earlier request, or an in-flight marker. `receipt_id` is None
        when the action is not receipt-eligible, which leaves `act()` exactly as
        it behaved before.
        """
        request_id = self._valid_client_request_id(action.get("client_request_id"))
        action_type = str(action.get("type") or "")
        if not request_id or action_type not in self.ACT_RECEIPT_TYPES:
            return None, None
        receipt_id = f"rcpt-{uuid.uuid4().hex}"
        now = time.time()
        db = None
        try:
            db = self._receipt_db()
            try:
                db.execute(
                    "INSERT INTO act_receipts(receipt_id, client_request_id, "
                    "session_id, action_type, state, created_at, updated_at) "
                    "VALUES(?,?,?,?,'running',?,?)",
                    (receipt_id, request_id, str(action.get("session_id") or ""),
                     action_type, now, now))
                db.commit()
                return receipt_id, None
            except sqlite3.IntegrityError:
                row = db.execute(
                    "SELECT receipt_id, client_request_id, session_id, action_type, "
                    "state, code, error, created_at, updated_at FROM act_receipts "
                    "WHERE client_request_id=?", (request_id,)).fetchone()
                if not row:                       # raced away; treat as unrecorded
                    return None, None
                return row[0], self._receipt_replay(self._receipt_row(row))
        except sqlite3.Error:
            # A receipt is an improvement on the old behaviour, never a gate on
            # it: a storage failure must not stop the user answering a prompt.
            return None, None
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _receipt_replay(receipt):
        """The answer for a `client_request_id` Fleet has already acted on."""
        if receipt["state"] == "running":
            return {"ok": False, "code": "in_flight", "receipt_id": receipt["receipt_id"],
                    "receipt_state": "running", "duplicate": True,
                    "error": "this action is still being delivered"}
        if receipt["state"] == "delivered":
            return {"ok": True, "receipt_id": receipt["receipt_id"],
                    "receipt_state": "delivered", "duplicate": True,
                    "replayed": True}
        return {"ok": False, "code": receipt["code"] or (
                    "delivery_uncertain" if receipt["state"] == "uncertain" else "failed"),
                "receipt_id": receipt["receipt_id"], "receipt_state": receipt["state"],
                "duplicate": True, "replayed": True,
                "error": receipt["error"] or "this action already failed"}

    # act() has already made invariant 66's call by the time it returns: it
    # labels a possibly-delivered failure with one of these codes and every
    # other refusal is one it made BEFORE touching the transport (an empty text,
    # a stale nonce, a capability gate). Re-deriving the verdict from the
    # transport classifier here would mislabel all of those as uncertain.
    ACT_RECEIPT_UNCERTAIN_CODES = frozenset({
        "delivery_uncertain", "control_delivery_uncertain", "action_raised"})

    def resolve_act_receipt(self, receipt_id, result):
        """Record what happened, taking act()'s own verdict at face value."""
        if not receipt_id:
            return result
        if result.get("ok"):
            state = "delivered"
        elif result.get("code") in self.ACT_RECEIPT_UNCERTAIN_CODES:
            state = "uncertain"      # keys may have landed; never repeatable
        else:
            state = "failed"
        db = None
        try:
            db = self._receipt_db()
            db.execute("UPDATE act_receipts SET state=?, code=?, error=?, updated_at=? "
                       "WHERE receipt_id=?",
                       (state, str(result.get("code") or "")[:64],
                        str(result.get("error") or "")[:500], time.time(), receipt_id))
            db.commit()
        except sqlite3.Error:
            return {**result, "receipt_id": receipt_id, "receipt_state": state,
                    "receipt_durable": False}
        finally:
            if db is not None:
                db.close()
        return {**result, "receipt_id": receipt_id, "receipt_state": state}

    def act_receipt(self, client_request_id):
        """Look one receipt up — how a reconnected browser learns the outcome."""
        request_id = self._valid_client_request_id(client_request_id)
        if not request_id:
            return {"ok": False, "error": "invalid request id"}
        db = None
        try:
            db = self._receipt_db()
            row = db.execute(
                "SELECT receipt_id, client_request_id, session_id, action_type, "
                "state, code, error, created_at, updated_at FROM act_receipts "
                "WHERE client_request_id=?", (request_id,)).fetchone()
        except sqlite3.Error as error:
            return {"ok": False, "error": f"receipt lookup failed: {error}"}
        finally:
            if db is not None:
                db.close()
        if not row:
            # Pruned after 24h, or the request never reached the daemon. Those
            # are different facts and the client must not conflate them, so say
            # which one this is rather than inventing an outcome.
            return {"ok": True, "found": False,
                    "reason": "no receipt: the request never reached Fleet, or it "
                              "is older than 24 hours"}
        return {"ok": True, "found": True, "receipt": self._receipt_row(row)}

    def prune_act_receipts(self, now=None):
        """Drop receipts past their retention window. Cheap and rate-limited."""
        now = time.time() if now is None else now
        if now < self._act_receipt_prune_due:
            return 0
        self._act_receipt_prune_due = now + self.ACT_RECEIPT_PRUNE_INTERVAL
        db = None
        try:
            db = self._receipt_db()
            cursor = db.execute("DELETE FROM act_receipts WHERE created_at < ?",
                                (now - self.ACT_RECEIPT_TTL_SECONDS,))
            db.commit()
            return cursor.rowcount or 0
        except sqlite3.Error:
            return 0
        finally:
            if db is not None:
                db.close()
