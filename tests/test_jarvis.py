import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient
from jarvis.brain import Brain
from jarvis.config import Config, DEFAULTS, _merge, norm_number
from jarvis.connectors.calendar import parse_events
from jarvis.connectors.email import parse_message
from jarvis.llm import LLM, BudgetExceeded
from jarvis.scheduler import Scheduler, in_quiet_hours, is_due, parse_every
from jarvis.server import create_app
from jarvis.store import Store


class FakeLLM:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def ask_json(self, system, messages, max_tokens=None):
        self.calls.append((system, messages))
        return self.replies.pop(0) if self.replies else {}

    ask = lambda self, *a, **k: "Good morning."


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def message(self, t): self.sent.append(("message", t))
    def send(self, how, t): self.sent.append((how, t))


def make(tmp_path, llm=None, **over):
    cfg = Config(_merge(DEFAULTS, {"owner": {"name": "Sam", "phone": "+15551112222", "whatsapp": "+15551112222"},
                                   "screening": {"vip": [{"name": "Mom", "number": "+1 (555) 000-1111"}]}, **over}),
                 str(tmp_path / "config.yaml"))
    store, n = Store(str(tmp_path / "j.db")), FakeNotifier()
    app = create_app(cfg, store, llm or FakeLLM(), n, verify_twilio=False)
    return cfg, store, n, TestClient(app)


def test_norm():
    assert norm_number("whatsapp:+1 (555) 000-1111") == "+15550001111"


def test_vip_goes_straight_to_owner(tmp_path):
    _, store, _, c = make(tmp_path)
    r = c.post("/voice/incoming", data={"From": "+15550001111"})
    assert "<Dial" in r.text and "+15551112222" in r.text and "<Gather" not in r.text
    assert store.calls_since(0)[0]["outcome"] == "vip"


def test_unknown_caller_is_questioned(tmp_path):
    _, _, _, c = make(tmp_path)
    r = c.post("/voice/incoming", data={"From": "+19998887777"})
    assert "<Gather" in r.text and "Who is calling" in r.text and "<Dial" not in r.text


def test_urgent_is_connected_with_whisper(tmp_path):
    _, _, _, c = make(tmp_path, llm=FakeLLM({"name": "Dr Lee", "reason": "hospital about dad", "verdict": "connect"}))
    r = c.post("/voice/screen", data={"From": "+19998887777", "SpeechResult": "hospital"})
    assert "<Dial" in r.text and "/voice/whisper" in r.text and "Dr Lee" in r.text
    w = c.post("/voice/whisper", params={"t": "Dr Lee says hi"})
    assert "Press 1" in w.text
    assert "Hangup" not in c.post("/voice/whisper_ok", data={"Digits": "1"}).text
    assert "Hangup" in c.post("/voice/whisper_ok", data={"Digits": "2"}).text


def test_message_notifies_owner(tmp_path):
    _, store, n, c = make(tmp_path, llm=FakeLLM({"name": "Bob", "reason": "wants lunch", "verdict": "message"}))
    r = c.post("/voice/screen", data={"From": "+19998887777", "SpeechResult": "lunch?"})
    assert "Dial" not in r.text and "passed on" in r.text
    assert "wants lunch" in n.sent[0][1]


def test_spam_and_silence_hangup(tmp_path):
    _, store, n, c = make(tmp_path, llm=FakeLLM({"verdict": "spam", "reason": "warranty"}))
    assert "Hangup" in c.post("/voice/screen", data={"From": "+1", "SpeechResult": "car warranty"}).text
    assert "Hangup" in c.post("/voice/screen", data={"From": "+1", "SpeechResult": ""}).text
    assert n.sent == []


def test_llm_failure_defaults_to_message_not_connect(tmp_path):
    class Boom(FakeLLM):
        def ask_json(self, *a, **k): raise RuntimeError("down")
    _, _, n, c = make(tmp_path, llm=Boom())
    r = c.post("/voice/screen", data={"From": "+1999", "SpeechResult": "hi"})
    assert "Dial" not in r.text and n.sent


