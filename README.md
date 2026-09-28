# Job Radar — Java / Java Full Stack (0–2 yrs) job alerts

Kaam kya karta hai: job boards se Java Developer / Java Full Stack ki openings uthata hai,
"0-2 years / fresher friendly" wali filter karta hai, aur naya job milte hi Telegram pe
message bhej deta hai. Ek baar notify hone ke baad wahi job dobara nahi aayega.

Sources abhi 3 tarah ke hain:
- **Arbeitnow** — free, key nahi chahiye, "java" keyword se hazaron company boards search karta hai
- **RemoteOK** — free, key nahi chahiye, remote Java jobs
- **Adzuna** — free API key chahiye (2 min ka signup), sabse bada coverage — India ke saath
  Lever/Greenhouse/company career pages sab index karta hai
- Greenhouse/Lever/Ashby/SmartRecruiters/Workable/Workday — agar koi **specific company** target
  karni ho to uska slug daal sakte ho (config.json me khaali hai abhi)

---

## STEP-BY-STEP SETUP (apne laptop pe)

### Step 1 — Python check karo
Terminal / Command Prompt khol ke:
```
python3 --version
```
(Windows pe `python --version`). Agar na ho to python.org se install kar lo (3.9+ chahiye).

### Step 2 — Files ek folder me rakho
`job_radar.py` aur `config.json` dono ek hi folder me rakho, jaise:
```
Desktop/job-radar/job_radar.py
Desktop/job-radar/config.json
```

### Step 3 — Library install karo
```
pip install requests
```
(agar error aaye to `pip3 install requests` try karo)

### Step 4 — Telegram bot banao (yahi tumhare notifications ka zariya hai)
1. Telegram app kholo → search bar me `@BotFather` dhoondho → chat kholo
2. `/newbot` type karo, enter
3. Bot ka naam poochega (kuch bhi, jaise `MyJobRadar`)
4. Username poochega — unique hona chahiye, end me `bot` hona chahiye (jaise `myjobradar114_bot`)
5. Ek **token** milega, kuch aisa dikhega: `7123456789:AAHx...........`
   → ise copy karke rakh lo, ye tumhara `bot_token` hai
6. **Zaroori:** ab apne isi naye bot ko khol ke usse ek message bhejo — "hi" likh do
   (bina isके Telegram tumhe message nahi bhej payega, sirf username kaafi nahi hai)

### Step 5 — apna chat_id nikaalo
Browser me ye URL kholo (TOKEN apna wala daalo):
```
https://api.telegram.org/bot<TOKEN>/getUpdates
```
Isme kuch aisa milega:
```json
"chat":{"id":987654321,"first_name":"..."}
```
`987654321` wala number tumhara `chat_id` hai.

> Agar khaali `{"ok":true,"result":[]}` aaye — iska matlab Step 4.6 nahi kiya, pehle bot ko
> message bhejo phir ye URL refresh karo.

### Step 6 — config.json bharo
File kholo, ye do line update karo:
```json
"telegram": {
  "bot_token": "yaha apna token paste karo",
  "chat_id": "yaha apna chat_id paste karo"
}
```

### Step 7 — Adzuna key lo (recommended, coverage best hai)
1. https://developer.adzuna.com/ pe jao → free signup
2. Dashboard me `app_id` aur `app_key` milega
3. config.json me:
```json
"adzuna": [
  {
    "app_id": "yaha app_id",
    "app_key": "yaha app_key",
    "country": "in",
    "what": "java developer",
    "pages": 3
  }
]
```
(Skip bhi kar sakte ho — Arbeitnow aur RemoteOK bina key ke chalenge)

### Step 8 — Test run karo
Terminal me us folder me jaake:
```
cd Desktop/job-radar
python3 job_radar.py --test
```
Ye sirf console pe dikhayega kya-kya match hua, Telegram pe kuch nahi bhejega.
Agar list dikhe (ya "0 nayi job" bhi dikhe to bhi theek hai, matlab code chal raha hai) —
matlab setup sahi hai.

### Step 9 — Asli run
```
python3 job_radar.py
```
Ab jo bhi match hoga uska Telegram pe message aa jayega.

### Step 10 — Baar-baar apne aap chale, iske liye 2 options:

**Option A — jab tak laptop chalu ho, terminal khula rakho:**
```
python3 job_radar.py --loop 1800
```
(1800 second = 30 min gap se check karta rahega, terminal band mat karo)

**Option B — laptop band ho tab bhi chale (GitHub Actions, free):**
1. GitHub pe ek **private repo** banao
2. `job_radar.py` aur `config.json` (bina apna real token/key daale — khaali rakho ya
   placeholder rakho) us repo me upload karo
3. `.github/workflows/job-radar.yml` naam ki file banao — mere diye `job-radar.yml` ka
   content usme daal do
4. Repo → Settings → Secrets and variables → Actions → **New repository secret**:
   - `TG_BOT_TOKEN` = apna bot token
   - `TG_CHAT_ID` = apna chat id
5. Bas — ab GitHub apne aap har 30 min script chalayega, tumhare laptop ki zaroorat nahi

---

## Filter adjust karna ho to (job_radar.py ke top pe):
- `MAX_YEARS_REQUIRED = 3` — isse zyada year maangne wali job skip hoti hai, number badal sakte ho
- `TITLE_BLOCKLIST` — "senior/lead/intern" jaise words hata/jod sakte ho
- Location filter chahiye (sirf India / Remote) — bolo to line add kar dunga

## Company add karni ho (Greenhouse/Lever specific)
Company ka career page URL bhejo, main uska slug nikaal ke config me daal dunga — ya
khud URL se pattern samajh sakte ho jo pehle wale message me table diya tha.

## Jo cheez script me nahi hai (honest)
- **LinkedIn** — koi public API nahi, scraping ToS violation + ban risk hai. Uske liye
  khud LinkedIn pe "Create job alert" on karo (Entry level + Past 24h filter).
- **Taleo, SuccessFactors, Jobvite** — har company ka instance alag, koi common free API nahi.
  Specific company chahiye to bata do, uska custom fetcher try karke dekhte hain.
