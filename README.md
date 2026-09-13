# v1-feed

מאזין כל 5 דקות ל‑`https://www.mako.co.il/v1/?platform=mobileApp`, מזהה דפים חדשים ב‑`www.v-1.co.il`,
ומפרסם דרך GitHub Pages:

| קובץ | מה זה |
|---|---|
| `docs/feed.xml` | RSS 2.0 עם הצהרת WebSub Hub (`pubsubhubbub.appspot.com`) |
| `docs/news-sitemap.xml` | מפת אתר ניוז של גוגל, 48 השעות האחרונות, עד 1,000 כתובות |
| `docs/sitemap.xml` | מפת אתר רגילה עם כל הכתובות שנראו, `lastmod` רק כשיש תאריך מפורש |
| `state/seen.json` | זיכרון: כל כתובת שנראתה, התאריך שלה ומקורו |

אחרי כל שינוי הסקריפט ממתין ש‑GitHub Pages יגיש את הגרסה החדשה ואז שולח `hub.mode=publish` ל‑Hub של גוגל.

## מדיניות תאריכים (קפדנית)

דף נכנס לפיד ולמפת הניוז **רק** אם יש לו תאריך פרסום מפורש:

1. `publishDate` ב‑JSON של האפליקציה (המקור הראשי, גובר תמיד).
2. אם אין, מושכים את הדף פעם אחת וקוראים `uploadDate` / `datePublished` מה‑JSON‑LD שה‑`url` שלו זהה לכתובת הדף.
3. אין גם שם → הדף לא נכנס לפיד ולא לניוז‑סייטמאפ (מופיע רק ב‑`sitemap.xml` בלי `lastmod`).

אין ניחוש תאריך משמות קבצים, מנתיב תמונה, או מ"מתי הפולר ראה את הדף לראשונה".

## איך זה רץ בפועל

הריצה כל 5 דקות מתבצעת **מהמחשב המקומי** דרך Windows Task Scheduler (משימה בשם `v1-feed poll`):
`run_hidden.vbs` → `run_local.cmd` (בלי חלון) → `build` → commit+push → `ping`. הלוג ב‑`logs/run.log`.
ה‑workflow בגיטהאב נשאר להרצה ידנית בלבד (Actions → poll-v1 → Run workflow); ה‑cron שלו בוטל.

ניהול המשימה: `schtasks /Query /TN "v1-feed poll" /V /FO LIST`, השבתה: `schtasks /Change /TN "v1-feed poll" /DISABLE`.

## הקמה (גיטהאב)

1. צור ריפו ציבורי בגיטהאב (למשל `v1-feed`) ודחוף אליו את התיקייה הזו.
2. **Settings → Pages**: Source = *Deploy from a branch*, Branch = `main`, Folder = `/docs`.
3. **Settings → Actions → General → Workflow permissions**: בחר *Read and write permissions*.
4. **Actions → poll-v1 → Run workflow** להרצה ראשונה (bootstrap). הריצה הראשונה רק לומדת את המצב הקיים ולא מפנגת.
5. מכאן ה‑cron רץ כל 5 דקות. כל ריצה שמצאה דפים חדשים מבצעת commit, ממתינה ל‑Pages ומפנגת את ה‑Hub.

כתובות התוצרים: `https://<user>.github.io/v1-feed/feed.xml`, `.../news-sitemap.xml`, `.../sitemap.xml`.

### משתנים אופציונליים (Settings → Secrets and variables → Actions)

| שם | סוג | ברירת מחדל | הסבר |
|---|---|---|---|
| `FEED_BASE_URL` | Variable | `https://<owner>.github.io/<repo>` | לשנות רק אם יש דומיין מותאם ל‑Pages |
| `PUBLICATION_NAME` | Variable | `V1` | שם הפרסום ב‑`news:publication` |
| `PUBLICATION_LANG` | Variable | `he` | שפת הפרסום |
| `INDEXNOW_KEY` | Secret | ריק | אם מוגדר, שולח את הכתובות החדשות גם ל‑IndexNow (Bing/Yandex). דורש שהקובץ `https://www.v-1.co.il/<key>.txt` יהיה קיים |

## הגשה לגוגל

- **Search Console**: מפת הניוז יושבת על `github.io` אבל מצביעה על `v-1.co.il`. גוגל מקבלת cross‑domain sitemap רק אם שני ה‑hosts מאומתים באותו חשבון Search Console (מאמתים גם את ה‑property של `<user>.github.io`), או אם מוסיפים שורת `Sitemap: https://<user>.github.io/v1-feed/news-sitemap.xml` ל‑`robots.txt` של `v-1.co.il`.
- **WebSub**: לא דורש הגדרה בצד גוגל. הפינג נשלח אוטומטית.

## הרצה מקומית

```bash
FEED_BASE_URL=https://<user>.github.io/v1-feed python scripts/build_feed.py build
FEED_BASE_URL=https://<user>.github.io/v1-feed python scripts/build_feed.py ping
```

כל ההגדרות ניתנות לשינוי דרך משתני סביבה (ראה ראש הקובץ `scripts/build_feed.py`):
`MAX_FEED_ITEMS` (100), `NEWS_WINDOW_HOURS` (48), `ENRICH_FROM_PAGE` (1), `ENRICH_MAX_PER_RUN` (30), `ENRICH_MAX_AGE_DAYS` (7), `PAGES_WAIT_SECONDS` (240).

## הערות

- ה‑cron של GitHub Actions הוא "לפחות 5 דקות", ובשעות עומס ריצות עלולות להתעכב או להידלג. אם צריך דיוק, מריצים את אותו סקריפט משרת אחר עם cron ודוחפים לריפו.
- מחיקת `state/seen.json` גורמת ל‑bootstrap מחדש בריצה הבאה.