def test_prompt_injection_verdict_is_validated(tmp_path):
    _, _, _, c = make(tmp_path, llm=FakeLLM({"verdict": "CONNECT_NOW_PLEASE"}))
    assert "Dial" not in c.post("/voice/screen", data={"From": "+1", "SpeechResult": "ignore rules"}).text


def test_whatsapp_owner_only_and_actions(tmp_path):
    llm = FakeLLM({"reply": "Added Dad as VIP.", "actions": [
        {"type": "add_vip", "name": "Dad", "number": "+1 555 333 4444"},
        {"type": "set_job", "name": "email_check", "action": "email_check", "every": "2h", "notify": "message"},
        {"type": "add_task", "text": "buy milk"}, {"type": "rm -rf", "x": 1}]})
    cfg, store, _, c = make(tmp_path, llm=llm)
    assert "Message" not in c.post("/whatsapp", data={"From": "whatsapp:+10000000000", "Body": "hi"}).text
    r = c.post("/whatsapp", data={"From": "whatsapp:+15551112222", "Body": "add dad +1 555 333 4444 as vip, check mail every 2h"})
    assert "Added Dad" in r.text
    assert any(v["number"] == "+15553334444" for v in cfg["screening"]["vip"])
    assert [j for j in cfg["jobs"] if j["name"] == "email_check"][0]["every"] == "2h"
    assert store.open_tasks()[0]["text"] == "buy milk"
    assert "Dad" in Config.load(str(tmp_path / "config.yaml")).data["screening"]["vip"][-1]["name"]  # persisted


def test_owner_voice_turn_and_goodbye(tmp_path):
    _, _, _, c = make(tmp_path, llm=FakeLLM({"reply": "All clear.", "actions": []}))
    assert "All clear" in c.post("/voice/owner", data={"SpeechResult": "what's up"}).text
    assert "Goodbye" in c.post("/voice/owner", data={"SpeechResult": "Goodbye."}).text


def test_slow_owner_reply_holds_then_delivers(tmp_path, monkeypatch):
    import time as _t
    from jarvis import server
    class SlowLLM(FakeLLM):
        def ask_json(self, *a, **k):
            _t.sleep(0.3)
            return super().ask_json(*a, **k)
    monkeypatch.setattr(server, "FAST_WAIT", 0.01)
    _, _, _, c = make(tmp_path, llm=SlowLLM({"reply": "Invoice paid.", "actions": []}))
    with c:
        first = c.post("/voice/owner", data={"CallSid": "CA1", "SpeechResult": "read my email"}).text
        assert "One moment" in first and "/voice/owner_wait" in first
        assert "Invoice paid" in c.post("/voice/owner_wait", data={"CallSid": "CA1"}).text
        assert "lost my train" in c.post("/voice/owner_wait", data={"CallSid": "CA1"}).text


def test_owner_calling_in_gets_owner_mode_and_pin(tmp_path, monkeypatch):
    _, _, _, c = make(tmp_path)
    monkeypatch.delenv("JARVIS_PHONE_PIN", raising=False)
    r = c.post("/voice/incoming", data={"From": "+1 555 111 2222"}).text
    assert "/voice/owner" in r and "/voice/screen" not in r
    monkeypatch.setenv("JARVIS_PHONE_PIN", "4711")
    assert "/voice/owner_pin" in c.post("/voice/incoming", data={"From": "+15551112222"}).text
    assert "/voice/owner" in c.post("/voice/owner_pin", data={"Digits": "4711"}).text
    assert "Hangup" in c.post("/voice/owner_pin", data={"Digits": "0000"}).text


def test_web_chat_needs_token(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_CHAT_TOKEN", "s3cret")
    _, _, _, c = make(tmp_path, llm=FakeLLM({"reply": "hello"}))
    assert c.post("/chat", json={"text": "hi", "token": "bad"}).status_code == 401
    assert c.post("/chat", json={"text": "hi", "token": "s3cret"}).json()["reply"] == "hello"
    assert "Sam" not in c.get("/").text and "Jarvis" in c.get("/").text


def test_twilio_signature_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "x")
    monkeypatch.setenv("PUBLIC_URL", "https://e.com")
    cfg = Config(_merge(DEFAULTS, {}))
    c = TestClient(create_app(cfg, Store(str(tmp_path / "s.db")), FakeLLM(), FakeNotifier(), verify_twilio=True))
    assert c.post("/voice/incoming", data={"From": "+1"}).status_code == 403


