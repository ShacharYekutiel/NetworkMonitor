# NetworkMonitor — סביבת Production ‏(GitOps)

התיקייה `deploy/production/` היא **ההגדרה המלאה של שרת ה-production**. השרת מושך את ה-repo
כל 2 דקות ומיישם בעצמו כל שינוי שנכנס לתיקייה הזו ב-`main`. אף אחד לא מתחבר לשרת כדי "להעלות גרסה".

- `release.env` — **הקובץ שמשנים כדי לשדרג**: הגרסה המדויקת ותמונת ה-Docker ‏(אפשר עם digest).
  לעולם לא `latest`.
- `compose.yaml` — ה-stack: ‏`init` ‏(יוצר סיסמת DB אקראית), ‏`db` ‏(TimescaleDB), ‏`netmon` ‏(האפליקציה,
  לא חשופה ישירות), ‏`proxy` ‏(Caddy: ‏HTTPS על 443, הפניה מ-80 ל-443, WebSocket).
- `config/netmon.env` — הגדרות אפליקציה שאינן סודיות (אזור זמן `Asia/Jerusalem` וכו').
- `caddy/Caddyfile` — הגדרות ה-reverse proxy.
- `ops/` — סקריפטים לשרת: ‏`bootstrap.sh`, ‏`sync.sh`, ‏`backup.sh`, ‏`restore.sh`, ‏`make-csr.sh`,
  ‏`install-cert.sh`, ‏`status.sh`.
- `systemd/` — ה-timers: סנכרון כל 2 דקות, גיבוי יומי ב-02:17.
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
| `/etc/netmon-gitops/proxy.env`, `app.env` | proxy (אם הוגדר); הגדרות מקומיות לאפליקציה |
| `/etc/netmon-gitops/ca/` | תעודות CA שהאפליקציה סומכת עליהן (מועתקות מ-`/usr/local/share/ca-certificates`) |
| `/etc/netmon-gitops/notify.env` | webhook להתראות (אופציונלי) |
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

## בעתיד: SNMP traps ו-syslog ‏(שלב 2b-3)

ב-`compose.yaml` יש placeholder בהערה לפורטים UDP 162 ‏(traps) ו-UDP/TCP 514 ‏(syslog). פורטים
שמפורסמים ב-Docker ‏(iptables DNAT) שומרים את כתובת ה-IP של השולח, וזה הכרחי לזיהוי המכשיר.
יש לפתוח אותם גם ב-firewall של השרת כשהשלב ייכנס.
