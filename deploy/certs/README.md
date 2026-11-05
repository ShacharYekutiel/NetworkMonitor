# Corporate CA certificates / תעודות CA ארגוניות

[English](#english) | [עברית](#עברית)

## English

Put your organisation's root CA certificate(s) in this folder if your network uses a
firewall or proxy that inspects HTTPS (it re-signs every HTTPS connection with a private
corporate CA). Typical symptoms without it:

- `docker compose ... up -d --build` stops at `npm ci` with
  `UNABLE_TO_GET_ISSUER_CERT_LOCALLY` or `SELF_SIGNED_CERT_IN_CHAIN`, or `pip install`
  fails with `CERTIFICATE_VERIFY_FAILED`;
- Telegram / webhook / email notifications or HTTPS sensors fail with
  `certificate verify failed: unable to get local issuer certificate`.

Nothing else needs to be configured: every file named `*.crt`, `*.pem` or `*.cer` in
this folder is trusted by the image build (npm, pip, apt) and by the running app.
Several files (or one file holding a whole chain) are fine. Other files are ignored,
and certificates in this folder are never committed to git (see `.gitignore`).

### Exporting the CA on Windows

**Option A, from the browser** (Edge or Chrome, any HTTPS site such as
`https://registry.npmjs.org`):

1. Click the padlock (or "site information") icon in the address bar, then
   *Connection is secure* → *Certificate is valid*.
2. Open the **Details** tab (Certification Path / Certificate Hierarchy) and select the
   **top** certificate of the chain, the root. On an inspected network its name is your
   organisation's or the firewall vendor's (for example "Contoso Root CA", "Fortinet",
   "Zscaler", "Palo Alto"), not a public CA.
3. **Export** → *Base-64 encoded ASCII, single certificate* (in the Windows certificate
   dialog: *Copy to File...* → **Base-64 encoded X.509 (.CER)**).
4. Save it into this folder, for example `deploy\certs\corporate-root.cer`.

**Option B, from the certificate store** (`Win + R` → `certlm.msc`, or `certmgr.msc`
for the current user):

1. *Trusted Root Certification Authorities* → *Certificates*.
2. Find the corporate root CA, right-click → *All Tasks* → *Export...*.
3. Choose **Base-64 encoded X.509 (.CER)** and save it into this folder.

A Base-64 `.cer` file *is* a PEM file; no conversion is needed. A DER (binary) file is
converted automatically during the build. If your IT department gives you the CA as a
file, just drop that file here.

### Then rebuild

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

Native installs: on **Windows** the app uses the Windows certificate store directly, so a
CA your IT department deployed is trusted with no action; if you copied a file here
before running `deploy\windows\install.ps1`, it is used for the web UI build and by the
service as well. On **Linux**, `deploy/linux/install.sh` adds the files in this folder
to the system trust store.

## עברית

<div dir="rtl">

אם הרשת שלכם עוברת דרך חומת אש או פרוקסי שמבצעים בדיקת HTTPS (הם חותמים מחדש כל חיבור
HTTPS בתעודת CA ארגונית פרטית), שימו בתיקייה הזו את תעודת ה-CA הראשית של הארגון.
תסמינים אופייניים כשהיא חסרה:

- `docker compose ... up -d --build` נעצר בשלב `npm ci` עם
  `UNABLE_TO_GET_ISSUER_CERT_LOCALLY` או `SELF_SIGNED_CERT_IN_CHAIN`, או ש-`pip install`
  נכשל עם `CERTIFICATE_VERIFY_FAILED`;
- התראות Telegram, Webhook או דוא"ל, או חיישני HTTPS, נכשלים עם
  `certificate verify failed: unable to get local issuer certificate`.

אין צורך בשום הגדרה נוספת: כל קובץ בשם `*.crt`, `*.pem` או `*.cer` בתיקייה הזו נחשב
מהימן בבניית האימג' (npm, pip, apt) ובאפליקציה עצמה בזמן ריצה. אפשר כמה קבצים, או קובץ
אחד שמכיל שרשרת שלמה. קבצים אחרים מתעלמים מהם, ותעודות בתיקייה הזו לעולם אינן נשמרות
ב-git (ראו `.gitignore`).

### ייצוא ה-CA ב-Windows

**אפשרות א', מהדפדפן** (Edge או Chrome, בכל אתר HTTPS, למשל `https://registry.npmjs.org`):

1. לחצו על סמל המנעול (או "פרטי האתר") בשורת הכתובת, ואז *החיבור מאובטח* ← *האישור חוקי*.
2. עברו ללשונית **פרטים** (נתיב אישורים / היררכיית אישורים) ובחרו את התעודה **העליונה**
   בשרשרת, תעודת השורש. ברשת עם בדיקת HTTPS השם שלה יהיה של הארגון או של יצרן חומת האש
   (למשל "Contoso Root CA",‏ "Fortinet",‏ "Zscaler",‏ "Palo Alto"), ולא של CA ציבורי.
3. **ייצוא** ← *Base-64 encoded ASCII, single certificate* (בחלון התעודה של Windows:
   *העתק לקובץ...* ← **Base-64 encoded X.509 (.CER)**).
4. שמרו את הקובץ בתיקייה הזו, למשל `deploy\certs\corporate-root.cer`.

**אפשרות ב', ממאגר התעודות** (`Win + R` ← `certlm.msc`, או `certmgr.msc` למשתמש הנוכחי):

1. *Trusted Root Certification Authorities* (רשויות אישורים בסיסיות מהימנות) ← *Certificates*.
2. מצאו את תעודת ה-CA הארגונית, לחצן ימני ← *All Tasks* ← *Export...*.
3. בחרו **Base-64 encoded X.509 (.CER)** ושמרו בתיקייה הזו.

קובץ `.cer` בקידוד Base-64 *הוא* קובץ PEM, ואין צורך בהמרה. קובץ DER (בינארי) מומר
אוטומטית בזמן הבנייה. אם מחלקת ה-IT נתנה לכם את ה-CA כקובץ, פשוט העתיקו אותו לכאן.

### ואז בנו מחדש

</div>

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

<div dir="rtl">

התקנה ללא Docker: ב-**Windows** האפליקציה משתמשת ישירות במאגר התעודות של Windows, ולכן CA
שמחלקת ה-IT הפיצה מהימן בלי לעשות דבר; אם העתקתם קובץ לכאן לפני הרצת
`deploy\windows\install.ps1`, הוא ישמש גם לבניית ממשק הווב וגם את השירות. ב-**Linux**,
הסקריפט `deploy/linux/install.sh` מוסיף את הקבצים מהתיקייה הזו למאגר התעודות של המערכת.

</div>

### Root, not intermediate / שורש, לא ביניים

The build checks that every certificate here chains to a **root** (a self-signed CA).
npm accepts an intermediate on its own, but Python/OpenSSL (pip and the app) do not, so a
build with only an intermediate stops with `corporate CA ... is an intermediate certificate
whose root is missing` and names the missing issuer. Export that issuer (the top of the
chain) into this folder too. Check a file on Windows with:

```powershell
$c = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new("deploy\certs\corporate-ca.crt")
"Subject: $($c.Subject)"; "Issuer:  $($c.Issuer)"
```

If Subject and Issuer are the same, it is a root.

הבנייה בודקת שכל תעודה כאן מובילה לתעודת **שורש** (CA שחתום על עצמו). npm מקבל גם תעודת ביניים
לבדה, אבל Python/OpenSSL (pip והאפליקציה) לא, ולכן בנייה עם תעודת ביניים בלבד נעצרת עם הודעה
שמציינת את התעודה החסרה. מייצאים גם אותה (הראשונה בשרשרת) לתיקייה הזו. אם ב-PowerShell שלמעלה
Subject ו-Issuer זהים, זו תעודת שורש.