def test_scheduler_rules():
    tz = ZoneInfo("UTC")
    now = datetime(2026, 1, 5, 9, 0, tzinfo=tz)
    assert parse_every("3h") == 10800 and parse_every("15m") == 900
    with pytest.raises(ValueError): parse_every("soon")
    assert is_due({"every": "3h"}, now.timestamp() - 4 * 3600, now)
    assert not is_due({"every": "3h"}, now.timestamp() - 3600, now)
    assert is_due({"at": "08:00"}, (now - timedelta(days=1)).timestamp(), now)
    assert not is_due({"at": "08:00"}, (now - timedelta(hours=1)).timestamp(), now)
    cfg = Config(_merge(DEFAULTS, {}))
    assert in_quiet_hours(cfg, now.replace(hour=23)) and in_quiet_hours(cfg, now.replace(hour=3)) and not in_quiet_hours(cfg, now)


def test_scheduler_tick_notifies_only_when_something_to_say(tmp_path):
    cfg, store, n, _ = make(tmp_path, jobs=[{"name": "b", "action": "briefing", "at": "08:00", "notify": "call"},
                                            {"name": "e", "action": "email_check", "every": "3h"}])
    from jarvis.skills import Skills
    s = Scheduler(cfg, store, Skills(cfg, store, FakeLLM()), n)
    now = datetime(2026, 1, 5, 9, 0, tzinfo=ZoneInfo("UTC"))
    assert len(s.tick(now)) == 1 and n.sent[0][0] == "call"    # email disabled -> silent
    assert s.tick(now) == []                                    # not re-run
    assert s.tick(now.replace(hour=23)) == []                   # quiet hours


def test_budget_blocks_llm(tmp_path):
    cfg = Config(_merge(DEFAULTS, {"llm": {"daily_token_budget": 10}}))
    store = Store(str(tmp_path / "b.db"))
    store.add_tokens(10)
    with pytest.raises(BudgetExceeded):
        LLM(cfg, store).ask("s", "hi")


def test_email_and_calendar_parsing():
    raw = b"From: =?utf-8?q?Ana?= <ana@x.com>\nSubject: Invoice due\nMessage-ID: <1@x>\nContent-Type: text/plain\n\nPlease pay by Friday."
    m = parse_message(raw)
    assert m["subject"] == "Invoice due" and "Friday" in m["snippet"] and "ana@x.com" in m["from"]
    ics = "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:1\nDTSTART:20260105T100000Z\nDTEND:20260105T110000Z\nRRULE:FREQ=DAILY;COUNT=3\nSUMMARY:Standup\nEND:VEVENT\nEND:VCALENDAR\n"
    s = datetime(2026, 1, 5, 0, 0, tzinfo=ZoneInfo("UTC"))
    assert [e["title"] for e in parse_events(ics, s, s + timedelta(days=2))] == ["Standup", "Standup"]


# ---------------- alerts: call inside the window, WhatsApp voice clip otherwise / when unanswered ----------------
from jarvis import tts
from jarvis.connectors import email as mailmod
from jarvis.notify import Notifier, can_call
from jarvis.skills import Skills

TZ = ZoneInfo("Europe/Stockholm")
MON_10 = datetime(2026, 10, 5, 10, 0, tzinfo=TZ)
MON_0830 = datetime(2026, 10, 5, 8, 30, tzinfo=TZ)
MON_2359 = datetime(2026, 10, 5, 23, 59, tzinfo=TZ)
SAT_10 = datetime(2026, 10, 3, 10, 0, tzinfo=TZ)


class FakeTwilio:
    def __init__(self):
        self.made, self.sent = [], []
        me = self

        class Calls:
            def create(self, **kw):
                me.made.append(kw)
                return type("C", (), {"sid": "CA123"})()

        class Msgs:
            def create(self, **kw):
                me.sent.append(kw)
                return type("M", (), {"sid": f"SM{len(me.sent)}"})()
        self.calls, self.messages = Calls(), Msgs()


