# NudgeBot Privacy Notice

**Last updated:** November 2026

NudgeBot is a private, personal utility designed to help you and your classmates stay on top of course deadlines. We respect your privacy and handle your data with care.

---

## 1. What We Store
When you interact with NudgeBot, we store only the minimal data required to monitor your portal:
- **Telegram Identifiers:** Your Telegram Chat ID and username (to send you notifications).
- **Portal Credentials:** Your portal URL, username, and password or access token. These are **symmetrically encrypted at rest** using Fernet (AES-128 in CBC mode with HMAC authentication) using a server-side encryption key. They are decrypted in memory only during the active seconds of your poll cycle.
- **Academic Item Metadata:** Course names, assignment titles, points, and due dates discovered on your portal. We cache these to compute changes (diffs) and send you upcoming reminders.
- **Operational Logs & Events:** Timestamps of poll cycles, status codes (ok/error), and anonymous counts of notifications sent to measure system reliability.

---

## 2. What We NEVER Do
- **No Credential Sharing:** We never share, sell, or expose your credentials to any third party.
- **No LLM Credential Leakage:** When AI summarization is enabled (Gemini or OpenAI-compatible), we **never** pass your passwords, access tokens, cookies, or personal identifiers. We only pass course codes, assignment titles, points, scores, and due dates (e.g. `CS 101: Problem Set 4`).
- **No Unauthorized Scraping:** NudgeBot only ever checks portals using credentials you explicitly provide, after you explicitly opt-in. We do not attempt to bypass access controls, captcha gates, or scrape unauthorized institutional data.

---

## 3. Pluggable AI Summarization & Third-Party Disclosure
NudgeBot supports pluggable LLM summarization (`LLM_PROVIDER=none|gemini|openai_compat`).
- **Default (Zero External Data):** `LLM_PROVIDER=none` uses 100% deterministic local templates. No external LLM calls are made and no academic data leaves your host.
- **Local LLM Option:** Operators can point `LLM_BASE_URL` to a self-hosted Ollama server on the local network (`http://localhost:11434/v1`) for local AI summaries with zero external transmission.
- **Hosted Free Tier Notice:** If the administrator configures a hosted cloud provider (such as the Google Gemini API or hosted OpenAI-compatible providers), assignment titles, course codes, and scores are transmitted over TLS to that provider to generate short notification messages. Note that **free tiers** of hosted AI providers may retain and use prompt data to train or improve their models according to their respective developer terms. If you do not want course titles processed by third-party AI services, ask your administrator to keep `LLM_PROVIDER=none`.

---

## 4. Your Rights & Data Control
- **Mute notifications:** Run `/pause` anytime to stop polls without losing your setup.
- **Delete credentials:** Run `/unlink confirm` to permanently delete your stored portal credentials and cached item history.
- **Full GDPR Erasure:** Run `/deletemydata confirm` to permanently erase all records associated with your Telegram account from the database.

---

## 5. Institutional Compliance
You must ensure that automating access to your student portal complies with your school's policies and acceptable use guidelines. NudgeBot operates with polite, rate-limited polling intervals (minimum 5 minutes) and standard user-agent strings.

