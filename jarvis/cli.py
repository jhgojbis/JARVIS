"""`jarvis setup | run | chat | doctor`"""
from __future__ import annotations
import argparse, os, shutil, sys
from pathlib import Path
from .config import Config


def ask(prompt, default=""):
    v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return v or default


def setup():
    """Interactive wizard: writes .env and config.yaml."""
    print("Jarvis setup - 6 questions.\n")
    cfg = Config.load()
    d, env = cfg.data, {}
    d["owner"]["name"] = ask("Your name", d["owner"]["name"])
    d["owner"]["phone"] = ask("Your mobile number (+countrycode...)")
    d["owner"]["whatsapp"] = d["owner"]["phone"]
    d["owner"]["timezone"] = ask("Timezone", d["owner"]["timezone"])
    if ask("Use your Claude Pro/Max login via the `claude` CLI instead of an API key? (y/n)", "n").lower() == "y":
        d["llm"]["backend"] = "claude_cli"
    else:
        env["ANTHROPIC_API_KEY"] = ask("Anthropic API key (console.anthropic.com)")
    env["TWILIO_ACCOUNT_SID"] = ask("Twilio Account SID")
    env["TWILIO_AUTH_TOKEN"] = ask("Twilio Auth Token")
    env["TWILIO_NUMBER"] = ask("Your Twilio phone number")
    env["TWILIO_WHATSAPP_NUMBER"] = "whatsapp:+14155238886"
    env["PUBLIC_URL"] = ask("Public URL (run `cloudflared tunnel --url http://localhost:8080`, paste the https URL)")
    env["JARVIS_CHAT_TOKEN"] = ask("Password for the browser chat page", os.urandom(6).hex())
    if ask("Connect email via IMAP app password? (y/n)", "n").lower() == "y":
        d["email"]["enabled"] = True
        env["EMAIL_ADDRESS"] = ask("Email address")
        env["EMAIL_APP_PASSWORD"] = ask("App password")
    ics = ask("Calendar secret iCal URL (blank to skip)")
    if ics:
        d["calendar"].update(enabled=True, ics_urls=[ics])
    vip = ask("VIP numbers that always ring you, comma separated (blank to skip)")
    d["screening"]["vip"] = [{"name": n.strip(), "number": n.strip()} for n in vip.split(",") if n.strip()]
    d["jobs"] = d["jobs"] or [
        {"name": "email_check", "action": "email_check", "every": "3h", "notify": "message"},
        {"name": "morning_briefing", "action": "briefing", "at": "08:00", "notify": "call"},
        {"name": "calendar_alert", "action": "calendar_alert", "every": "15m", "notify": "message"}]
    cfg.path = "config.yaml"
    cfg.save()
    Path(".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    os.chmod(".env", 0o600)
    print(f"""
Done. Start it:  jarvis run
Then in the Twilio console, for your number set:
  Voice  'A call comes in' -> {env['PUBLIC_URL']}/voice/incoming   (HTTP POST)
  WhatsApp sandbox 'When a message comes in' -> {env['PUBLIC_URL']}/whatsapp
Finally forward your real phone to {env['TWILIO_NUMBER']} (carrier code **61*number# = forward when unanswered).
Chat in browser: {env['PUBLIC_URL']}/""")


def doctor():
    cfg = Config.load()
    ok = lambda c, m: print(("OK   " if c else "FAIL ") + m)
    ok(Path("config.yaml").exists(), "config.yaml")
    ok(cfg["owner"]["phone"], "owner phone set")
    ok(cfg["llm"]["backend"] == "claude_cli" and shutil.which("claude") or Config.env("ANTHROPIC_API_KEY"), "LLM credentials")
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_NUMBER", "PUBLIC_URL"):
        ok(Config.env(k), k)


def main(argv=None):
    p = argparse.ArgumentParser(prog="jarvis")
    p.add_argument("cmd", choices=["setup", "run", "chat", "doctor"])
    p.add_argument("--port", type=int, default=8080)
    a = p.parse_args(argv)
    if a.cmd == "setup":
        return setup()
    if a.cmd == "doctor":
        return doctor()
    cfg = Config.load()
    if a.cmd == "chat":                       # text REPL, handy for testing without phone
        from .server import create_app
        brain = create_app(cfg).state.brain
        while (t := input("you> ").strip()) not in ("exit", "quit"):
            print("jarvis>", brain.chat("cli", t))
        return
    import uvicorn
    from .server import create_app
    uvicorn.run(create_app(cfg), host="0.0.0.0", port=a.port)


if __name__ == "__main__":
    sys.exit(main())