@pytest.fixture
def alerts(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "https://j.example")
    monkeypatch.setenv("TWILIO_NUMBER", "+15550009999")
    monkeypatch.setenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
    monkeypatch.setattr(tts, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(tts, "_synth", lambda text, voice, path: path.write_bytes(b"mp3"))
    cfg, store, _, _ = make(tmp_path, voice={"engine": "thomas"})
    tw = FakeTwilio()
    return cfg, store, tw, Notifier(cfg, tw, store)


def test_call_window(tmp_path):
    cfg, *_ = make(tmp_path)
    assert can_call(cfg, MON_10) and can_call(cfg, MON_2359) and can_call(cfg, SAT_10)
    assert not can_call(cfg, MON_0830) and not can_call(cfg, datetime(2026, 10, 6, 0, 0, tzinfo=TZ))
    cfg["calls"]["weekdays_only"] = True
    assert can_call(cfg, MON_10) and not can_call(cfg, SAT_10)


def test_alert_calls_inside_window_and_remembers_text(alerts):
    cfg, store, tw, n = alerts
    assert n.alert("Sam, an email needs you. Anna: invoice due Friday", MON_10) == "call"
    call = tw.made[0]
    assert call["to"] == "+15551112222" and call["status_callback"] == "https://j.example/voice/call_status"
    assert "<Play>https://j.example/audio/" in call["twiml"] and not tw.sent
    assert store.get("alert:CA123") == "Sam, an email needs you. Anna: invoice due Friday"


def test_alert_outside_window_sends_voice_clip_not_call(alerts):
    _, _, tw, n = alerts
    assert n.alert("Invoice due", MON_0830) == "clip"
    assert not tw.made
    m = tw.sent[0]
    assert m["to"] == "whatsapp:+15551112222" and m["body"] == "Invoice due"
    assert m["media_url"][0].startswith("https://j.example/audio/") and m["media_url"][0].endswith(".mp3")


@pytest.mark.parametrize("status,duration,clip", [("no-answer", 0, True), ("busy", 0, True), ("failed", 0, True),
                                                  ("completed", 4, True), ("completed", 40, False)])
def test_unheard_alert_call_falls_back_to_clip(alerts, status, duration, clip):
    _, store, tw, n = alerts
    n.alert("Invoice due", MON_10)
    assert n.call_finished("CA123", status, duration) is clip
    assert len(tw.sent) == int(clip)
    assert n.call_finished("CA123", status, duration) is False      # one clip per alert, never two


def test_unknown_call_sid_is_ignored(alerts):
    _, _, tw, n = alerts
    assert n.call_finished("CA-other", "no-answer", 0) is False and not tw.sent


def test_call_status_webhook_triggers_clip(tmp_path):
    class N(FakeNotifier):
        def call_finished(self, sid, status, dur): self.sent.append((sid, status, dur)); return True
    cfg, _, _, _ = make(tmp_path)
    n = N()
    c = TestClient(create_app(cfg, Store(str(tmp_path / "x.db")), FakeLLM(), n, verify_twilio=False))
    assert c.post("/voice/call_status", data={"CallSid": "CA9", "CallStatus": "no-answer", "CallDuration": "0"}).status_code == 204
    assert n.sent == [("CA9", "no-answer", 0)]


def test_voice_falls_back_to_polly_when_tts_breaks(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(tts, "_synth", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    cfg, *_ = make(tmp_path, voice={"engine": "thomas"})
    assert "<Say" in tts.tag(cfg, "hello") and "<Play" not in tts.tag(cfg, "hello")
    cfg["voice"]["engine"] = "polly"
    assert "<Say" in tts.tag(cfg, "hello")


def test_audio_route_serves_only_rendered_files(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "AUDIO_DIR", tmp_path / "audio")
    monkeypatch.setattr(tts, "_synth", lambda text, voice, path: path.write_bytes(b"ID3"))
    cfg, _, _, c = make(tmp_path, voice={"engine": "luke"})
    name = tts.render(cfg, "Secret summary")
    assert c.get(f"/audio/{name}").content == b"ID3"
    assert c.get("/audio/" + "0" * 32 + ".mp3").status_code == 404
    assert c.get("/audio/..%2Fconfig.yaml").status_code == 404
    assert c.get("/audio/notahash.mp3").status_code == 404


def test_own_number_calling_back_is_hung_up(tmp_path, monkeypatch):
    monkeypatch.setenv("TWILIO_NUMBER", "+15550009999")
    _, store, _, c = make(tmp_path)
    r = c.post("/voice/incoming", data={"From": "+1 555 000 9999"})
    assert "Hangup" in r.text and "Gather" not in r.text and "Dial" not in r.text


# ---------------- email triage and the digest read out when the owner phones in ----------------
MAILS = [{"id": "<1>", "from": "Anna Lind <anna@x.se>", "subject": "Faktura 4411", "snippet": "Förfaller fredag, 1200 kr"},
         {"id": "<2>", "from": "Shop <news@shop.com>", "subject": "Sale!", "snippet": "50% off"}]


def mail_skills(tmp_path, monkeypatch, llm, mails=MAILS):
    cfg, store, _, _ = make(tmp_path, email={"enabled": True})
    monkeypatch.setattr(mailmod, "fetch_unread", lambda cfg, *a, **k: list(mails))
    monkeypatch.setattr(mailmod, "fetch_recent", lambda cfg, *a, **k: list(mails))
    return cfg, store, Skills(cfg, store, llm)


def test_triage_keeps_only_mail_that_needs_a_reaction_with_reason(tmp_path, monkeypatch):
    _, store, sk = mail_skills(tmp_path, monkeypatch, FakeLLM({"important": [{"i": 0, "why": "invoice 1200 kr due Friday"}]}))
    items = sk.important_email()
    assert [m["id"] for m in items] == ["<1>"] and items[0]["why"] == "invoice 1200 kr due Friday"
    assert sk.email_text(items) == "Anna Lind: invoice 1200 kr due Friday"
    assert sk.important_email() == []                                   # already triaged: nothing new


def test_triage_still_accepts_plain_indices(tmp_path, monkeypatch):
    _, _, sk = mail_skills(tmp_path, monkeypatch, FakeLLM({"important": [1, "x", 7]}))
    assert [m["id"] for m in sk.important_email()] == ["<2>"]


def test_digest_rechecks_everything_unread_even_if_already_alerted(tmp_path, monkeypatch):
    _, store, sk = mail_skills(tmp_path, monkeypatch,
                               FakeLLM({"important": [{"i": 0, "why": "invoice due Friday"}]}, {"important": [{"i": 0, "why": "invoice due Friday"}]}))
    sk.important_email()                                                 # the scheduler already alerted about it
    d = sk.digest()
    assert "1 email needs you" in d and "First, Anna Lind: invoice due Friday." in d and d.endswith("What can I do for you?")


def test_digest_all_clear_and_mail_down(tmp_path, monkeypatch):
    _, _, sk = mail_skills(tmp_path, monkeypatch, FakeLLM({"important": []}))
    assert "Nothing in your inbox needs you" in sk.digest()
    monkeypatch.setattr(mailmod, "fetch_unread", lambda *a, **k: (_ for _ in ()).throw(OSError("imap down")))
    assert "could not reach your email" in sk.digest()


def test_owner_phoning_in_hears_the_digest_first(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_PHONE_PIN", raising=False)
    cfg, store, _, c = make(tmp_path, email={"enabled": True},
                            llm=FakeLLM({"important": [{"i": 0, "why": "invoice due Friday"}]}))
    monkeypatch.setattr(mailmod, "fetch_unread", lambda cfg, *a, **k: list(MAILS))
    monkeypatch.setattr(mailmod, "fetch_recent", lambda cfg, *a, **k: list(MAILS))
    with c:
        r = c.post("/voice/incoming", data={"From": "+15551112222", "CallSid": "CA5"}).text
    assert "Anna Lind: invoice due Friday" in r and "/voice/owner" in r
    assert "Anna Lind" in store.recent_history("voice")[-1]["content"]


def test_scheduler_alert_job_uses_alert_channel(tmp_path, monkeypatch):
    cfg, store, sk = mail_skills(tmp_path, monkeypatch, FakeLLM({"important": [{"i": 0, "why": "invoice due Friday"}]}))
    cfg.data["jobs"] = [{"name": "email_check", "action": "email_check", "every": "5m", "notify": "alert"}]
    cfg["owner"]["quiet_hours"] = ["00:00", "09:00"]
    n = FakeNotifier()
    s = Scheduler(cfg, store, sk, n)
    assert s.tick(MON_0830) == []                                        # quiet until 09:00, mail waits
    assert s.tick(MON_10) and n.sent[0][0] == "alert" and "invoice due Friday" in n.sent[0][1]


# ---------------- "send me a summary on WhatsApp" by voice ----------------
def brain_with(tmp_path, llm, notifier):
    cfg, store, _, _ = make(tmp_path)
    return Brain(cfg, store, llm, Skills(cfg, store, llm), notifier)


def test_voice_can_send_whatsapp_to_owner_only(tmp_path):
    from jarvis.brain import Brain  # noqa: F401
    sent = []

    class N(FakeNotifier):
        def message(self, t, media=None): sent.append(("text", t))
        def voice_clip(self, t): sent.append(("clip", t))
    b = brain_with(tmp_path, FakeLLM(
        {"reply": "Sent, Boss.", "actions": [{"type": "send_whatsapp", "text": "Summary: Google invoice, Apple invoice.", "to": "+15559998888"}]},
        {"reply": "Sent with audio.", "actions": [{"type": "send_whatsapp", "text": "Again", "voice": True}]}), N())
    assert b.chat("voice", "send a summary to my whatsapp") == "Sent, Boss."
    assert b.chat("voice", "and as audio") == "Sent with audio."
    assert sent == [("text", "Summary: Google invoice, Apple invoice."), ("clip", "Again")]


def test_failed_whatsapp_is_not_reported_as_sent(tmp_path):
    class N(FakeNotifier):
        def message(self, t, media=None): raise RuntimeError("63016 outside the 24h window")
    b = brain_with(tmp_path, FakeLLM({"reply": "Sent, Boss.", "actions": [{"type": "send_whatsapp", "text": "x"}]}), N())
    r = b.chat("voice", "whatsapp me")
    assert "Sent" not in r and "refused" in r


# ---------------- undelivered WhatsApp -> SMS fallback ----------------
def test_undelivered_whatsapp_is_resent_as_sms_with_audio_link(alerts):
    _, store, tw, n = alerts
    n.voice_clip("Invoice due Friday")
    assert tw.sent[0]["to"].startswith("whatsapp:") and tw.sent[0]["status_callback"] == "https://j.example/whatsapp/status"
    assert n.whatsapp_status("SM1", "undelivered") is True
    sms = tw.sent[1]
    assert sms["to"] == "+15551112222" and sms["from_"] == "+15550009999"
    assert sms["body"].startswith("Invoice due Friday Listen: https://j.example/audio/")
    assert n.whatsapp_status("SM1", "undelivered") is False          # only once


def test_delivered_whatsapp_sends_no_sms(alerts):
    _, _, tw, n = alerts
    n.message("hello")
    assert n.whatsapp_status("SM1", "delivered") is False and len(tw.sent) == 1
    assert n.whatsapp_status("SM-unknown", "failed") is False


def test_whatsapp_status_webhook(tmp_path):
    class N(FakeNotifier):
        def whatsapp_status(self, sid, status): self.sent.append((sid, status)); return True
    cfg, _, _, _ = make(tmp_path)
    n = N()
    c = TestClient(create_app(cfg, Store(str(tmp_path / "y.db")), FakeLLM(), n, verify_twilio=False))
    assert c.post("/whatsapp/status", data={"MessageSid": "SM9", "MessageStatus": "undelivered"}).status_code == 204
    assert n.sent == [("SM9", "undelivered")]
