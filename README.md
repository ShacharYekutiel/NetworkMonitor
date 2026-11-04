# NetworkMonitor

## English

NetworkMonitor is a free, license-free network monitoring web application in the spirit of PRTG. It monitors
devices with ping, SNMP (system, CPU, memory, interface traffic), HTTP(S), TCP ports and DNS. It keeps
time-series history, rolls up status for groups and devices, and alerts by email or webhook. It updates live
through a WebSocket, and the UI comes in English and Hebrew (RTL).

- **Single process:** a Python FastAPI backend serves the API, the WebSocket and the React UI, and runs the
  poller and the alert engine.
- **Storage:** PostgreSQL with TimescaleDB. It falls back to plain PostgreSQL automatically when TimescaleDB is
  not available.
- **Zero configuration:** secrets are generated on first run. Open the web UI and the setup wizard creates the
  admin user.

### Quick start

**Docker (recommended)**
```bash
docker compose -f deploy/docker-compose.yml up -d
# open http://<server>:8080 and complete the setup wizard
```

**Native Linux** (systemd, run as root)
```bash
sudo ./deploy/linux/install.sh
```

**Native Windows** (elevated PowerShell)
```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1
```

Either way, the app listens on port **8080**. The health check is at `http://<server>:8080/api/health`.

The docs are in [`docs/`](docs/):
- [architecture](docs/architecture.md)
- [data model](docs/data-model.md)
- [API](docs/api.md)
- [development conventions](docs/conventions.md)

---

<div dir="rtl">

## עברית

NetworkMonitor היא מערכת ניטור רשת מבוססת דפדפן, חינמית וללא רישוי, ברוח PRTG. המערכת מנטרת התקנים באמצעות
Ping, ‏SNMP (מידע מערכת, מעבד, זיכרון, תעבורת ממשקים), ‏HTTP(S), פורטי TCP ו-DNS. היא שומרת היסטוריית מדידות,
מחשבת סטטוס מצטבר לקבוצות ולהתקנים, ושולחת התראות בדוא"ל או ב-Webhook. התצוגה מתעדכנת בזמן אמת, והממשק זמין
בעברית (מימין לשמאל) ובאנגלית.

- **תהליך יחיד:** שרת FastAPI (‏Python) מגיש את ה-API, את ה-WebSocket ואת ממשק ה-React, ומריץ גם את מנוע הדגימה
  ואת מנוע ההתראות.
- **אחסון:** ‏PostgreSQL עם TimescaleDB. כש-TimescaleDB לא זמין, המערכת עוברת אוטומטית ל-PostgreSQL רגיל.
- **ללא הגדרות:** הסודות נוצרים אוטומטית בהפעלה הראשונה. בכניסה הראשונה לממשק, אשף ההתקנה יוצר את משתמש המנהל.

### התחלה מהירה

**Docker (מומלץ)**

<div dir="ltr">

```bash
docker compose -f deploy/docker-compose.yml up -d
```

</div>

אחר כך פתחו את `http://<server>:8080` והשלימו את אשף ההתקנה.

**לינוקס (התקנה מקומית, systemd, כ-root)**

<div dir="ltr">

```bash
sudo ./deploy/linux/install.sh
```

</div>

**Windows (התקנה מקומית, PowerShell כמנהל)**

<div dir="ltr">

```powershell
powershell -ExecutionPolicy Bypass -File .\deploy\windows\install.ps1
```

</div>

בכל דרכי ההתקנה המערכת מאזינה בפורט **8080**. בדיקת התקינות זמינה בכתובת `/api/health`. התיעוד המלא נמצא
בתיקייה [`docs/`](docs/).

</div>
