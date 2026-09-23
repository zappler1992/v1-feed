# v1-feed

**קבצים חיים:** [feed.xml](https://zappler1992.github.io/v1-feed/feed.xml) · [news-sitemap.xml](https://zappler1992.github.io/v1-feed/news-sitemap.xml) · [video-sitemap.xml](https://zappler1992.github.io/v1-feed/video-sitemap.xml) · [sitemap.xml](https://zappler1992.github.io/v1-feed/sitemap.xml) · [mako/feed.xml](https://zappler1992.github.io/v1-feed/mako/feed.xml) · [mako/all.xml](https://zappler1992.github.io/v1-feed/mako/all.xml) · [mako/food/sitemap-index.xml](https://zappler1992.github.io/v1-feed/mako/food/sitemap-index.xml) · [דף אינדקס](https://zappler1992.github.io/v1-feed/)

מאזין כל 5 דקות ל‑`https://www.mako.co.il/v1/?platform=mobileApp`, מזהה דפים חדשים ב‑`www.v-1.co.il`,
ומפרסם דרך GitHub Pages:

| קובץ | מה זה |
|---|---|
| `docs/feed.xml` | RSS 2.0 עם הצהרת WebSub Hub (`pubsubhubbub.appspot.com`) |
| `docs/news-sitemap.xml` | מפת אתר ניוז של גוגל, 48 השעות האחרונות, עד 1,000 כתובות |
| `docs/sitemap.xml` | מפת אתר רגילה עם כל הכתובות שנראו, `lastmod` רק כשיש תאריך מפורש |
| `docs/video-sitemap.xml` | מפת וידאו לגוגל: thumbnail, כותרת, תיאור, קובץ HLS, משך, תאריך, tags (מדורים) |
| `state/seen.json` | זיכרון: כל כתובת שנראתה, התאריך שלה ומקורו |
| `docs/mako/all.xml` | **mako.co.il + Fashion Forward**: RSS של *כל* הכתבות המקושרות מדף הבית (לא רק הסליידר) ושל הפיד של `fashionforward.mako.co.il`, ללא ממומן. כל כתובת חדשה נשלחת ל‑**IndexNow** (Bing/Yandex). `scripts/build_mako_all.py`, זיכרון ב‑`state/mako_seen.json` |
| `docs/mako/feed.xml` | **mako.co.il**: RSS‑snapshot של הכתבה הראשית + הסליידר בדף הבית, ללא הטיזר הממומן (`scripts/build_mako_home.py`, מקור `https://www.mako.co.il/?platform=mobileApp`, תאריכים מ‑`date.datetime`). מפונג ל‑Hub בכל שינוי |

הפיד הוא Media RSS: לכל פריט `category` (המדור שבו הוא מוצג + מדור ה‑URL), `content:encoded` עם תמונה ותיאור,
`media:content` של הווידאו (HLS, משך) עם `media:thumbnail`, `media:keywords` ו‑`media:rating`.
כל השדות נשלפים מה‑JSON של המקור בלבד.

אחרי כל שינוי הסקריפט ממתין ש‑GitHub Pages יגיש את הגרסה החדשה ואז שולח `hub.mode=publish` ל‑Hub של גוגל.

## מדיניות תאריכים (קפדנית)

דף נכנס לפיד ולמפת הניוז **רק** אם יש לו תאריך פרסום מפורש:

1. `publishDate` ב‑JSON של האפליקציה (המקור הראשי, גובר תמיד).
2. אם אין, מושכים את הדף פעם אחת וקוראים `uploadDate` / `datePublished` מה‑JSON‑LD שה‑`url` שלו זהה לכתובת הדף.
3. אין גם שם → הדף לא נכנס לפיד ולא לניוז‑סייטמאפ (מופיע רק ב‑`sitemap.xml` בלי `lastmod`).

אין ניחוש תאריך משמות קבצים, מנתיב תמונה, או מ"מתי הפולר ראה את הדף לראשונה".

## איך זה רץ בפועל

שתי משימות ב‑Windows Task Scheduler, שתיהן `pythonw.exe` בלי חלון:

| משימה | תדירות | מה היא עושה | לוג |
|---|---|---|---|
| `v1-feed poll` | 5 דקות | `run_local.py`: בונה את כל הפידים והמפות (V1 + mako), commit+push, ו‑`ping` ל‑Hub לכל פיד שהשתנה | `logs/run.log` |
| `v1-feed indexnow` | 2 דקות | `run_indexnow.py`: מריץ `build_mako_all.py submit` — רק גילוי כתובות חדשות, בדיקת הדף והגשה ל‑IndexNow. בלי קבצי פיד ובלי גיט | `logs/indexnow.log` |

הגשה ל‑IndexNow לא תלויה ב‑GitHub Pages, ולכן היא לא צריכה לחכות למחזור המלא. שתי המשימות לוקחות את אותו
מנעול (`scripts/runlock.py`, קובץ `logs/.run.lock`), כך שהן לא נוגעות ב‑`state/` בו‑זמנית; מי שלא קיבל את המנעול
מדלג על אותו טיק. מנעול ישן מ‑15 דקות נחשב שארית של ריצה שקרסה ונלקח.

ה‑workflow בגיטהאב נשאר להרצה ידנית בלבד (Actions → poll-v1 → Run workflow); ה‑cron שלו בוטל.

ניהול: `schtasks /Query /TN "v1-feed poll" /V /FO LIST`, השבתה: `schtasks /Change /TN "v1-feed indexnow" /DISABLE`.

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
- **מפת וידאו**: להגיש גם את `video-sitemap.xml` ב‑Search Console (אותו נכס).
- **WebSub**: לא דורש הגדרה בצד גוגל. הפינג נשלח אוטומטית.

## IndexNow למאקו

`scripts/build_mako_all.py` שולח כל כתובת חדשה שמופיעה בדף הבית של מאקו ל‑IndexNow (Bing, Yandex ושאר המנועים
שמשתתפים). המפתח מאומת בקובץ `https://www.mako.co.il/d5a26e08f7db8e599910507fb5dc73c4.txt` — **אסור למחוק אותו**,
אחרת ההגשות ייפסלו.

- כתובת נשלחת **פעם אחת בלבד**; `state/mako_seen.json` זוכר מה נשלח ומתי. כתובת שההגשה שלה נכשלה תנוסה שוב בריצה הבאה.
- נכללות רק כתובות שאינן ממומנות ושאינן חסומות ב‑`robots.txt` של אותו אתר.

### בדיקת הדף עצמו (לפני כל הגשה)

כל כתובת חדשה נמשכת **פעם אחת** ונבדקת, והתשובה נשמרת ב‑state כך שלא נבדקת שוב. נפסלות:

| סיבה | דוגמה |
|---|---|
| `noindex` (meta robots או X‑Robots‑Tag) | `/Sports-*` ו‑`news-sport` — שכפולים של כתבות sport5 |
| canonical שמצביע לכתובת אחרת | `/special-mako-news/...` שה‑canonical שלו הוא `/news-israel-elections/...`, ועמודי עונה ב‑VOD |
| הפניה לכתובת אחרת, או 4xx | |

מאקו מחזירה stub של 15KB בלי תגיות ה‑head למי ששולח `Accept: */*`, ולכן הבדיקה שולחת כותרות דפדפן מלאות.
תשובה בלי `robots` ובלי `canonical` נחשבת בדיקה שנכשלה (ננסה שוב, עד 3 פעמים) ולא "תקין" — כך שדף לא מוגש בטעות.

### דומיינים

IndexNow מתייחס לכל סאב‑דומיין כאתר נפרד, ולכן לכל אחד דרוש **קובץ מפתח משלו**. הסקריפט בודק את הקובץ לפני כל הגשה,
מגיש רק דומיינים שעברו אימות, ומשאיר את השאר בתור. ברגע שקובץ המפתח יעלה, ההגשה תצא לבד בריצה הבאה, בלי שינוי קוד.

| דומיין | מקור התוכן | קובץ מפתח | סטטוס |
|---|---|---|---|
| `www.mako.co.il` | דף הבית (`?platform=mobileApp`) | `https://www.mako.co.il/d5a26e08f7db8e599910507fb5dc73c4.txt` | מאומת, מגיש |
| `fashionforward.mako.co.il` | הפיד של האתר (`/feed/`) + טיזרים בדף הבית | `https://fashionforward.mako.co.il/d5a26e08f7db8e599910507fb5dc73c4.txt` | **חסר** — הכתובות ממתינות בתור |
- לכיבוי זמני: `MAKO_INDEXNOW=0`. להחלפת מפתח: `MAKO_INDEXNOW_KEY`.

## מפת אתר XML למאקו אוכל

**אינדקס:** [mako/food/sitemap-index.xml](https://zappler1992.github.io/v1-feed/mako/food/sitemap-index.xml) · [דף סטטוס](https://zappler1992.github.io/v1-feed/mako/food/) · `docs/mako/food/report.json`

מפת אתר נושאית לכל עמודי `/food-*` ב‑mako.co.il, נבנית מהמפה הכללית של מאקו ומנוקה בסריקה של כל כתובת
(`scripts/build_food_sitemap.py`). נכנסות רק כתובות שענו 200, קנוניות לעצמן וללא `noindex`; כתובות שחסומות
ב‑`robots.txt` של מאקו, 404, הפניות וכפילויות של אותו מתכון בכמה נתיבים מושמטות (הפירוט ב‑`report.json`).
בנוסף נסרקים יעדי canonical/redirect שהמפה של מאקו לא מכילה. לכל כתובת `lastmod` מה‑CMS של מאקו
ו‑`image:image` עם תמונת ה‑og של המתכון/הכתבה. בלי `priority`/`changefreq`.

| קובץ | תוכן |
|---|---|
| `hubs.xml` | עמודי קטגוריה וערוצים |
| `recipes-baking.xml` | מתכונים: עוגות, קינוחים, לחמים, מאפים |
| `recipes-savory.xml` | מתכונים: בשר, עוף, דגים, פסטה, מרקים, סלטים, ארוחות |
| `recipes-healthy.xml` | מתכונים: בריא, צמחוני, טבעוני, ללא גלוטן |
| `recipes-holidays.xml` | מתכונים: חגים |
| `recipes-tv.xml` | מתכונים: מאסטר שף, בייק אוף, מבשלים עם קשת |
| `recipes-general.xml` | מתכונים: אירוח, מיוחדים, מהירים, משקאות, מגזין |
| `articles-restaurants.xml` | כתבות: מסעדות, אוכל רחוב, גורמה |
| `articles-holidays.xml` | כתבות: חגים |
| `articles-tv.xml` | כתבות: תוכניות הבישול |
| `articles-magazine.xml` | כתבות: מגזין, צרכנות, תזונה, טיפים |

**ריצה:** פעם ביום מהמחשב המקומי דרך Windows Task Scheduler (משימה `mako-food-sitemap`, 05:30, רצה בהזדמנות הראשונה אם המחשב היה כבוי):
`pythonw.exe run_food_sitemap.py` → סריקה מלאה (~25 דקות, כ‑24 אלף כתובות) → commit+push של `docs/mako/food` אם השתנה. הלוג ב‑`logs/food/run.log`,
מטמון הסריקה ב‑`logs/food/crawl.jsonl` (לא בגיט; תוצאות צעירות מ‑20 שעות משמשות שוב, כך שריצה חוזרת באותו יום מהירה).

```bash
FEED_BASE_URL=https://zappler1992.github.io/v1-feed python scripts/build_food_sitemap.py build
```

**הגשה לגוגל:** כמו שאר המפות כאן, הקובץ יושב על `github.io` ומצביע על `mako.co.il`, ולכן ב‑Search Console מגישים אותו
בנכס של `www.mako.co.il` רק אחרי ששני ה‑hosts מאומתים באותו חשבון, או שמוסיפים `Sitemap: https://zappler1992.github.io/v1-feed/mako/food/sitemap-index.xml`
ל‑`robots.txt` של מאקו. כדאי להגיש גם כל תת‑מפה בנפרד כדי לקבל דוח כיסוי לכל נושא.

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
