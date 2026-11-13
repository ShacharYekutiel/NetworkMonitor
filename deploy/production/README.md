# NetworkMonitor — סביבת Production ‏(GitOps)

התיקייה `deploy/production/` היא **ההגדרה המלאה של שרת ה-production**. השרת מושך את ה-repo
כל 2 דקות ומיישם בעצמו כל שינוי שנכנס לתיקייה הזו ב-`main`. אף אחד לא מתחבר לשרת כדי "להעלות גרסה".

- `release.env` — **הקובץ שמשנים כדי לשדרג**: הגרסה המדויקת ותמונת ה-Docker ‏(אפשר עם digest).
  לעולם לא `latest`.
- `compose.yaml` — ה-stack: ‏`init` ‏(יוצר סיסמת DB אקראית), ‏`db` ‏(TimescaleDB), ‏`netmon` ‏(האפליקציה;
  ממשק הווב לא חשוף ישירות, ‏traps ב-UDP 162 ו-syslog ב-UDP/TCP 514 כן — ראו "SNMP traps ו-syslog"; ‏flows
  ב-UDP 2055/4739/6343 — ראו "שלב 3"),
  ‏`proxy` ‏(Caddy: ‏HTTPS על 443, הפניה מ-80 ל-443, WebSocket).
- `config/settings.env` — הגדרות אפליקציה שאינן סודיות (אזור זמן `Asia/Jerusalem` וכו').
- `caddy/Caddyfile` — הגדרות ה-reverse proxy.
- `ops/` — סקריפטים לשרת: ‏`bootstrap.sh`, ‏`sync.sh`, ‏`backup.sh`, ‏`restore.sh`, ‏`make-csr.sh`,
  ‏`install-cert.sh`, ‏`status.sh`, ‏`ingest-firewall.sh` ‏(אופציונלי: מי רשאי לשלוח traps/syslog/flows),
  ‏`assistant-net.sh` ‏(עוזר AI: ‏NO_PROXY ובדיקת הנתיב ל-Bedrock).
- `aws/` — תבניות IAM לעוזר AI ‏(Amazon Bedrock) ו-`render-policies.py` שממלא אותן — ראו "שלב 3b".
- `systemd/` — ה-timers: סנכרון כל 2 דקות, גיבוי יומי ב-02:17; ‏`netmon-ingest-firewall.service` ‏(מותקן,
  **לא** מופעל — רק אם בוחרים בו).
- `tests/` — בדיקות של `sync.sh`, ‏`install-cert.sh` ו-`assistant-net.sh` בלי Docker ‏(רצות ב-CI).

סודות **אינם** ב-git. הם נמצאים רק בשרת, בקבצים של root תחת `/etc/netmon-gitops/`.

---

## איך שינוי מגיע ל-production

```
PR שמשנה את deploy/production/  ->  merge ל-main  ->  השרת מושך תוך ~2 דקות  ->  deploy
```

מה `sync.sh` עושה בכל ריצה (`netmon-gitops.timer`):

1. `git fetch` של `origin/main` ל-`/opt/netmon`.
2. **deploy רק אם התוכן של `deploy/production/` השתנה** מאז הגרסה שרצה (משווים את ה-git tree של
   התיקייה, כמו `git diff --quiet <deployed>..origin/main -- deploy/production`).
   merge של קוד בלבד **אף פעם** לא גורם ל-deploy.
3. לפני החלפת תמונת האפליקציה: גיבוי DB ‏(`pg_dump`) אל
   `/var/backups/netmon/pre-deploy-<rev>-<time>.dump` ‏(ה-migrations הם חד-כיווניים).
4. `docker compose pull`, ‏`up -d --remove-orphans`, ואז המתנה עד שכל הקונטיינרים healthy
   ו-`https://<fqdn>/api/health` מחזיר `ok` ‏(עד 5 דקות).
5. **אם נכשל:** חוזרים אוטומטית לגרסה הקודמת שעבדה, והשינוי מסומן "רע" — לא ינסו אותו שוב עד שיגיע
   שינוי **חדש** ל-`deploy/production/`. **מסד הנתונים לעולם לא משוחזר אוטומטית** (ראו "שחזור").
6. אם אין שינוי: בדיקת drift — קונטיינר שלא רץ או תמונה לא נכונה מתוקנים אוטומטית.

ריצות לא חופפות (`flock`). הכול נרשם ל-journald:

```bash
journalctl -u netmon-gitops -n 50
```

## שדרוג גרסה

1. מוציאים גרסה בקוד: merge ל-`main`, ואז tag:
   ```bash
   git tag v1.1.0 && git push origin v1.1.0
   ```
2. ה-workflow ‏`release.yml` בונה את התמונה `ghcr.io/shacharyekutiel/networkmonitor:1.1.0` ופותח PR
   בשם **"Deploy v1.1.0 to production"** שמשנה רק את `deploy/production/release.env`
   (גרסה + digest).
3. בודקים ועושים merge ל-PR. תוך ~2 דקות השרת מתעדכן.

אפשר גם לשנות את `release.env` ידנית ב-PR רגיל:

```
NETMON_VERSION=1.1.0
NETMON_IMAGE=ghcr.io/shacharyekutiel/networkmonitor:1.1.0
# או עם digest:
# NETMON_IMAGE=ghcr.io/shacharyekutiel/networkmonitor:1.1.0@sha256:<64 hex>
```

`ops/check-release.sh` ‏(ב-CI ובשרת) דוחה כל פורמט אחר, ובפרט `latest`. ‏`:latest` ב-ghcr מתעדכן גם מכל
push ל-`main` ‏(image.yml) וגם מכל tag ‏(release.yml), ולכן אסור להשתמש בו ב-production.

> PR שנפתח אוטומטית על ידי `GITHUB_TOKEN` לא מפעיל workflows אחרים. הבדיקות של `release.env`
> רצות כבר בתוך `release.yml` לפני פתיחת ה-PR, ושוב (`production.yml`) אחרי ה-merge ל-`main`.
> כדי להריץ אותן גם על ה-PR: Close ואז Reopen.

## חזרה לגרסה קודמת (Rollback)

```bash
git revert <commit של ה-bump>      # ב-PR, ואז merge
```

השרת יחזיר את התמונה הקודמת תוך ~2 דקות. **שימו לב:** אם הגרסה החדשה כבר הריצה migration על ה-DB,
ייתכן שהגרסה הישנה לא תעלה על ה-DB החדש. במקרה כזה משחזרים את ה-dump שנלקח לפני ה-deploy
(ראו "שחזור"). השרת לא עושה זאת לבד.


### ‏[1.4.2] סודות מוצפנים, חזרה ל-1.4.1 ו-`secret.key`

- ‏1.4.2 מוסיפה עותק מוצפן (AES-GCM) לכל community/מפתח SNMP, סיסמת SMTP, טוקן טלגרם וכתובות Teams/webhook.
  **הטקסט הגלוי נשאר במקומו ומסונכרן בגרסה הזאת**, כדי ש-rollback ל-1.4.1 (אוטומטי או `git revert`) ימשיך לעבוד.
  הוא יוסר בגרסה הבאה, או עכשיו: `docker compose --env-file release.env exec netmon netmon secrets remove-plaintext`
  ואז restart ל-netmon. לפני חזרה ל-1.4.1 אחרי הסרה: `netmon secrets restore-plaintext`.
- חזרה ל-1.4.0 או ישנה יותר: רק עם שחזור ה-dump שנלקח לפני ה-deploy (`ops/restore.sh`).
- ‏`secret.key` נמצא בגיבוי של volume הנתונים (`netmon-data-*.tar.gz`), לא ב-dump של ה-DB. החלפת מפתח:
  `netmon secrets rekey` כשהשרת עצור. פרטים: [docs/production.md](../../docs/production.md#142-secretkey).
- הקונטיינרים רצים בלי capabilities (Caddy: רק `NET_BIND_SERVICE`), עם מערכת קבצים לקריאה בלבד, ה-DB כמשתמש
  postgres, ו-API הניהול של Caddy כבוי (החלפת תעודה עושה restart ל-proxy).

---

## התקנה ראשונה של השרת (Ubuntu 22.04 / 24.04)

צריך מראש:

- שרת Ubuntu עם גישה לאינטרנט (ישירה או דרך proxy), פורטים 80 ו-443 פתוחים מהרשת המשרדית.
- רשומת DNS: השם הקצר `NetMonitor` והשם המלא (FQDN, למשל `netmonitor.corp.local`) מצביעים לשרת.
- **שני tokens של GitHub** (נשמרים רק בשרת):
  - **fine-grained token** עם `Contents: Read-only` על ה-repo ‏`shacharyekutiel/networkmonitor` בלבד
    (ל-`git clone` / `git fetch`).
  - **classic token** עם `read:packages` (ל-`docker login ghcr.io`, התמונה פרטית).

### שלב 1 — CA ו-proxy ארגוניים (רק אם צריך)

אם הרשת עושה TLS inspection, מתקינים את ה-CA הארגוני:

```bash
sudo cp corp-root-ca.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates
```

(הקובץ חייב להיות PEM עם סיומת `.crt`. קובץ DER ממירים קודם:
`openssl x509 -inform DER -in ca.cer -out corp-root-ca.crt`.)

אם היציאה לאינטרנט היא דרך proxy, מגדירים אותו ב-shell של root:

```bash
sudo -i
export HTTPS_PROXY=http://proxy.corp.local:8080
export HTTP_PROXY=http://proxy.corp.local:8080
export NO_PROXY=localhost,127.0.0.1,.corp.local
```

### שלב 2 — git ו-clone

```bash
apt-get update && apt-get install -y git
git clone https://github.com/shacharyekutiel/networkmonitor.git /opt/netmon
```

git ישאל: **Username** = שם המשתמש ב-GitHub, **Password** = ה-fine-grained token.

### שלב 3 — bootstrap

```bash
cd /opt/netmon/deploy/production && sudo -E NETMON_FQDN=netmonitor.corp.local ./ops/bootstrap.sh
```

(`-E` מעביר את משתני ה-proxy.) הסקריפט:

- שומר את ה-proxy ‏(אם הוגדר) עבור apt, ה-Docker daemon ‏(systemd drop-in) ו-git, ול-`app.env`
  של האפליקציה (התראות Telegram/מייל);
- מתקין `docker.io` ו-`docker-compose-v2` מהמאגר של Ubuntu;
- שואל (קלט מוסתר) את שני ה-tokens, ושומר אותם ב-`/etc/netmon-gitops/` ‏(root בלבד). ה-clone משתמש
  ב-token דרך credential helper (ה-token לא נכתב ל-`.git/config`). שם המשתמש ל-ghcr נלקח
  מ-`GITHUB_USER` או נשאל;
- שומר את ה-FQDN ב-`/etc/netmon-gitops/site.env`;
- אם אין עדיין תעודה: יוצר **תעודה זמנית self-signed ל-30 יום**, כדי ש-HTTPS יעבוד מיד;
- מתקין ומפעיל את ה-timers ומריץ את ה-deploy הראשון;
- מדפיס את הכתובת.

אפשר להריץ שוב בכל עת (idempotent): tokens, תעודות ונתונים נשמרים.

### שלב 4 — אשף ההגדרה (מיד!)

פותחים את `https://netmonitor.corp.local/` ומשלימים את **אשף ההגדרה**.
**המשתמש הראשון שנוצר הוא ה-admin.** עד שהאשף הושלם, כל מי שמגיע לדף יכול ליצור את חשבון ה-admin —
לכן עושים זאת **מיד** אחרי ההפעלה הראשונה. (הדפדפן יציג אזהרת תעודה עד שלב 5.)

### שלב 5 — תעודה מה-CA הארגוני (CSR)

```bash
cd /opt/netmon/deploy/production
sudo ./ops/make-csr.sh netmonitor.corp.local 10.0.0.15     # ה-IP אופציונלי
```

הסקריפט יוצר `/etc/netmon-gitops/tls/netmon.key` ‏(רק אם אינו קיים; מפתח חדש רק עם `--force`)
ו-`/etc/netmon-gitops/tls/netmon.csr`, ומדפיס את ה-CSR. ה-SANs: ‏`DNS:<fqdn>`, ‏`DNS:NetMonitor`
ו-`IP:<ip>`.
שולחים את ה-CSR לצוות ה-CA ומבקשים תעודת Web Server בפורמט PEM (Base-64) **כולל ה-intermediate**
(full chain).

כשהתעודה חוזרת, מעתיקים אותה לשרת ומריצים:

```bash
sudo ./ops/install-cert.sh /path/to/signed.pem
# אם ה-chain הגיע בקובץ נפרד:
sudo ./ops/install-cert.sh /path/to/signed.pem /path/to/chain.pem
```

`install-cert.sh` בודק שהתעודה מתאימה למפתח (השוואת public key), שה-SAN כולל את ה-FQDN ושהיא בתוקף,
מתקין אותה כ-`cert.pem` ‏(full chain) + `key.pem`, שומר את הזוג הקודם ב-`tls/previous-<time>/`
וטוען מחדש את Caddy. חידוש תעודה: אותו תהליך (אפשר עם אותו מפתח, בלי `--force`).

### שלב 6 — בדיקה

```bash
systemctl list-timers 'netmon*'
journalctl -u netmon-gitops -n 50
sudo /opt/netmon/deploy/production/ops/status.sh
```

`status.sh` מציג: הגרסה שרצה מול `origin/main` (ואם יש שינוי ממתין או שינוי שסומן "רע"),
גרסת התמונה, מצב הקונטיינרים, `/api/health`, הגיבוי האחרון ותוקף התעודה.

---

## גיבויים

| מה | איפה | מתי | שמירה |
|---|---|---|---|
| `pg_dump` ‏(custom format) | `/var/backups/netmon/daily/netmon-db-<time>.dump` | כל יום 02:17 | 14 יום |
| volume הנתונים של האפליקציה (`db_password`, ‏`secret.key`, תבניות) | `/var/backups/netmon/daily/netmon-data-<time>.tar.gz` | כל יום 02:17 | 14 יום |
| גרסה שרצה בזמן הגיבוי | `/var/backups/netmon/daily/netmon-meta-<time>.txt` | כל יום | 14 יום |
| `pg_dump` לפני כל החלפת גרסה | `/var/backups/netmon/pre-deploy-<rev>-<time>.dump` | לפני deploy | 10 אחרונים |

הגיבויים מכילים סודות (root בלבד). **מומלץ להעתיק את `/var/backups/netmon` גם לשרת אחר** (גיבוי
ארגוני / rsync) — גיבוי על אותו דיסק לא מגן מתקלת דיסק.

גיבוי ידני: `sudo systemctl start netmon-backup.service`

## שחזור (ידני בלבד)

```bash
cd /opt/netmon/deploy/production
# DB בלבד (למשל ה-dump שנלקח לפני deploy שנכשל):
sudo ./ops/restore.sh --dump /var/backups/netmon/pre-deploy-<rev>-<time>.dump
# DB + volume הנתונים:
sudo ./ops/restore.sh --dump /var/backups/netmon/daily/netmon-db-<time>.dump \
                      --data /var/backups/netmon/daily/netmon-data-<time>.tar.gz
```

הסקריפט עוצר את ה-timer, עוצר את `netmon` ו-`proxy`, לוקח dump בטיחות של המצב הנוכחי
(`/var/backups/netmon/pre-restore-<time>.dump`), יוצר מחדש את ה-DB, משחזר במצב TimescaleDB
(`timescaledb_pre_restore` / `post_restore`), מעלה את ה-stack, מחכה ל-health ומפעיל שוב את ה-timer.

**חשוב:** הגרסה שרצה אחרי השחזור חייבת להיות **שווה או חדשה** מהגרסה שכתבה את ה-dump (גרסה חדשה
מריצה migrations קדימה; גרסה ישנה לא יודעת לקרוא schema חדש). לכן ב-rollback אחרי migration:
קודם merge של ה-revert של `release.env`, ואז שחזור ה-`pre-deploy` dump שנלקח לפני השדרוג.

**שרת חדש (Disaster Recovery):** מריצים את שלבים 1-3 עם `NETMON_NO_DEPLOY=1` (בלי deploy ראשון),
ואז `restore.sh` עם `--dump` ו-`--data`, ואז:

```bash
sudo systemctl start netmon-gitops.service
sudo systemctl start netmon-gitops.timer netmon-backup.timer
```

## התראות על כשל (אופציונלי)

`/etc/netmon-gitops/notify.env` ‏(root, ‏0600):

```bash
NOTIFY_WEBHOOK_URL=https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>
NOTIFY_FORMAT=form        # form = Telegram; json = {"text": "..."} (Slack/Teams/webhook)
NOTIFY_ON_SUCCESS=0       # 1 = גם הודעה על deploy מוצלח
```

נשלחת הודעה על: deploy שנכשל (ותוצאת ה-rollback), deploy שנחסם (pull / גיבוי), drift שלא תוקן,
`git fetch` שנכשל 30 דקות, גיבוי יומי שנכשל. כל בעיה מדווחת פעם אחת, לא כל 2 דקות.

## קבצים בשרת

| נתיב | תוכן |
|---|---|
| `/opt/netmon` | ה-clone של ה-repo (מנוהל על ידי `sync.sh`, **לא עורכים בו קבצים**) |
| `/etc/netmon-gitops/repo-token`, `ghcr-token`, `ghcr-user` | tokens ‏(root, 0600) |
| `/etc/netmon-gitops/site.env` | `NETMON_FQDN` |
| `/etc/netmon-gitops/tls/` | `cert.pem`, ‏`key.pem` (בשימוש), ‏`netmon.key`, ‏`netmon.csr` |
| `/etc/netmon-gitops/proxy.env`, `app.env` | proxy (אם הוגדר); הגדרות מקומיות לאפליקציה (`HTTPS_PROXY` / `NO_PROXY` ל-Telegram, ‏Teams, ‏Entra ID) |
| `/etc/netmon-gitops/ca/` | תעודות CA שהאפליקציה סומכת עליהן (מועתקות מ-`/usr/local/share/ca-certificates`) |
| `/etc/netmon-gitops/aws/` | ‏(3b, אופציונלי) תעודה ומפתח ל-IAM Roles Anywhere אם בוחרים בקבצים בשרת במקום העלאה בממשק; מופיע בקונטיינר כ-`/etc/netmon/aws` |
| `/etc/netmon-gitops/notify.env` | webhook להתראות (אופציונלי) |
| `/etc/netmon-gitops/ingest-allow.conf` | הרשתות שרשאיות לשלוח traps/syslog/flows (אופציונלי, `ingest-firewall.sh`) |
| `/var/lib/netmon-gitops/` | `deployed_rev`, ‏`bad_trees`, ‏`history.log` |
| `/usr/local/lib/netmon-gitops/` | העותק המותקן של `ops/` (ה-timer מריץ אותו; מתעדכן אחרי deploy תקין) |
| `/var/backups/netmon/` | גיבויים |

## פתרון תקלות

```bash
journalctl -u netmon-gitops -n 100          # מה הסנכרון עשה ולמה
journalctl -u netmon-backup -n 50           # גיבוי
sudo /opt/netmon/deploy/production/ops/status.sh
cd /opt/netmon/deploy/production && sudo docker compose --env-file release.env ps
cd /opt/netmon/deploy/production && sudo docker compose --env-file release.env logs --tail 100 netmon
sudo /usr/local/lib/netmon-gitops/sync.sh --dry-run   # מה הסנכרון היה עושה עכשיו
```

| תסמין | סיבה / פתרון |
|---|---|
| `git fetch failed` | token פג תוקף או proxy. ‏`sudo rm /etc/netmon-gitops/repo-token` ושוב `bootstrap.sh`. |
| `cannot pull ... (ghcr token? proxy? tag missing?)` | token של ghcr פג, proxy, או שה-tag עוד לא נבנה. ‏`sudo rm /etc/netmon-gitops/ghcr-token` ושוב `bootstrap.sh`. נבדק שוב בכל ריצה. |
| `x509: certificate signed by unknown authority` ב-pull | ה-CA הארגוני לא מותקן: שלב 1, ואז `sudo systemctl restart docker`. |
| `marked bad; waiting for a new change` | ה-deploy נכשל וחזר לגרסה הקודמת. מתקנים ב-PR חדש, או אחרי תיקון בשרת: `sudo /usr/local/lib/netmon-gitops/sync.sh --force` |
| הדפדפן מזהיר על התעודה | עדיין התעודה הזמנית (`status.sh` מציין זאת) או שה-CA הארגוני לא מותקן במחשב. |
| עבודת תחזוקה ידנית | `sudo systemctl stop netmon-gitops.timer` (אחרת drift יתוקן אוטומטית), בסוף `start`. |
| כל ה-syslog/traps "מגיעים" מ-`172.x.0.1` (אזהרת NAT בדף הלוגים) | שלחתם מהשרת עצמו (עובר דרך `docker-proxy`) — בודקים ממחשב אחר. |
| traps/syslog ממכשיר מסוים הפסיקו להגיע אחרי deploy | רשומת conntrack ישנה. ‏`sync.sh` מנקה אותה אוטומטית (כשהחבילה `conntrack` מותקנת); ידנית: `sudo conntrack -D -p udp --dport 162` ‏(ו-514). |
| בהגדרות ← תכונות: `Address already in use` | תוכנה אחרת בשרת תופסת את 162/514: ‏`sudo ss -lunpt '( sport = :162 or sport = :514 )'`. |
| flows ממכשיר הפסיקו להגיע אחרי deploy | רשומת conntrack ישנה; ‏`sync.sh` מנקה גם את 2055/4739/6343. ידנית: `sudo conntrack -D -p udp --dport 6343` ‏(ו-2055, ‏4739). |
| Kerberos SSO נכשל / הדפדפן מציג טופס | `status.sh` ← "Time" ‏(הפרש שעון מעל 5 דקות); הכתובת בדפדפן חייבת להיות ה-FQDN ‏(`https://netmonitor` מפנה אליו); האתר ב-Local intranet; ‏`setspn -L` מציג SPN אחד. |
| עוזר AI: ‏Test נכשל | ‏`sudo /opt/netmon/deploy/production/ops/assistant-net.sh check --host <vpce name>`; הטבלה ב"תקלות — עוזר AI". |
| אישור מ-Telegram לא עובד / Telegram "נתקע" | ה-proxy מנתק בקשות ארוכות: מורידים את *Telegram poll timeout* בהגדרות (10–50 שניות). ‏`HTTPS_PROXY` ב-`app.env`? |

## SNMP traps ו-syslog ‏(שלב 2b-3)

`compose.yaml` מפרסם את הפורטים הסטנדרטיים: ‏**UDP 162** ‏(traps) ו-**UDP + TCP 514** ‏(syslog), שמועברים לפורטים
1162 / 1514 בתוך הקונטיינר (האפליקציה רצה בלי root). **אחרי השדרוג הכול כבוי ושום דבר לא מאזין** עד שמנהל מפעיל
*Traps* / *Syslog* ב-*הגדרות ← תכונות*. ההפעלה מיידית — **אין צורך בשינוי בשרת או ב-deploy**. בשורת הסטטוס שם
רואים "Listening on UDP 162" או את השגיאה.

### כתובת השולח נשמרת

Docker מממש פורט מפורסם עם iptables DNAT, שמשאיר את כתובת ה-IP המקורית של השולח, וכך כל הודעה משויכת למכשיר
הנכון. שלוש הסתייגויות:

- **בדיקה מהשרת עצמו** (או hairpin) עוברת דרך `docker-proxy` ומגיעה מכתובת ה-gateway של ה-bridge ‏(`172.x.0.1`).
  בודקים ממחשב אחר.
- **Docker Desktop ‏(Windows/macOS) לא שומר את כתובת השולח** (רלוונטי רק לבדיקות במחשב אישי, לא לשרת הזה).
- **conntrack של UDP:** מכשיר שהמשיך לשלוח בזמן שהקונטיינר הוחלף עלול להישאר עם רשומת conntrack ישנה (אותו
  5-tuple; syslog נשלח מפורט מקור 514) ו"להיעלם". ‏`sync.sh` מוחק את רשומות ה-UDP של 162/514 אחרי deploy שיצר
  מחדש את הקונטיינר `netmon` — אם החבילה `conntrack` מותקנת (`bootstrap.sh` מתקין אותה; בשרת קיים:
  `sudo apt-get install -y conntrack`). ידנית:

```bash
sudo conntrack -D -p udp --dport 162
sudo conntrack -D -p udp --dport 514
```

### חומת אש: ‏ufw **לא** מסנן פורטים של Docker

החוקים של Docker ‏(שרשראות `DOCKER` / `FORWARD`) נבדקים **לפני** ufw, ולכן `ufw deny 514` לא חוסם אף אחד.
אין צורך ב-`ufw allow` כדי לקבל traps/syslog, וגם אי אפשר לחסום איתו. המקום הנכון לחוקים משלכם הוא השרשרת
`DOCKER-USER`, עם הפורט **המקורי** (לפני ה-DNAT ל-1514):

```bash
# דוגמה: לקבל syslog ב-UDP רק מ-10.0.0.0/8
sudo iptables -I DOCKER-USER -p udp -m conntrack --ctstate DNAT --ctdir ORIGINAL --ctorigdstport 514 ! -s 10.0.0.0/8 -j DROP
```

הסקריפט האופציונלי `ops/ingest-firewall.sh` עושה את זה לשלושת הפורטים (162/udp, ‏514/udp, ‏514/tcp) ומשאיר את
ממשק הווב (443) בלי שינוי. **שום דבר לא מופעל אוטומטית**:

```bash
# 1. רשת אחת בכל שורה (# להערות)
printf '10.0.0.0/8\n192.168.10.0/24\n' | sudo tee /etc/netmon-gitops/ingest-allow.conf
# 2. הפעלה עכשיו ובכל אתחול (וגם אחרי restart של Docker)
sudo systemctl enable --now netmon-ingest-firewall.service
# מצב / ביטול
sudo /usr/local/lib/netmon-gitops/ingest-firewall.sh status
sudo systemctl disable --now netmon-ingest-firewall.service     # מסיר את החוקים
```

`--ctstate DNAT` מגביל את החוק לתעבורה **שנכנסת** לפורטים המפורסמים, כך ששליחה **יוצאת** של האפליקציה ל-SIEM
בפורט 514 לא נחסמת. שכבה שנייה, רכה יותר: רשימת ההתעלמות באפליקציה (*Logs ← Unknown sources ← Ignore*).

### התנגשויות פורטים

```bash
sudo ss -lunpt '( sport = :162 or sport = :514 )'    # צריך להופיע רק docker-proxy
sudo ss -ltnp  '( sport = :514 )'
```

`snmptrapd` או `rsyslog` עם `imudp`/`imtcp` על 514 יכשילו את `docker compose up` ‏(`address already in use`).
`ops/status.sh` מציג את המאזינים ואת מצב מסנן השולחים.

### הגדרת המכשירים (דוגמאות — **יש לבדוק על המכשיר**)

התחביר משתנה לפי דגם וגרסת firmware. מחליפים את `10.0.0.50` בכתובת שרת NetworkMonitor. מומלץ לשלוח syslog ברמת
`warning` ‏(`informational` ב-Comware רושם כל התחברות וכל פקודה; ~75MB ליום לכל הודעה בשנייה).

| מכשיר | Syslog | Traps |
|---|---|---|
| HPE Comware 7 ‏(FF 5945 IRF) | `info-center enable`<br>`info-center loghost 10.0.0.50 facility local7`<br>`info-center source default loghost level warning`<br>`info-center loghost source Vlan-interface<mgmt>` | v2c: `snmp-agent target-host trap address udp-domain 10.0.0.50 params securityname <community> v2c`<br>`snmp-agent trap enable`<br>`snmp-agent trap source Vlan-interface<mgmt>`<br>v3: `snmp-agent usm-user v3 <user> <group> simple authentication-mode sha <pw> privacy-mode aes128 <pw>` + `snmp-agent target-host trap address udp-domain 10.0.0.50 params securityname <user> v3 privacy` (אותו משתמש ומפתחות של ה-polling) |
| ArubaOS-Switch | `logging 10.0.0.50`<br>`logging severity warning`<br>`logging facility local7` | `snmp-server host 10.0.0.50 community <community> trap-level all`<br>`snmp-server enable traps link-change <ports>`<br>v3: ‏`snmpv3 notify` / `snmpv3 targetaddress` (לפי מדריך המתג) |
| Check Point Gaia ‏(clish) | `add syslog log-remote-address 10.0.0.50 level warning`<br>`save config`<br>(הודעות מערכת ההפעלה Gaia בלבד — **לא** לוגי תעבורה של Log Exporter, שיציפו את המקלט) | `add snmp traps receiver 10.0.0.50 version v2 community <community>`<br>`set snmp traps trap <name> enable`<br>`save config` |

המכשיר צריך לשלוח מהכתובת שבה הוא מנוטר. אחרת מוסיפים את הכתובת הנוספת בטופס המכשיר (*Additional source
addresses*) או משתמשים ב-"Assign to device" בלשונית *Unknown sources* בדף הלוגים.

### Active Directory ‏(LDAPS)

ההתחברות עם משתמשי AD מאמתת את תעודת ה-DC מול אותן תעודות CA כמו HTTPS: מאגר המערכת של ה-image, ‏certifi,
והתיקייה `/etc/netmon-gitops/ca/` ‏(מוצמדת לקונטיינר כ-`/etc/netmon/certs`, כבר קיימת מ-`bootstrap.sh`). כלומר
תעודת ה-root של Mnidom-PKI-SERV-CA שכבר הותקנה מספיקה. ברשימת השרתים משתמשים בשם ה-DC שבתעודה
(`dc1.mnidom.net`, לא IP). בדיקה מהשרת:

```bash
openssl s_client -connect dc1.mnidom.net:636 -showcerts </dev/null | head -n 20
```

הכול מוגדר בממשק (*הגדרות ← Active Directory*); אין שום דבר להגדיר בשרת.

## שלב 3: flows, גיבוי קונפיגורציה, SSO

**אחרי השדרוג הכול כבוי.** מפעילים ב-*הגדרות ← תכונות*. בשרת יש רק ארבעה דברים: הפורטים של flows (כבר ב-`compose.yaml`),
ההפניה של השם הקצר ב-Caddy (כבר ב-`caddy/Caddyfile`), ה-proxy של האפליקציה ב-`app.env` ‏(ידני, בשרת בלבד) ו-NTP.

### Flows ‏(NetFlow / IPFIX / sFlow)

`compose.yaml` מפרסם **UDP 2055** ‏(NetFlow v5/v9 ו-IPFIX), ‏**UDP 4739** ‏(IPFIX) ו-**UDP 6343** ‏(sFlow v5), אחד לאחד
(כולם מעל 1024). שום דבר לא מאזין עד שמפעילים *Flows*. כל מה שנכתב למעלה על traps ו-syslog נכון גם כאן:

- כתובת השולח נשמרת (DNAT); בדיקה מהשרת עצמו מגיעה מ-`172.x.0.1`. ‏exporter של NetFlow/IPFIX מזוהה לפי כתובת המקור,
  ‏sFlow לפי ה-agent address שבתוך החבילה. exporter לא מוכר מופיע ב-*Flows ← Exporters* והנתונים שלו נזרקים עד שמאשרים.
- **conntrack:** ‏exporter שולח כל כמה שניות מאותו פורט מקור, כך שרשומה ישנה לא פגה לעולם. ‏`sync.sh` מנקה עכשיו
  גם את 2055/4739/6343 אחרי deploy שיצר מחדש את הקונטיינר.
- **חומת אש:** ‏`ufw` לא מסנן. ‏`ops/ingest-firewall.sh` ‏(אופציונלי) מכסה גם את שלושת הפורטים, עם אותה רשימת רשתות
  ב-`/etc/netmon-gitops/ingest-allow.conf`. אחרי עדכון: ‏`sudo systemctl restart netmon-ingest-firewall` ‏(אם הופעל).
- **נפח:** כ-0.4GB לכל exporter עמוס (raw ‏24 שעות, דקות 48 שעות, שעות 30 יום). ‏`status.sh` מציג את גודל מסד
  הנתונים ואת טבלאות ה-flows. מומלץ לייצא רק מה-core ‏(FF 5945 IRF, ‏sFlow) ומה-Check Point ‏(NetFlow v9).

הגדרת המכשירים (דוגמאות — **יש לבדוק על המכשיר**; ‏`10.0.0.50` = השרת). ב-NetFlow/IPFIX: ‏active timeout של 60 שניות
לכל היותר, ו-ingress בלבד (אחרת התעבורה נספרת פעמיים).

| מכשיר | Flow export |
|---|---|
| HPE Comware 7 ‏(FF 5945 IRF), ‏sFlow | `sflow agent ip <mgmt ip>`<br>`sflow source ip <mgmt ip>`<br>`sflow collector 1 ip 10.0.0.50 port 6343 description netmon`<br>בכל ממשק uplink: `sflow sampling-rate 4096` ו-`sflow flow collector 1` |
| ArubaOS-Switch, ‏sFlow | `sflow 1 destination 10.0.0.50 6343`<br>`sflow 1 sampling <ports> 4096` (התחביר משתנה לפי גרסה) |
| Check Point Gaia ‏(clish) | `add netflow collector ip 10.0.0.50 port 2055 export-format Netflow_V9`<br>`save config` ‏(או `IPFIX` לפורט 4739) |

### גיבוי קונפיגורציה

השרת מתחבר **החוצה** למכשירים ב-SSH ‏(TCP 22) ומריץ רק פקודות show/display. אין פורט נכנס ואין מה להגדיר בשרת. את
פרטי ההתחברות מזינים בממשק, והם נשמרים מוצפנים. חשבונות מומלצים לכל פלטפורמה: ‏`docs/architecture.md` §13.9.3.

### השם הקצר `https://netmonitor` מפנה ל-FQDN

עד עכשיו `https://netmonitor` הציג את האפליקציה. מעכשיו הוא **מפנה** ל-`https://netmonitor.mnidom.net`: ל-Kerberos
יש SPN רק לשם המלא, וגם ה-cookie ובדיקת ה-Origin צריכים שם אחד. התעודה ממשיכה לכלול את שני השמות (אין שינוי
ב-`make-csr.sh`), ולכן אין אזהרה. ב-Caddy אין שום הגדרה מיוחדת ל-SSO.

### Kerberos SSO — הוראות למנהל ה-AD ‏(פעם אחת)

1. **DNS:** ‏`netmonitor.mnidom.net` חייב להיות **רשומת A** ‏(לא CNAME — אחרת Chrome/Edge מבקשים כרטיס לשם היעד).
2. **חשבון שירות**, למשל `svc-netmon-http`: הסיסמה לא פגה, מסומן *This account supports Kerberos AES 256 bit
   encryption*, בלי הרשאות נוספות.
3. **SPN ו-keytab** — על DC, ב-cmd עם הרשאות מנהל:

<div dir="ltr">

```
setspn -S HTTP/netmonitor.mnidom.net MNIDOM\svc-netmon-http
ktpass /princ HTTP/netmonitor.mnidom.net@MNIDOM.NET /mapuser MNIDOM\svc-netmon-http /crypto AES256-SHA1 /ptype KRB5_NT_PRINCIPAL /pass <new password> /out netmon-http.keytab
setspn -L MNIDOM\svc-netmon-http
```

</div>

   ‏`ktpass` קובע את הסיסמה הזו לחשבון וממפה אליו את ה-principal, כך שה-salt של מפתח ה-AES ב-keytab תואם ל-AD.
   ‏`setspn -L` צריך להציג `HTTP/netmonitor.mnidom.net` אחד בלבד (`setspn -X` מוצא כפילויות). רק ל-FQDN יש SPN.
4. **העלאה:** ‏*הגדרות ← Single sign-on ← Kerberos* ← מעלים את `netmon-http.keytab`. ה-principal צריך להופיע עם
   etype 18 ‏(aes256-cts). אחר כך **מוחקים את הקובץ** — הוא שקול לסיסמת החשבון.
   ה-keytab **לא נשמר בשרת**: הוא מוצפן במסד הנתונים (ונכלל בגיבוי היומי), ונכתב ל-`/data/run/http.keytab`
   ‏(0600, בתוך ה-volume) רק כל עוד התכונה פעילה. ‏`run/` לא נכלל בגיבוי ה-volume ונוצר מחדש בכל הפעלה. החלפה: מריצים
   `ktpass` שוב עם סיסמה חדשה ומעלים את ה-keytab החדש.
5. **דפדפנים (GPO):** ‏`https://netmonitor.mnidom.net` באזור **Local intranet** ‏(*Site to Zone Assignment List*,
   ערך `1`; משמש את Edge ו-Chrome ב-Windows), או מדיניות Chrome/Edge ‏`AuthServerAllowlist = netmonitor.mnidom.net`.
   Firefox: ‏`network.negotiate-auth.trusted-uris`.
6. **שעון:** הפרש של יותר מ-5 דקות שובר את Kerberos. מסנכרנים את השרת ל-DC:

```bash
sudo mkdir -p /etc/systemd/timesyncd.conf.d
printf '[Time]\nNTP=dc1.mnidom.net dc2.mnidom.net\n' | sudo tee /etc/systemd/timesyncd.conf.d/netmon.conf
sudo systemctl restart systemd-timesyncd && timedatectl timesync-status
```

7. *Active Directory* ‏(שלב 2b-3) חייב להיות פעיל ומוגדר: התפקיד של משתמשי SSO נקבע לפי אותן קבוצות AD.

### Entra ID — רק אם רוצים "Sign in with Microsoft"

App registration ‏(single tenant), פלטפורמה **Web**, ‏redirect URI
`https://netmonitor.mnidom.net/api/auth/oidc/callback`; ‏client secret (מזינים בממשק את תאריך התפוגה שלו — מוצגת
אזהרה 30 יום לפני); ‏app roles ‏`NetMon.Admin` / `NetMon.Viewer`, משויכים למשתמשים/קבוצות ב-*Enterprise
applications*; ‏token configuration: ‏optional claims ‏`email`, ‏`preferred_username`. ה-proxy צריך לאפשר את
`login.microsoftonline.com`.

### proxy יוצא לאפליקציה (Telegram, ‏Teams, ‏Entra ID)

האפליקציה (httpx) משתמשת ב-`HTTPS_PROXY` / `NO_PROXY` מהקובץ `/etc/netmon-gitops/app.env` ‏(בשרת בלבד, לא ב-git).
SSH, ‏SNMP, ‏flows ו-LDAP לא עוברים דרך proxy לעולם. ‏`bootstrap.sh` יוצר את הקובץ רק בפעם הראשונה (בשרת חדש
`NO_PROXY` כולל גם את הדומיין, למשל `.mnidom.net`). בשרת הקיים עורכים ידנית:

<div dir="ltr">

```
HTTPS_PROXY=http://<proxy>:<port>
HTTP_PROXY=http://<proxy>:<port>
NO_PROXY=localhost,127.0.0.1,::1,db,netmon,proxy,.mnidom.net,<internal webhook targets>
```

</div>

ואז: ‏`cd /opt/netmon/deploy/production && sudo docker compose --env-file release.env up -d netmon` ‏(יוצר מחדש את
הקונטיינר עם הסביבה החדשה). ה-proxy חייב לאפשר **בקשות ארוכות (70 שניות ומעלה)** ל-`api.telegram.org` ‏(long polling
של *Chat acknowledge*). אם הוא מנתק אותן, מורידים את *Telegram poll timeout* בהגדרות (10–50, ברירת מחדל 50).

---

## שלב 3b: עוזר AI ‏"שאל את הרשת" ‏(Amazon Bedrock)

**אחרי השדרוג הכול כבוי.** בשרת אין קונטיינר חדש, אין פורט נכנס ואין חבילה חדשה. כל ההגדרות נעשות ב-AWS ובממשק
(*הגדרות ← עוזר*). המסמך באנגלית: ‏`docs/production.md` ‏(Phase 3b). החוזה: ‏`docs/architecture.md` ‏§14.9.

סדר העבודה: ‏(1) אזור ו-inference profile ← ‏(2) רשת ← ‏(3) ‏IAM Roles Anywhere ‏(או (4) מפתח גישה) ← ‏(5) ‏Knowledge Base
(אופציונלי) ← בממשק: מילוי הטופס ← **Test connection** ← המתג בתחתית הטופס ← ‏(6) תקרת עלות.

מה נוסף בתיקייה הזו:

| קובץ | תפקיד |
|---|---|
| `ops/assistant-net.sh` | ‏`check`: לכל כתובת של Bedrock / ‏Roles Anywhere / ‏KB ‏(ולכל שם של VPC endpoint) — ‏DNS, כתובת פרטית/ציבורית, האם האפליקציה תצא **ישירות** או **דרך ה-proxy** (אותם כללי `NO_PROXY` כמו באפליקציה), והאם יש מענה. ‏`no-proxy`: מוסיף ל-`NO_PROXY` ב-`app.env` ‏(פעם אחת). |
| `ops/status.sh` | סעיף חדש *Assistant*: המצב מ-`/api/health`, ה-proxy והאם `*.vpce.amazonaws.com` עוקף אותו, קבצי תעודה בשרת (אם נבחרו). |
| `aws/*.json` | תבניות IAM עם placeholders: ‏policy ל-Bedrock, ‏policy ל-KB ‏(רק אם יש KB), ‏policy לכפתור *List models* ‏(אופציונלי), ‏trust policy ל-Roles Anywhere, ‏policy ל-VPC endpoint. |
| `aws/render-policies.py` | ממלא את התבניות (Python בלבד, רץ גם במחשב Windows של מנהל ה-AWS). |

<div dir="ltr">

```bash
cd deploy/production/aws
python3 render-policies.py --account 111122223333 --region us-east-1 \
  --profile us.anthropic.<model> [--light-profile us.anthropic.<light-model>] \
  [--ra-region <region>] [--trust-anchor-id <uuid | ARN>] [--kb-id <KB-ID>] [--cert-cn netmonitor.mnidom.net]
# -> ./out/*.json  (תבנית שחסר לה ערך מדולגת עם הודעה)
```

</div>

‏[1.4.1] ‏`--trust-anchor-id` מקבל את המזהה בלבד (uuid) **או** את ה-ARN המלא כפי שהוא מופיע בקונסולה; עם ARN הסקריפט בודק שהאזור והחשבון שבו תואמים ל-`--ra-region` ‏(או `--region`) ול-`--account` ועוצר עם הודעה ברורה אם לא. ‏`--cert-cn` חייב להיות **זהה בדיוק** ל-CN שב-Subject של התעודה שהונפקה (בדיקה: `openssl x509 -in netmonitor.cer -noout -subject` או `certutil -dump netmonitor.cer`); הסקריפט מדפיס איזה CN ואיזה trust anchor ה-trust policy שנוצר מצפה להם.

### 1. בחירת אזור ו-inference profile

ב-il-central-1 מודלי Claude זמינים **רק** דרך ה-inference profile הגלובלי (`global.`). שתי אפשרויות:

| | **א. us-east-1 + ‏`us.` ‏(מומלץ)** | **ב. il-central-1 + ‏`global.`** |
|---|---|---|
| איפה השאלות מעובדות | רק באזורי ארה"ב שב-profile | בכל אזור מסחרי של AWS בעולם |
| בטופס | אזור `us-east-1`, מודל `us.anthropic.<model>` | אזור `il-central-1`, מודל `global.anthropic.<model>` |
| רשת מהמשרד | צריך נתיב ל-us-east-1 ‏(Direct Connect gateway, ‏TGW peering או ה-endpoint הציבורי דרך ה-proxy — סעיף 2) | ‏VPC endpoint ב-VPC הקיים ב-il-central-1 שכבר מגיעים אליו ב-Direct Connect — הכי פשוט |
| זמן תגובה | ‏~150ms נוספים לכל round trip — זניח לעומת זמן המודל (שניות) | הכי קרוב ל-endpoint; המודל עצמו עשוי לרוץ רחוק |
| מדיניות | גאוגרפיה ברורה (ארה"ב) לצוות אבטחת המידע | צריך לאשר "בכל מקום"; ‏SCP שמגביל אזורים צריך לאפשר global inference |
| מחיר | לפי דף התמחור של AWS ל-profile של ארה"ב | לפי דף התמחור ל-profile הגלובלי (AWS תמחרו global ו-geographic אחרת — להשוות) |

בשני המקרים: ‏Bedrock לא שומר את השאלות כברירת מחדל (סעיף 6), החשבונית וה-metrics הם באזור המקור, ואפשר להחליף
בהמשך (נדרש Test חדש).

**הפעלת גישה למודל** (פעם אחת לכל חשבון AWS, באזור שנבחר): קונסולת Bedrock ← מעבר לאזור ← *Model catalog* ←
*Anthropic* ← מודל Claude עדכני ← אם מתבקש: מילוי **טופס ה-use case** של Anthropic ו/או *Request model access*
(הממשק השתנה עם הזמן: בקונסולות חדשות המודל נפתח בשימוש הראשון, אבל ל-Anthropic עדיין נדרש הטופס החד-פעמי).
את השלב הזה עושה מנהל ה-AWS עם הרשאות admin ‏(SCP שחוסם `aws-marketplace:*` יכול לחסום את השימוש הראשון).

**מציאת ה-inference profile ID:** קונסולת Bedrock ← *Infer* ← *Cross-region inference* ‏(בגרסאות מסוימות:
*Inference profiles*) ← לשונית *System-defined* ← השורה של מודל Claude שנבחר עם הקידומת `us.` ‏(א) או `global.` ‏(ב)
← מעתיקים את **Inference profile ID** ‏(למשל `us.anthropic.claude-<model>-v1:0`) ושומרים את ה-**ARN** בשביל ה-policy.
ב-CLI:

<div dir="ltr">

```bash
aws bedrock list-inference-profiles --region us-east-1 \
  --query "inferenceProfileSummaries[?contains(inferenceProfileId,'anthropic')].[inferenceProfileId,status]" --output table
```

</div>

*מודל קל* (אופציונלי): ‏profile של מודל Claude זול יותר מאותה רשימה, לתכונות הרקע (הערות להתראות, קורלציה, digest,
סיכומי דוחות). מזהה מודל "חשוף" (בלי `us.` / `global.`) נכשל עם "on-demand throughput isn't supported"
(`bedrock_model_invalid`).

**SCP:** ‏cross-region inference נבדק גם באזורי היעד. אם הארגון מגביל אזורים (`aws:RequestedRegion`), מנהל ה-AWS
צריך לאפשר את אזורי ה-profile של ארה"ב (א), או לפעול לפי התיעוד של AWS ל-global inference profiles ‏(ב).

### 2. רשת

| כתובת (באזור שנבחר `<r>`) | מתי |
|---|---|
| `bedrock-runtime.<r>.amazonaws.com` ‏(או שם ה-VPC endpoint) | תמיד |
| `rolesanywhere.<r>.amazonaws.com` ‏(או VPC endpoint) | ‏IAM Roles Anywhere |
| `bedrock-agent-runtime.<r>.amazonaws.com` | רק עם Knowledge Base |
| `bedrock.<r>.amazonaws.com` | רק לכפתור *List models* ‏(אופציונלי) |

הכול HTTPS/443 **יוצא** מהשרת; שום דבר נכנס. **שעון:** ‏AWS דוחה בקשות עם הפרש של יותר מ-5 דקות — NTP מול ה-DC
(כמו ב-Kerberos; ‏`status.sh` ← *Time*).

**VPC interface endpoints ‏(מומלץ).** באזור ה-endpoint שקוראים לו (א: us-east-1, ‏ב: il-central-1): קונסולת VPC ←
*Endpoints* ← *Create endpoint* ← *AWS services* ← `com.amazonaws.<r>.bedrock-runtime` ‏(ועוד
`com.amazonaws.<r>.rolesanywhere` אם הוא קיים באזור ורוצים גם את Roles Anywhere בפרטי; ‏
`com.amazonaws.<r>.bedrock-agent-runtime` ל-KB) ← subnets בשני AZ ← **security group: נכנס TCP 443 מכתובת שרת
NetMonitor** ‏(`/32`; טווחי המשרד רק אם צריך) ← policy: ‏*Custom* עם `aws/vpc-endpoint-policy.json` המלא (רק ה-role/user
של NetMonitor).

**הגעה ל-us-east-1 מהמשרד (אפשרות א).** ה-Direct Connect נוחת היום ב-il-central-1. לפי סדר עדיפות:
1. **Direct Connect gateway ‏(גלובלי) ← us-east-1.** ‏DX gateway לא שייך לאזור: משייכים את ה-DX gateway הקיים
   (ב-transit VIF — ל-Transit Gateway ב-us-east-1; ב-private VIF — ל-virtual private gateway של VPC ב-us-east-1)
   ומוסיפים את ה-CIDR של ה-VPC ב-us-east-1 ל-*allowed prefixes*. הנתב במשרד לומד אותו ב-BGP, והתעבורה עוברת
   ב-backbone של AWS. ‏DX gateway לא מנתב בין ה-VPCs שמשויכים אליו — לא צריך את זה כאן (רק משרד ↔ us-east-1). ‏CIDRs
   בלי חפיפה.
2. **Transit Gateway inter-region peering.** ‏peering בין ה-TGW ב-il-central-1 ‏(שכבר מגיעים אליו ב-DX) ל-TGW
   ב-us-east-1; ‏routes **סטטיים** בשני הצדדים (ל-peering אין propagation) ל-CIDR של us-east-1 ולטווחי המשרד, ופרסום
   ה-CIDR של us-east-1 למשרד ב-DX. העברת נתונים בין אזורים בתשלום לפי GB ‏(זניח לטקסט).
3. **ה-endpoint הציבורי דרך ה-proxy הארגוני** (בלי עבודת רשת ב-AWS): *Endpoint URL* ריק, *Proxy: from environment*
   ‏(או *custom*), ומאשרים ב-proxy את `bedrock-runtime.us-east-1.amazonaws.com` ו-`rolesanywhere.<r>.amazonaws.com`.
   התעבורה מוצפנת מקצה לקצה וחתומה (SigV4). ה-proxy לא צריך "לאגור" תשובות זורמות (Test מזהיר `stream_buffered`).
   ‏public VIF ב-Direct Connect, אם קיים, מגיע גם הוא ל-endpoints הציבוריים.

VPC peering **לא** מתאים לתעבורה מהמשרד (לא טרנזיטיבי).

**DNS.** עדיף להזין ב-*Endpoint URL* את **השם הייחודי של ה-endpoint** מלשונית *Details*
(`https://vpce-0abc…-xyz.bedrock-runtime.<r>.vpce.amazonaws.com`, בלי path): הוא נפתר ב-DNS הציבורי לכתובות הפרטיות
של ה-endpoint, ולכן לא צריך שינוי DNS. רק אם השם האזורי (`bedrock-runtime.<r>.amazonaws.com`) חייב להיפתר לכתובת
פרטית: *private DNS* ב-endpoint, ‏Route 53 Resolver **inbound endpoint** באותו VPC, ו-**conditional forwarder** ב-DNS
של ה-AD לשם המדויק הזה (forwarder לכל `amazonaws.com` ישבור גישה לשירותי AWS אחרים). אותו דבר ל-`rolesanywhere`.

**proxy ו-NO_PROXY.** כשיש proxy ב-`app.env`, שם של VPC endpoint חייב לעקוף אותו (אחרת הבקשה יוצאת לאינטרנט, או
נכשלת כי ה-proxy לא מכיר את השם). אחת משתיים: *Proxy: direct* בטופס (משפיע רק על קריאות ה-AWS של העוזר), או להשאיר
*from environment* ו:

<div dir="ltr">

```bash
sudo /opt/netmon/deploy/production/ops/assistant-net.sh no-proxy --restart .vpce.amazonaws.com
# שמות אזוריים שנפתרים לכתובת פרטית (private DNS), למשל:
sudo /opt/netmon/deploy/production/ops/assistant-net.sh no-proxy --restart bedrock-runtime.il-central-1.amazonaws.com rolesanywhere.il-central-1.amazonaws.com
# בדיקה (קריאה בלבד): DNS, ישיר/proxy, מענה
sudo /opt/netmon/deploy/production/ops/assistant-net.sh check --region us-east-1 --host vpce-0abc…-xyz.bedrock-runtime.us-east-1.vpce.amazonaws.com
```

</div>

בשרת חדש `bootstrap.sh` כבר מכניס `.vpce.amazonaws.com` ל-`NO_PROXY`. ‏*Test connection* בממשק מציג את הכתובת,
‏`via: direct|proxy …`, סוג הכתובת (ב-DX מצופה `private`) ומזהיר `vpce_via_proxy`.
בהתקנה בלי Docker: ‏`install.sh --https-proxy …` / ‏`install.ps1 -HttpsProxy …` מוסיפים תמיד `.vpce.amazonaws.com`;
שמות אזוריים פרטיים מוסיפים עם `--no-proxy` / ‏`-NoProxy`.

### 3. ‏IAM Roles Anywhere ‏(מומלץ)

השרת מקבל מ-AWS הרשאות **זמניות** (ברירת מחדל שעה) בעזרת תעודה מה-PKI הפנימי — אין סוד קבוע. יוצרים הכול באזור
אחד שבו IAM Roles Anywhere זמין (בדרך כלל אזור ה-Bedrock מסעיף 1; לבדוק שהשירות קיים שם) — זה *Roles Anywhere region*
בטופס.

1. **Trust anchor.** מנהל ה-PKI מייצא את תעודת ה-**CA המנפיק** ‏*Mnidom-PKI-SERV-CA* כ-Base-64 ‏(PEM) ‏(קונסולת ה-CA ←
   Properties ← *View certificate* ← *Copy to file* ← Base-64, או `certutil -ca.cert serv-ca.cer` ואז `certutil -encode`).
   מנהל ה-AWS: ‏IAM Roles Anywhere ← *Create a trust anchor* ← שם `mnidom-pki-serv-ca` ← *External certificate bundle* ←
   מדביקים את ה-PEM. (אם בוחרים ב-PKI-ROOT-CA כ-anchor, מעלים באפליקציה את ה-CA המנפיק כ-chain.) לפני חידוש תעודת
   ה-CA עצמה — מוסיפים את התעודה החדשה ל-bundle, כך ששתיהן מוכרות בזמן המעבר.
2. **ביטול תעודות (CRL).** ‏Roles Anywhere **לא** מוריד CRL מה-CDP בעצמו. מייבאים את ה-CRL של ה-CA ומרעננים אותו לפי
   לוח הפרסום של ה-CA (למשל משימה מתוזמנת על ה-CA או על מחשב ניהול):

   <div dir="ltr">

   ```bash
   aws rolesanywhere import-crl --region <ra-region> --name mnidom-pki-serv-ca --enabled \
     --crl-data fileb://Mnidom-PKI-SERV-CA.crl --trust-anchor-arn <trust-anchor-arn>
   aws rolesanywhere update-crl --region <ra-region> --crl-id <crl-id> --crl-data fileb://Mnidom-PKI-SERV-CA.crl
   ```

   </div>

   בלי ייבוא CRL — מסתמכים על תוקף קצר של התעודה. עצירת חירום: *Disable* ל-profile או ל-trust anchor.
3. **Role** בשם `netmon-bedrock`: ‏IAM ← Roles ← *Create role* ← *Custom trust policy* ← מדביקים את
   `aws/rolesanywhere-trust-policy.json` המלא. הוא סומך רק על `rolesanywhere.amazonaws.com`, רק דרך ה-trust anchor
   הזה (`aws:SourceArn`), ורק על תעודה עם subject ‏`CN=netmonitor.mnidom.net` שהונפקה על ידי `CN=Mnidom-PKI-SERV-CA`
   (`aws:PrincipalTag/x509Subject/CN`, ‏`…/x509Issuer/CN`; מוודאים את שני הערכים על התעודה שהונפקה:
   ‏`openssl x509 -in netmonitor.cer -noout -subject -issuer`). הרשאות: ‏inline policy מתוך
   `aws/bedrock-invoke-policy.json` המלא; ‏`aws/bedrock-knowledge-base-policy.json` **רק** אם משתמשים ב-KB;
   ‏`aws/bedrock-list-models-policy.json` רק לכפתור *List models*.
   - ‏Converse ו-ConverseStream מורשים דרך `bedrock:InvokeModel` ו-`bedrock:InvokeModelWithResponseStream` ‏(אין פעולת
     IAM נפרדת בשם `bedrock:Converse`). שום דבר אחר: לא `bedrock:*`, לא כתיבה, לא `iam:*`.
   - ההצהרה הראשונה מתירה את ה-inference profile שנבחר; השנייה מתירה את המודל עצמו **בכל אזור** (profile חוצה אזורים
     מנתב לכמה אזורים; ה-`*` מכסה גם את ה-ARN בלי אזור של profiles גלובליים) — **רק** כשהקריאה עוברת דרך ה-profile
     הזה (`bedrock:InferenceProfileArn`).
   - *Maximum session duration* של ה-role ≥ משך ה-session בטופס (ברירת מחדל 3600 שניות).
4. **Profile:** ‏IAM Roles Anywhere ← *Create a profile* ← שם `netmonitor` ← role ‏`netmon-bedrock` ← משך session שעה
   (≥ ההגדרה בטופס). רושמים את שלושת ה-ARNs: ‏trust anchor, ‏profile, ‏role.
5. **התעודה — CSR שנוצר באפליקציה.** *הגדרות ← עוזר* ← אימות *IAM Roles Anywhere* ← **יצירת CSR**: האפליקציה יוצרת
   את המפתח הפרטי (הוא לא יוצא מהשרת; נשמר מוצפן ב-DB) ומורידה `netmonitor.csr` עם `CN=netmonitor.mnidom.net`.
   מנהל ה-PKI מנפיק מתבנית עם: ‏key usage *Digital Signature*; ‏EKU **Client Authentication**; ‏subject *Supply in the
   request*; ‏RSA 2048 ומעלה או ECDSA P-256/P-384; ‏SHA-256; תוקף שנה (עד שנתיים). למשל: שכפול התבנית *Computer* בשם
   `NetMonBedrockClient` ‏(Compatibility ‏2008 ומעלה; *Extensions ← Application Policies* = רק Client Authentication;
   *Subject Name* = Supply in the request), פרסום התבנית, ואז:

   <div dir="ltr">

   ```
   certreq -submit -config "<ca-host>\Mnidom-PKI-SERV-CA" -attrib "CertificateTemplate:NetMonBedrockClient" netmonitor.csr netmonitor.cer
   ```

   </div>

   מעלים את `netmonitor.cer` ‏(PEM או DER; ‏chain רק אם ה-anchor הוא ה-root) באותו טופס — האפליקציה בודקת שהתעודה
   מתאימה למפתח ומציגה subject, ‏issuer ותפוגה. חלופות: העלאת `.pfx` מ-AD CS, או קבצים בשרת:
   ‏`NETMON_AWS_RA_CERT_FILE=/etc/netmon/aws/cert.pem` ‏(וגם `_KEY_FILE`, ‏`_CHAIN_FILE`) ב-`app.env`; הקבצים
   ב-`/etc/netmon-gitops/aws/`, המפתח `0600` בבעלות uid ‏`10001`.
6. **בטופס:** אזור, אזור Roles Anywhere, שלושת ה-ARNs, מזהה המודל (סעיף 1), ‏Endpoint URL ו-endpoint של Roles Anywhere
   (סעיף 2; ריק = ציבורי), מצב proxy ← **Test connection** ← המתג בתחתית.
7. **חידוש.** ‏30 יום לפני התפוגה יש אזהרה (בממשק, אירוע `system` יומי, ו-`status.sh` לקבצים בשרת): שוב *יצירת CSR*
   (מפתח חדש) ← הנפקה ← העלאה ← Test. ב-AWS לא משנים כלום כל עוד ה-CN וה-CA המנפיק זהים. אחרי המעבר מבטלים
   (revoke) את התעודה הישנה ב-CA ומרעננים את ה-CRL ב-AWS.

### 4. חלופה: מפתח גישה קבוע (access key)

רק אם אי אפשר Roles Anywhere. ‏IAM ← Users ← `netmon-bedrock-key`, **בלי גישה לקונסולה**, ‏inline policy = אותו
`aws/bedrock-invoke-policy.json` המלא (+ ‏policy של KB אם יש). *Security credentials* ← *Create access key* ←
*Application running outside AWS*. בטופס: אימות *Access key*, ה-Key ID, הסוד (נשמר מוצפן, לא מוצג שוב) ותאריך
היצירה (תזכורת אחרי 90 יום). החלפה כל 90 יום: יוצרים מפתח שני ← מזינים ← Test ← *Deactivate* לישן ← מוחקים אחרי
שבוע. אפשר להוסיף תנאי IAM על `aws:SourceVpce` ‏(VPC endpoint) או `aws:SourceIp` ‏(endpoint ציבורי — כתובת היציאה של
ה-proxy). אם יש policy ל-VPC endpoint — מוסיפים לו גם את ה-ARN של המשתמש.

### 5. ‏Knowledge Base ‏(אופציונלי, התכונה `ai_knowledge`)

1. ‏S3 bucket **באותו אזור** של Bedrock ‏(Block public access, הצפנה SSE-S3 או SSE-KMS) עם נהלים ו-runbooks ‏(PDF,
   ‏DOCX, ‏HTML, ‏MD, ‏TXT). **בלי סודות במסמכים** — קטעים מהם מוצגים למנהלים ונשלחים למודל.
2. ‏Bedrock ← *Knowledge Bases* ← *Create* ← מקור S3 ← מודל embeddings **רב-לשוני** (עברית + אנגלית) ← vector store
   (להשוות את העלות החודשית המינימלית של האפשרויות לפני הבחירה) ← *Sync*; לתזמן סנכרון או לסנכרן אחרי כל שינוי.
3. מעתיקים את **Knowledge Base ID**, ממלאים את ה-policies עם `--kb-id` ומצרפים את
   `bedrock-knowledge-base-policy.json` ל-role/user ‏(`bedrock:Retrieve` על ה-KB הזה בלבד).
4. רשת: ‏VPC endpoint ‏`com.amazonaws.<r>.bedrock-agent-runtime` ‏(או אישור `bedrock-agent-runtime.<r>.amazonaws.com`
   ב-proxy).
5. *הגדרות ← עוזר ← Knowledge Base*: ‏KB ID ו-endpoint ← **Test** ← המתג בתחתית.

### 6. עלות, תקרה חודשית, טיפול בנתונים

- עלות = טוקנים נכנסים × מחיר + טוקנים יוצאים × מחיר (+ cache). שאלה טיפוסית: ‏6,000–20,000 טוקנים נכנסים ב-2–4 קריאות
  למודל ו-300–900 יוצאים. במחירים *להמחשה* של 3 / 15 דולר למיליון טוקנים — כ-0.03 דולר לשאלה, כדולר ליום ל-30 שאלות.
- בממשק (*הגדרות ← עוזר ← שימוש ותקרות*): מזינים את **המחירים** של המודל וה-profile שנבחרו מדף התמחור של Amazon
  Bedrock ‏(דולר למיליון טוקנים; האפליקציה לא מגיעה עם מחירים), ואז **תקרת עלות חודשית בדולרים** (למשל 50). בנוסף:
  תקרת טוקנים (ברירת מחדל 5M בחודש), שאלות ליום לכל מנהל (50), ועצירת רקע (80%: תכונות הרקע נעצרות קודם, השימוש
  האינטראקטיבי ב-100%). בלי מחירים פועלת רק תקרת הטוקנים.
- שומר שני ב-AWS: ‏*AWS Budgets* ← cost budget מסונן לשירות Amazon Bedrock עם התראת מייל ב-80% / ‏100%. אחרי כמה ימים
  משווים את דף השימוש באפליקציה ל-Cost Explorer.
- **טיפול בנתונים — לאימות של צוות אבטחת המידע** מול תנאי השירות העדכניים של AWS, ה-DPA של AWS וההגדרות של החשבון
  (האפליקציה לא נותנת הצהרת תאימות משלה): לפי התיעוד של AWS בזמן הכתיבה, Amazon Bedrock לא שומר שאלות ותשובות כברירת
  מחדל, לא משתמש בהן לאימון מודלים ולא משתף אותן עם ספק המודל; העיבוד נעשה באזור המקור או באזורי ה-profile שנבחר
  (`us.` — אזורי ארה"ב; ‏`global.` — כל אזור מסחרי); **model invocation logging** הוא אופציה ברמת החשבון (*Bedrock ←
  Settings*) שאם הופעלה — כותבת שאלות ותשובות ל-CloudWatch Logs / ‏S3 של החשבון. מה NetMonitor שולח ומה הוא מסנן:
  ‏`docs/architecture.md` ‏§14.6.1.

### תקלות — עוזר AI

| שלב ב-Test / קוד | מה לבדוק |
|---|---|
| ‏`endpoint` נכשל, ‏`aws_network` | ‏`assistant-net.sh check …`; ה-SG של ה-endpoint מתיר 443 מהשרת; ניתוב ב-DX; ‏`NO_PROXY` / *Proxy: direct*; ה-CA הארגוני ב-`/etc/netmon-gitops/ca` אם ה-proxy פותח TLS. |
| אזהרת `vpce_via_proxy` | ‏`assistant-net.sh no-proxy --restart .vpce.amazonaws.com` או *Proxy: direct*. |
| ‏`rolesanywhere_failed` ‏"Untrusted certificate" | ב-trust anchor יש את ה-CA המנפיק? הועלה chain כשה-anchor הוא ה-root? ה-CRL מסמן את התעודה כמבוטלת? |
| ‏`rolesanywhere_failed` ‏"Unable to assume role" | ‏CN / ‏issuer CN / ‏ARN של ה-trust anchor ב-trust policy; ה-role ב-profile; משך session ≤ המקסימום של ה-profile/role. |
| ‏`aws_clock_skew` | ‏NTP ‏(`status.sh` ← Time). |
| ‏`bedrock_model_access` | גישה למודל / טופס use case של Anthropic באותו אזור (סעיף 1). |
| ‏`bedrock_model_invalid` | מזהה **profile** ‏(`us.…` / ‏`global.…`), לא מזהה מודל חשוף; ה-profile קיים באזור. |
| ‏`bedrock_access_denied` | ה-ARNs ב-policy ‏(אזור, חשבון, profile, מודל); ‏policy של ה-VPC endpoint; ‏SCP ל-cross-region inference. |
| אזהרת `stream_buffered` | ה-proxy אוגר תשובות — ‏VPC endpoint עם *Proxy: direct*, או חריגה ב-proxy. |
