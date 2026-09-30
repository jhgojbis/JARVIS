# Jarvis – a personal voice assistant that screens your calls

Jarvis answers your phone when you can't, **rings you straight through for VIPs**, questions everyone else,
checks your email/calendar on a schedule, **phones or WhatsApps you only when it matters**, and you can talk to it
(phone, WhatsApp or browser) to reconfigure it: *"Jarvis, add Dad as VIP and check my mail every 2 hours."*

## How it works
```
caller ─► your phone ─(forward)─► Twilio number ─► Jarvis server
                                   VIP?  ─yes─► rings your phone directly
                                    │no
                        "Who is calling and why?" ─► Claude Haiku judges (1 call, ~150 tokens)
                          urgent ─► rings you with a whisper ("Dr Lee: hospital. Press 1")
                          else   ─► takes a message, WhatsApps you │ spam ─► hangs up
scheduler: email (3h) · calendar alerts (15m) · 08:00 briefing call ─► only speaks up if there's something to say
```

## Setup (5 steps)
1. `pip install -e .` (Python 3.10+)
2. Get a **Twilio** account + voice number (~$1/mo + ~$0.01/min) and an **Anthropic API key** (or choose the `claude_cli` backend to use your Claude Pro/Max login).
3. `jarvis setup` – answers 6–9 questions, writes `.env` + `config.yaml`.
4. `cloudflared tunnel --url http://localhost:8080` (or ngrok), then `jarvis run`. Paste the tunnel URL into Twilio → *Voice: a call comes in* = `<URL>/voice/incoming`, WhatsApp sandbox webhook = `<URL>/whatsapp`.
5. Forward your phone to the Twilio number (conditional forwarding `**61*<number>#` = when unanswered; `**21*<number>#` = always). Check with `jarvis doctor`; chat in the browser at `<URL>/`.

Email: IMAP + app password (Gmail/Outlook/iCloud). Calendar: the "secret iCal address" of Google/Outlook/Apple. No OAuth apps to register.

## Token / cost efficiency
- Claude Haiku 4.5 by default; screening a call ≈ 150 tokens, an email batch ≈ 1 call per check, **no call at all when there's no new mail**.
- Email is triaged in one batch with truncated snippets; already-seen mail is never re-sent.
- Static prompts use prompt caching; chat history capped at 6 turns; context (email/calendar) is only fetched when you ask about it.
- `llm.daily_token_budget` is a hard stop. Typical personal use: well under $1–3/month in LLM cost.

## Security
- Twilio request signatures verified whenever `TWILIO_AUTH_TOKEN` is set. WhatsApp accepts the owner's number only.
- Caller speech is treated as data; the verdict is whitelisted (`connect|message|spam`) and any LLM failure falls back to *take a message*, never to *connect*.
- The LLM can only change config through a fixed list of whitelisted actions.
- Email is opened read-only (never marked read).

## The "Jarvis voice" – licensing (not legal advice)
- The movie voice is **Paul Bettany's performance of a Marvel/Disney character**. Cloned "JARVIS" voice models (Piper/RVC/fish.audio) are fine for personal fan use but **must not be shipped or sold**: right-of-publicity + Marvel trademark/copyright. Also don't call a paid product "Jarvis" – use your own name (it is one config line: `voice.assistant_name`).
- What this repo ships instead: a **free British-butler-style voice** – Amazon Polly `Brian` via Twilio `<Say>` on calls, and the browser's built-in en-GB voice on the web page. Sell-safe alternatives: Kokoro (Apache-2.0), Piper voices with permissive licences (check each voice card), ElevenLabs/Azure voices under their commercial terms, or **a voice actor you hire and license** for a real signature voice.
- Selling: the code is MIT. Using Claude: customers should bring their own API key (Anthropic's terms don't allow reselling a Pro/Max subscription login; `claude_cli` is for the user's *own* personal machine). Recording/screening calls needs a disclosure in many regions – the greeting can say so.

## Test
`pytest` (16 tests, no network/keys needed: LLM and Twilio are faked).

## Roadmap
Realtime streaming voice (Twilio ConversationRelay) for lower latency · Google/Microsoft OAuth connectors · Telegram · outbound calls on your behalf.
