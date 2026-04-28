# test100 — Premium Quiz + Fortune Wheel + Telegram Bot

Glassmorphism dizaynli, Telegram chat ko'rinishidagi quiz, "rigged" omad g'ildiragi, lid yig'ish formasi va to'liq dinamik admin Telegram bot — bitta FastAPI ilovasida.

## Xususiyatlar

- 💎 Glassmorphism + gradient + animatsiyalar
- 📱 To'liq responsive (iPhone, Android, MacBook, Windows)
- 💬 Telegram chat-style quiz (typewriter effekt, chat bubble animatsiyasi)
- 🎡 Canvas omad g'ildiragi — backenddagi foizga "rigged" tushadi
- 📝 Lid forma → Telegram guruhga avtomatik yuboriladi
- 🤖 Admin bot orqali to'liq boshqaruv (savollar, ism, foto, foiz, guruh)

## O'rnatish

```bash
cd ~/Downloads/keraksiz/test100
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env ichidagi BOT_TOKEN, ADMIN_IDS, TARGET_GROUP_ID ni to'ldiring
python app.py
```

Saytni oching: http://localhost:8000

## Telegram bot komandalari (faqat admin)

```
/start          - panel ko'rsatish
/status         - joriy sozlamalar
/setname X      - profil ismini o'zgartirish
/setsubtitle X  - "online" matnini o'zgartirish
/setphoto       - keyin rasm yuboring
/setpercent 75  - g'ildirak qaysi foizga tushishini sozlash
/setgroup -100… - lidlar yuboriladigan guruh chat_id
/addquestion    - savol qo'shish (interaktiv)
/listq          - savollar ro'yxati
/delq 3         - savol o'chirish
/leads          - oxirgi 10 lid
/id             - chat_id ni ko'rsatish (guruhda chaqiring)
```

## Guruhga ulash

1. Botni guruhga admin qilib qo'shing.
2. Guruhda `/id` yuboring — chat_id ni oling.
3. Botga shaxsan: `/setgroup -1001234567890` yuboring.

Endi har lid avtomatik o'sha guruhga tushadi.

## Texnik stack

- Backend: FastAPI + aiogram 3 (bitta event loopda)
- DB: SQLite (`data.db` — avtomatik yaratiladi)
- Frontend: Vanilla HTML/CSS/JS (build kerakmas)

## Strukturasi

```
test100/
├── app.py              # FastAPI + bot
├── requirements.txt
├── .env.example
├── data.db             # avtomatik
└── static/
    ├── index.html
    ├── styles.css
    ├── app.js
    ├── default-avatar.svg
    └── uploads/        # bot yuborgan rasmlar
```
# TARGEBOT
