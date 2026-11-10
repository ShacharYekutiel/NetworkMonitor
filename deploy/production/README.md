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
  ‏`install-cert.sh`, ‏`status.sh`, ‏`ingest-firewall.sh` ‏(אופציונלי: מי רשאי לשלוח traps/syslog/flows).
- `systemd/` — ה-timers: סנכרון כל 2 דקות, גיבוי יומי ב-02:17; ‏`netmon-ingest-firewall.service` ‏(מותקן,
  **לא** מופעל — רק אם בוחרים בו).
- `tests/` — בדיקת הלוגיקה של `sync.sh` בלי Docker ‏(רצה ב-CI).

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
