import time
import re
import json
import os
import csv
import io
import html as html_lib
import calendar
import logging
import numpy as np
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta

import pandas as pd
import feedparser
import requests

try:
    from twitter_source import harvest_twitter_stories
    TWITTER_AVAILABLE = True
except ImportError:
    TWITTER_AVAILABLE = False
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders


# ==========================================
# 1. CONFIGURATION
# ==========================================
EMAIL_SENDER   = "midosapien@gmail.com"

def _load_email_password():
    pw = os.environ.get("EMAIL_APP_PASSWORD", "").strip()
    if pw:
        return pw
    try:
        with open(os.path.expanduser("~/.email_app_password"), "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

EMAIL_PASSWORD = _load_email_password()   # v9.7: no longer hardcoded — see setup note below
EMAIL_RECEIVER = "midosapien@gmail.com"

CHECK_INTERVAL_MINUTES = 60
MAX_STORY_AGE_HOURS    = 24      # v9.6: tightened from 36 — "past 24 hours" per editorial requirement
SEEN_URL_TTL_HOURS     = 24
DIGEST_TOP_TIER        = 50      # v9.9: top tier PER PLATFORM (was combined pre-v9.9)
PLATFORM_MAX_STORIES   = 150     # v9.9: hard cap PER PLATFORM (50 top + 100 more) — RSS and X
                                   # are now independent pools so neither can crowd out the other
STATEMENT_MAX_STORIES  = 50      # v11: video-statements section — focused lane, all "top", no more-split

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Fallback: if the env var isn't set, read the key from ~/.anthropic_key
if not ANTHROPIC_API_KEY:
    try:
        _key_path = os.path.expanduser("~/.anthropic_key")
        with open(_key_path, "r", encoding="utf-8") as _kf:
            ANTHROPIC_API_KEY = _kf.read().strip()
    except Exception:
        ANTHROPIC_API_KEY = ""
CLAUDE_MODEL     = "claude-haiku-4-5-20251001"   # bulk scoring + translation (cheap, parallel, simple)
CLAUDE_MODEL_SMART = "claude-sonnet-4-5"          # v10: dedup + final ranking — the two hardest
                                                   # semantic judgments, run once each, where failures
                                                   # (e.g. the Balogun 10-peat) have concentrated
BATCH_SIZE       = 100
BATCH_DELAY      = 3             # seconds between Claude API batches (rate limit)


# ==========================================
# 2. CLAUDE PROMPT
# ==========================================
CLAUDE_SYSTEM_PROMPT = r"""You score news headlines 1-10 for viral potential for a video-first Arabic news channel that covers the WHOLE WORLD.

You are answering ONE question: "Will millions of people watch and share a 60-second video about this TODAY?"

Topic does not matter. Country does not matter. What matters is HEAT + a VIDEO STORY.

═══ AXIS 1: VIRAL HEAT (this is the score) ═══
A story has heat when it contains one or more of these shapes:

1. A PROVOCATIVE STATEMENT by a named person — mockery, defiance, insult, confession, breaking ranks, saying the unsayable.
   ترمب يكشف كواليس مكالمة مع ماكرون هدده فيها بالرسوم ليرضخ خلال 3 دقائق ← 10
   آنا كاسباريان: لا أطيق إسرائيل وجنودها أناس مقززون ← 10
   روبرت دي نيرو: حان الوقت لقول لا للملوك، لا لدونالد ترمب ← 9
   إيريك كانتونا: إذا قرر رئيس خوض حرب فليكن هو الأول على الجبهة ← 9
   STATEMENTS ARE VIRAL GOLD when the speaker is known and the words are charged. A famous person saying something shocking IS the incident.

   BREAK vs POSITION — the critical distinction for statement scoring:
   A statement is only viral gold if it is a BREAK: the speaker says something surprising
   FOR THEM — against their own interest, betraying their own side, confessing, mocking
   an ally, or defying the position they're expected to hold. An official or spokesperson
   stating their side's EXPECTED position — even forcefully, even about a charged topic —
   is a POSITION, not a break, and caps at 5-6 regardless of how strongly it's worded.
   The test: would this quote surprise someone who already knows this person's job and side?
     سموتريتش: فخور بقيادة البناء في المستوطنات، ولن تقوم دولة فلسطينية ← 5 (his job IS to say this — zero surprise)
     مسؤول إسرائيلي: العملية العسكرية ستستمر حتى تحقيق الأهداف ← 4 (pure routine position statement)
     السفير الأمريكي: نصف مليون مستوطن يعيشون بسلام في الضفة الغربية ← 5 (restating his government's line)
     مقابل ذلك — يائير نتنياهو يهاجم بن غفير: وزير تيك توك حوّل الشرطة لأداة يسارية ← 9 (ally attacking ally — a real break)
     ضابط إسرائيلي سابق: ما فعلناه في غزة جريمة حرب لا يمكن تبريرها ← 9 (insider breaking ranks against his own institution)
   A press-release read aloud by a famous face is still a press release.

2. A CAUGHT-ON-CAMERA MOMENT — someone filmed doing something outrageous, heroic, absurd, or humiliating.
   ترمب يرفع إصبعه الأوسط ويشتم موظفًا في فورد ← 10
   موظف في مطار كراكوف يهاجم إسرائيليين: هذه ليست إسرائيل هذه بولندا ← 9
   شرطي ألماني يهدد متظاهرة باعتقالها بسبب "من النهر إلى البحر" ← 9
   لصوص يحاولون سرقة صراف آلي بجرافة ويفشلون فشلًا ذريعًا فيتحولون إلى مادة للسخرية ← 9
   جندي روسي يتخفى بعباءة كالبطريق والمسيرة الأوكرانية تقتله ← 9

3. AN IRONIC REVERSAL or ABSURD TWIST — reality writing better fiction than fiction.
   طردته أمه لشبهه بأفيخاي أدرعي.. شاب لبناني يستغل الشبه لتكذيب تصريحاته ← 10
   امرأة تركية ترفع دعوى إثبات نسب ضد ترمب لشبهها بإيفانكا ← 9
   دعا والده لضم الضفة.. مقتل المستوطن المتطرف يهودا شيرمان في حادث سير ← 9
   صائغ تركي يقسم "والله بالله تالله" أن الذهب نفد من محله ← 8

4. POWER HUMILIATED / EXPOSED — scandal, leak, hypocrisy of the powerful, secret revealed.
   تسريب صوتي: إبستين ينصح إيهود باراك بالتواصل مع بيتر تيل ← 9
   ميليندا غيتس تلمح: علاقة بيل بإبستين سبب طلاقي ← 9
   وثائق إبستين تكشف تخطيطه للسيطرة على أموال ليبيا المجمدة ← 9

5. AN ORDINARY PERSON IN AN EXTRAORDINARY MOMENT — human drama with a moral, emotional, or bizarre charge.
   شاب سوداني يتسلق برج ضغط عالٍ ويهدد بالانتحار إن لم يتزوج حبيبته عبير ← 9
   طبيب مصري قضى 30 عامًا بالغربة لأبنائه فاستولوا على ثروته وتركوه ← 9
   زوجة صينية تُحاكم بالاعتذار العلني لزوجها الخائن 15 يومًا ← 9
   مشجع لم يقص شعره منذ 16 شهرًا حتى يحقق يونايتد 5 انتصارات متتالية ← 8

6. A CELEBRITY / ATHLETE TAKES A STAND — especially on Palestine, migrants, war, injustice.
   بيلي آيليش تحول فوزها بالغرامي لرسالة ضد ICE: لا أحد غير قانوني على أرض مسروقة ← 9
   غوارديولا: صور الضحايا من فلسطين إلى السودان تدفعني لاستخدام صوتي ← 9
   جاكي شان يبكي بعد مشاهدة مقطع لطفل من غزة ← 9

7. MASS SPECTACLE / DRAMATIC EVENT with footage — explosions, disasters, huge protests, military drama, WHEN there is a specific dramatic visual, scale, or death of someone notable.
   إسرائيليون يوثقون سقوط صواريخ عنقودية غريبة تغطي 8 كيلومترات ← 9
   اعتقالات ورذاذ فلفل في سيدني ضد زيارة هرتسوغ ← 8
   انفجار مخزن ألعاب نارية في ماليزيا يثير ذعر السكان ← 8

8. THE IRRESISTIBLE QUESTION — a mystery or "wait, WHAT?" fact people click to resolve.
   "أجمل امرأة في العالم" ليست بشرًا.. كيف اكتشف المتابعون أنها ذكاء اصطناعي؟ ← 9
   صوت الأذان في منبه هاتفه يجبر طائرة على الهبوط.. ماذا جرى للمسافر؟ ← 9
   سمكة بلا ذكور منذ 100 ألف عام تتحدى قواعد الطبيعة ← 8
   NOTE: explainers and questions ARE viral when the underlying fact is startling. Do not penalize question framing.

═══ AXIS 2: THE VIDEO TEST (gate, not score) ═══
This channel makes VIDEOS, not articles. For a 7+ score there must be a visual path:
- footage exists or almost certainly exists (caught on camera, speech, event, court, stadium, disaster), OR
- a named face + a strong quote (statement stories: the face and the words are the video), OR
- strong visual material (documents, photos, the person, the place).
If the story is purely abstract with no face, no footage, no scene → cap at 6 regardless of importance.
A headline starting with "[VIDEO]" means an actual video clip is CONFIRMED attached at the
source — this automatically satisfies the video test, no inference needed. Treat it as a
positive signal on top of the heat score, not a replacement for heat.

═══ CONFIRMED MOMENT RULE ═══
The moment must have ACTUALLY HAPPENED and be verifiable on camera or on record.
Fan theories, speculation about what someone "may have said/meant", lip-reading debates, unverified claims spreading online → cap at 6.
"Fans claim Ronaldo whispered Bismillah before penalty" ← 6 (speculation, no confirmed moment)
"Ronaldo says Bismillah in post-match interview" ← would be 9 (confirmed, on camera)

═══ TRAVELS-WORLDWIDE RULE ═══
Heat must be legible WITHOUT local context. A story that is huge inside one country's bubble but needs cultural translation → cap at 6.
"Man's high school yearbook predicted his team wins 2026 title" ← 3-4 (American feel-good, needs context, no stakes for a global viewer)
"29-year-old defeats congresswoman who held office her entire lifetime" ← 3-4 (US-domestic politics, no global charge)
The test: would a viewer in Cairo, Casablanca, AND Jakarta feel this story with zero explanation?

═══ ARABIC AUDIENCE AMPLIFIERS (+1 to +2, after heat) ═══
+2 Western/global figure breaks ranks on Palestine, or defends Arabs/Muslims
+2 Israeli official/soldier/settler caught doing something outrageous, or Israeli internal scandal/embarrassment
+2 Anyone powerful humiliated while attacking Arabs/Muslims (backfire stories)
+1 Discrimination incident against Arabs/Muslims in the West with a specific victim or scene
+1 Arab person (athlete, doctor, ordinary hero) does something remarkable abroad
+1 Epstein-file revelations touching politicians or Israel
+1 Arab local drama (Egypt, Maghreb, Gulf, Levant) with bizarre or emotional charge

═══ WHAT IS NOT VIRAL (score 1-5) ═══
- Institutional process: ministry announces, summit concludes, committee recommends, talks continue
- Routine war updates with no specific moment: "strikes continue", "death toll rises", "sides exchange fire"
- Generic protests with no confrontation, arrest, or striking visual
- Policy/economy stories with no named victim or scene (price changes CAN be viral if there is mass anger — score the anger, not the policy)
- Anniversaries, retrospectives, previews, "what to expect"
- Local crime with no twist: a murder is 4; a murder with an absurd motive, famous victim, or ironic detail is 8+
- Sports results without drama: a score is 3; a brawl, a curse, a wild celebration, a defiant gesture is 8+
- Meta-stories about virality itself ("X goes viral", "AI video spreads") unless the underlying moment is independently hot
- STALE SAGA CONTINUATION: an ongoing multi-day story (a funeral spanning several days, "war enters day 40", a
  trial's routine session) with NO new specific fact, incident, or reversal is NOT fresh virality — it is background
  noise on an old event. Score it 4-5 UNLESS this specific article contains a genuinely new development (an arrest,
  a death, a reversal, new footage, a shocking statement). The test: could this headline have been written yesterday
  or the day before with only the date changed? If yes, it is stale continuation, not today's viral story.

═══ ABSOLUTE RULES ═══
1. Score the SPECIFIC STORY, not the topic. "Israel bombs Gaza again" = 3. "Settler killed by friendly fire mistaken for Palestinian" = 10.
2. NEVER cap a story for being a statement, a question, or an explainer. Cap only for lacking heat, lacking a video path, being unconfirmed, or not traveling.
3. Weird-news is ONE lane, not the whole road. A weird story competes on the same heat scale as everything else. "Man arrested with 2,200 smuggled ants" = 8. "Odd local incident, no twist" = 5.
4. The bar for 9-10: one of today's most-shared stories ON EARTH, or guaranteed massive resonance with Arab audiences.
5. The bar for 8: a story a viewer stops scrolling for and probably shares.
6. 7 = solid, would fill a slow day. 6 and below = does not enter the digest.
7. When torn between two scores, ask: "Is there a MOMENT — a face, a quote, a scene — I can see the thumbnail of?" Yes → higher. No → lower.
8. USE THE FULL RANGE WITHIN YOUR SELECTION. Do not default to 8 for everything you include. Among the ≤18 headlines you return: roughly 1-3 should be 9-10, most of the rest 7-8. An 8 must beat every 7 you didn't return, not merely qualify. If you find yourself about to mark 15+ headlines as 8, stop and re-cut — you are not discriminating enough; most of those are 6-7 real-world-newsworthy-but-not-viral stories that belong OUTSIDE your selection entirely, not inside it at a padded 8.
9. MEASURED SIGNALS. Some headlines carry tags: [♥45K] = the story already has that many likes on X (real measured engagement, not a guess), and [📹] = a video clip is confirmed attached. Treat ♥ as strong evidence of actual virality — a story already at ♥40K+ has proven it travels, so weight it up; ♥ under ♥2K is weak traction, weight it down. [📹] satisfies the video test automatically. Headlines with no such tag are from RSS (no engagement data available) — judge those on heat alone as usual, do not penalize them for lacking a number.

═══ CONTENT CATEGORY (separate from the score — tag every headline you select) ═══
- "crime": the story is fundamentally about a criminal act — murder, assault, robbery, kidnapping, sexual violence, fraud, a gang, a court case or verdict — where an individual perpetrator/victim is the actor. Do NOT use "crime" for Israeli/Palestinian conflict violence (settler attacks, IDF actions, strikes, war deaths) or other state/military/political violence — even though people die, that stays "other" because it belongs to the ongoing conflict/political coverage, not the true-crime bucket.
- "bizarre": the story's hook is "wait, WHAT?" — an absurd, surreal, or freak incident: an animal doing something human, an outlandish accident, a strange coincidence, a stunt or scheme gone wrong. The appeal is disbelief, not danger, politics, or tragedy as such.
- "other": everything else — statements, politics, war/conflict, sports, mass-casualty disasters (unless genuinely freakish), celebrity news, science, etc.
Exactly one category per headline. When a story could read as either (a murder with a bizarre method), pick the DOMINANT hook: if the "no way, really?" absurdity is what makes it shareable, it's bizarre; if the criminal act itself is the point, it's crime."""


CLAUDE_USER_PROMPT = """Score each headline 1-10 for viral potential per the system rules.
Heat, video path, confirmed moment, travels worldwide.

HARD CAP: return AT MOST 18 headlines out of every 100 — the true strongest ones only.
This is a ceiling, not a target. A slow-news batch with only 4 real winners should
return 4, not padded up to 18. Do NOT include a headline just to fill the cap.
Every batch of 100 real-world headlines contains far more than 18 that are merely
"newsworthy" — your job is to exclude those and keep only the ones that clear the
actual viral bar (rule 8 in the system prompt). If you are unsure whether a story
belongs in your top 18, it does not — leave it out entirely rather than including it
at a lower score. Omitted headlines are treated as non-winners; do not return an entry
for them at all.

Also classify each headline with "is_video_statement": true ONLY IF the story is a
named public figure (politician, official, commentator, celebrity, pundit) SAYING
something charged — provocative, defiant, mocking, shocking, breaking ranks — where
the heat is in the WORDS SPOKEN, and it plausibly exists as a video clip (a [📹] tag,
or an interview/speech/press-conference/broadcast moment). Set false for events,
incidents, disasters, crimes, or anything where the heat is not someone's spoken words.
When unsure, false.

Also tag each headline with "category": "crime", "bizarre", or "other" per the
CONTENT CATEGORY rules in the system prompt.

Headlines:
{headlines}

Return ONLY JSON array for your selected headlines (max 18 entries):
{{"index": N, "score": N, "is_video_statement": true/false, "category": "crime"/"bizarre"/"other"}}
Start with ["""


# ==========================================
# 3. LOGGING
# ==========================================
def setup_logging():
    fmt = logging.Formatter('%(asctime)s  %(levelname)-8s  %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S')
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)
    fh = RotatingFileHandler('viral_engine.log', maxBytes=10*1024*1024,
                             backupCount=5, encoding='utf-8')
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    for name in ['urllib3', 'requests', 'httpx', 'httpcore']:
        logging.getLogger(name).setLevel(logging.WARNING)

setup_logging()


# ==========================================
# 4. INITIALIZATION
# ==========================================
logging.info("Loading Engine...")

if not ANTHROPIC_API_KEY:
    logging.critical("❌ ANTHROPIC_API_KEY not set. Run: export ANTHROPIC_API_KEY=\"sk-ant-...\"")
    exit()
logging.info(f"✅ API key loaded (ends in ...{ANTHROPIC_API_KEY[-6:]})")

if not EMAIL_PASSWORD:
    logging.warning("⚠️  EMAIL_APP_PASSWORD not set — digest will be generated but NOT emailed. "
                     "Set env var EMAIL_APP_PASSWORD or create ~/.email_app_password")
else:
    logging.info("✅ Email credential loaded.")

try:
    sources_df    = pd.read_csv('sources_rss.csv')
    rss_feed_list = sources_df['RSS_URL'].dropna().tolist()
    logging.info(f"✅ {len(rss_feed_list)} RSS sources loaded.")
except Exception as e:
    logging.critical(f"❌ CSV error: {e}")
    exit()


# ==========================================
# 5. TEXT UTILITIES
# ==========================================
def clean_text(raw):
    text = html_lib.unescape(str(raw))
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[أإآٱ]', 'ا', text)
    text = re.sub(r'[\u064B-\u065F\u0670]', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def detect_language(text):
    arabic = len(re.findall(r'[\u0600-\u06FF]', text))
    latin = len(re.findall(r'[a-zA-Z]', text))
    total = arabic + latin
    if total == 0: return 'ar'
    if arabic / total > 0.3: return 'ar'
    tl = text.lower()
    fr = [' le ', ' la ', ' les ', ' des ', ' du ', ' une ', ' dans ',
          ' sur ', ' avec ', ' pour ', " d'", " l'", " n'", " s'"]
    es = [' el ', ' los ', ' las ', ' del ', ' una ', ' por ', ' con ', ' para ']
    if sum(1 for m in fr if m in f' {tl} ') >= 2: return 'fr'
    if sum(1 for m in es if m in f' {tl} ') >= 2: return 'es'
    return 'en'


# Arab world detection for sorting
_ARAB_MARKERS = {
    'مصر', 'مصري', 'مصرية', 'السعودية', 'سعودي', 'الامارات',
    'الكويت', 'قطر', 'البحرين', 'عمان', 'العراق', 'عراقي',
    'سوريا', 'سوري', 'لبنان', 'لبناني', 'الاردن', 'اردني',
    'فلسطين', 'فلسطيني', 'فلسطينية', 'ليبيا', 'ليبي',
    'تونس', 'تونسي', 'الجزائر', 'جزائري', 'المغرب', 'مغربي',
    'السودان', 'سوداني', 'اليمن', 'يمني',
    'القاهرة', 'الرياض', 'جدة', 'دبي', 'الدوحة', 'بغداد',
    'دمشق', 'بيروت', 'طرابلس', 'غزة', 'القدس', 'الضفة',
    'جنين', 'نابلس', 'الخليل', 'بنغازي', 'الخرطوم',
    'الاقصى', 'حماس', 'حزب الله', 'الحوثي', 'المقاومة',
    'مستوطن', 'مستوطنين', 'الاحتلال', 'اسرائيل', 'نتنياهو',
    'بن غفير', 'الازهر', 'مكة',
    'egypt', 'saudi', 'uae', 'qatar', 'kuwait', 'bahrain',
    'iraq', 'syria', 'lebanon', 'jordan', 'palestine', 'palestinian',
    'libya', 'libyan', 'tunisia', 'algeria', 'morocco', 'sudan', 'yemen',
    'gaza', 'west bank', 'israel', 'israeli', 'hamas', 'hezbollah',
    'tripoli', 'cairo', 'riyadh', 'baghdad', 'damascus', 'beirut',
}

def is_arab_world(story):
    combined = re.sub(r'[أإآٱ]', 'ا',
        f"{story.get('title', '')} {story.get('title_ar', '')}".lower())
    return any(m in combined for m in _ARAB_MARKERS)


# ==========================================
# RAG: VIRAL STORIES RETRIEVAL
# ==========================================
_rag_vectorizer = None
_rag_matrix = None
_rag_titles = None

def load_rag_database():
    """Load the TF-IDF vector database built by build_embeddings.py."""
    global _rag_vectorizer, _rag_matrix, _rag_titles
    try:
        import pickle
        from scipy.sparse import csr_matrix
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError as e:
        logging.warning(f"⚠️  RAG dependencies missing ({e}). Run: pip install scipy scikit-learn")
        return False

    rag_file = 'viral_stories_rag.npz'
    if not os.path.exists(rag_file):
        logging.warning(f"⚠️  RAG database not found ({rag_file}). Run build_embeddings.py first.")
        return False
    try:
        data = np.load(rag_file, allow_pickle=True)
        _rag_titles = list(data['titles'])
        shape = tuple(data['matrix_shape'])
        _rag_matrix = csr_matrix(
            (data['matrix_data'], data['matrix_indices'], data['matrix_indptr']),
            shape=shape
        )
        _rag_vectorizer = pickle.loads(data['vectorizer'].tobytes())
        logging.info(f"✅ RAG database loaded: {len(_rag_titles)} stories.")
        return True
    except Exception as e:
        logging.warning(f"⚠️  RAG load error: {e}")
        return False

def retrieve_similar_stories(query_title, top_k=5):
    """Find the top_k most similar proven viral stories for a given title."""
    if _rag_vectorizer is None or _rag_matrix is None:
        return []
    try:
        from sklearn.metrics.pairwise import cosine_similarity
        norm = re.sub(r'[أإآٱ]', 'ا', query_title)
        norm = re.sub(r'[\u064B-\u065F\u0670]', '', norm)
        qvec = _rag_vectorizer.transform([norm])
        scores = cosine_similarity(qvec, _rag_matrix).flatten()
        top_indices = scores.argsort()[-top_k:][::-1]
        return [_rag_titles[i] for i in top_indices if scores[i] > 0.15]
    except Exception:
        return []

def build_rag_examples(batch_titles, max_examples=5):
    """Build a RAG examples block for a batch of headlines."""
    if _rag_vectorizer is None:
        return ""
    # For the batch, find 5 best examples across all titles
    all_similar = []
    seen = set()
    for title in batch_titles[:20]:  # sample first 20 to avoid slowdown
        similar = retrieve_similar_stories(title, top_k=3)
        for s in similar:
            if s not in seen:
                seen.add(s)
                all_similar.append(s)
        if len(all_similar) >= max_examples * 2:
            break

    if not all_similar:
        return ""

    examples = all_similar[:max_examples]
    lines = "\n".join(f"- {e}" for e in examples)
    return f"\nCHANNEL-PROVEN EXAMPLES (similar stories that each got 1M+ views on this channel):\n{lines}\nUse these as calibration for this batch.\n"


_COUNTRY_FLAGS = [
    # Arab world
    ('🇵🇸', ['فلسطين', 'فلسطيني', 'غزة', 'الضفة', 'القدس', 'جنين', 'نابلس', 'الخليل', 'الاقصى', 'palestine', 'palestinian', 'gaza', 'west bank']),
    ('🇮🇱', ['اسرائيل', 'اسرائيلي', 'مستوطن', 'مستوطنين', 'نتنياهو', 'بن غفير', 'الكنيست', 'تل ابيب', 'israel', 'israeli', 'netanyahu', 'ben gvir', 'idf']),
    ('🇪🇬', ['مصر', 'مصري', 'مصرية', 'القاهرة', 'egypt', 'egyptian', 'cairo']),
    ('🇸🇦', ['السعودية', 'سعودي', 'الرياض', 'جدة', 'مكة', 'saudi', 'riyadh']),
    ('🇱🇧', ['لبنان', 'لبناني', 'بيروت', 'حزب الله', 'lebanon', 'lebanese', 'beirut', 'hezbollah']),
    ('🇸🇾', ['سوريا', 'سوري', 'دمشق', 'syria', 'syrian', 'damascus']),
    ('🇮🇶', ['العراق', 'عراقي', 'بغداد', 'iraq', 'iraqi', 'baghdad']),
    ('🇱🇾', ['ليبيا', 'ليبي', 'طرابلس', 'بنغازي', 'libya', 'libyan', 'tripoli']),
    ('🇹🇳', ['تونس', 'تونسي', 'tunisia', 'tunisian']),
    ('🇩🇿', ['الجزائر', 'جزائري', 'algeria', 'algerian']),
    ('🇲🇦', ['المغرب', 'مغربي', 'الرباط', 'morocco', 'moroccan']),
    ('🇸🇩', ['السودان', 'سوداني', 'الخرطوم', 'sudan', 'sudanese', 'khartoum']),
    ('🇾🇪', ['اليمن', 'يمني', 'الحوثي', 'صنعاء', 'yemen', 'yemeni', 'houthi']),
    ('🇯🇴', ['الاردن', 'اردني', 'عمان', 'jordan', 'jordanian']),
    ('🇰🇼', ['الكويت', 'كويتي', 'kuwait', 'kuwaiti']),
    ('🇶🇦', ['قطر', 'قطري', 'الدوحة', 'qatar', 'qatari']),
    ('🇦🇪', ['الامارات', 'اماراتي', 'دبي', 'ابوظبي', 'uae', 'emirates', 'dubai']),
    ('🇧🇭', ['البحرين', 'bahrain']),
    ('🇴🇲', ['عمان', 'عماني', 'oman', 'omani']),
    ('🇸🇴', ['الصومال', 'صومالي', 'somalia', 'somali']),
    # Non-Arab
    ('🇺🇸', ['اميركي', 'اميركية', 'واشنطن', 'ترمب', 'ترامب', 'البيت الابيض', 'الكونغرس', 'ICE', 'america', 'american', 'trump', 'washington', 'us ', 'u.s.']),
    ('🇬🇧', ['بريطاني', 'بريطانيا', 'لندن', 'britain', 'british', 'uk ', 'london', 'england']),
    ('🇫🇷', ['فرنسا', 'فرنسي', 'باريس', 'ماكرون', 'france', 'french', 'paris', 'macron']),
    ('🇪🇸', ['اسبانيا', 'اسباني', 'سانشيز', 'مدريد', 'برشلونة', 'spain', 'spanish', 'sanchez', 'barcelona']),
    ('🇩🇪', ['المانيا', 'الماني', 'برلين', 'germany', 'german', 'berlin']),
    ('🇮🇷', ['ايران', 'ايراني', 'طهران', 'الحرس الثوري', 'خامنئي', 'iran', 'iranian', 'tehran']),
    ('🇹🇷', ['تركيا', 'تركي', 'اسطنبول', 'اردوغان', 'turkey', 'turkish', 'istanbul', 'erdogan']),
    ('🇷🇺', ['روسيا', 'روسي', 'بوتين', 'موسكو', 'russia', 'russian', 'putin', 'moscow']),
    ('🇨🇳', ['الصين', 'صيني', 'بكين', 'china', 'chinese', 'beijing']),
    ('🇯🇵', ['اليابان', 'ياباني', 'طوكيو', 'japan', 'japanese', 'tokyo']),
    ('🇮🇳', ['الهند', 'هندي', 'مودي', 'india', 'indian', 'modi']),
    ('🇦🇺', ['استراليا', 'استرالي', 'سيدني', 'australia', 'australian']),
    ('🇧🇷', ['البرازيل', 'برازيلي', 'brazil', 'brazilian']),
    ('🇵🇭', ['الفلبين', 'فلبيني', 'philippine', 'filipino', 'manila']),
    ('🇳🇱', ['هولندا', 'هولندي', 'netherlands', 'dutch', 'amsterdam']),
    ('🇮🇹', ['ايطاليا', 'ايطالي', 'روما', 'italy', 'italian', 'rome']),
    ('🇰🇷', ['كوريا', 'كوري', 'korea', 'korean', 'seoul']),
    ('🇵🇰', ['باكستان', 'باكستاني', 'pakistan', 'pakistani']),
    ('🇹🇭', ['تايلاند', 'تايلندي', 'thailand', 'thai']),
    ('🇲🇽', ['المكسيك', 'مكسيكي', 'mexico', 'mexican']),
    ('🇨🇭', ['سويسرا', 'سويسري', 'switzerland', 'swiss']),
    ('🇳🇬', ['نيجيريا', 'نيجيري', 'nigeria', 'nigerian']),
    ('🇺🇦', ['اوكرانيا', 'اوكراني', 'زيلينسكي', 'ukraine', 'ukrainian', 'zelensky']),
    ('🌍', []),  # fallback
]


def get_country_flags(story):
    """Return 1-2 country flag emojis for a story based on title keywords."""
    combined = re.sub(r'[أإآٱ]', 'ا',
        f"{story.get('title', '')} {story.get('title_ar', '')}".lower())
    flags = []
    for flag, keywords in _COUNTRY_FLAGS:
        if not keywords: continue
        if any(k in combined for k in keywords):
            flags.append(flag)
            if len(flags) >= 2:
                break
    return ''.join(flags) if flags else '🌍'


# ==========================================
# 6. HARVEST FILTERS (only age + dedup)
# ==========================================
seen_urls = {}
_title_fps = []
TITLE_DEDUP_THRESHOLD = 0.45
SEEN_URLS_FILE = 'seen_urls.json'   # v10: persist across runs (GitHub Actions = fresh machine each run)

def load_seen_urls():
    """v10: load seen URLs from disk so a story shown in the morning digest
    doesn't reappear in the evening one. On GitHub Actions this file is
    restored via the workflow (cache or committed artifact)."""
    global seen_urls
    try:
        with open(SEEN_URLS_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        seen_urls = {u: datetime.fromisoformat(ts) for u, ts in raw.items()}
        logging.info(f"✅ Loaded {len(seen_urls)} seen URLs from previous runs.")
    except FileNotFoundError:
        seen_urls = {}
        logging.info("ℹ️  No seen_urls.json yet — starting fresh (first run).")
    except Exception as e:
        seen_urls = {}
        logging.warning(f"⚠️  Could not load seen_urls.json ({e}) — starting fresh.")

def save_seen_urls():
    """v10: write seen URLs back to disk after the run."""
    try:
        with open(SEEN_URLS_FILE, 'w', encoding='utf-8') as f:
            json.dump({u: ts.isoformat() for u, ts in seen_urls.items()}, f)
        logging.info(f"💾 Saved {len(seen_urls)} seen URLs for next run.")
    except Exception as e:
        logging.warning(f"⚠️  Could not save seen_urls.json ({e}).")


# ==========================================
# SOURCE CONTRIBUTION SCOREBOARD (v10.1)
# ==========================================
# Evidence-based source pruning: tally, cumulatively across runs, how many
# stories from each source actually reach the final digest (and how many make
# the TOP tier). After a week or two this produces a ranked list — sources
# that never contribute a winner are dead weight; frequent contributors stay.
# This replaces guessing about which of the 291 feeds / 254 accounts to cut.
SOURCE_STATS_FILE = 'source_stats.json'

def _load_source_stats():
    try:
        with open(SOURCE_STATS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {'runs': 0, 'sources': {}}
    except Exception as e:
        logging.warning(f"⚠️  Could not load source_stats.json ({e}) — starting fresh.")
        return {'runs': 0, 'sources': {}}

def update_source_stats(statement_winners, rss_winners, twitter_winners,
                         crime_winners=None, bizarre_winners=None):
    """Tally this run's digest contributions into the cumulative scoreboard."""
    crime_winners = crime_winners or []
    bizarre_winners = bizarre_winners or []
    stats = _load_source_stats()
    stats['runs'] = stats.get('runs', 0) + 1

    def tally(stories):
        for i, s in enumerate(stories):
            src = s.get('source', 'unknown')
            rec = stats['sources'].setdefault(src, {'digest': 0, 'top': 0, 'origin': s.get('origin', '?')})
            rec['digest'] += 1
            if i < DIGEST_TOP_TIER:
                rec['top'] += 1

    tally(statement_winners)
    tally(rss_winners)
    tally(twitter_winners)
    tally(crime_winners)
    tally(bizarre_winners)

    try:
        with open(SOURCE_STATS_FILE, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"⚠️  Could not save source_stats.json ({e}).")

    # Log this run's contributors + the all-time deadweight picture
    run_sources = {}
    for s in statement_winners + rss_winners + twitter_winners + crime_winners + bizarre_winners:
        run_sources[s.get('source', 'unknown')] = run_sources.get(s.get('source', 'unknown'), 0) + 1
    top_this_run = sorted(run_sources.items(), key=lambda x: -x[1])[:8]
    logging.info(f"📊 This run's top contributing sources: {top_this_run}")

    # After a few runs, surface sources that have NEVER contributed a winner
    if stats['runs'] >= 5:
        contributed = set(stats['sources'].keys())
        logging.info(f"📊 Scoreboard: {len(contributed)} sources have contributed ≥1 digest story "
                     f"across {stats['runs']} runs. (See source_stats.json for the full ranked table; "
                     f"sources absent from it are candidates for pruning.)")

def title_fingerprint(title):
    ar = set(re.findall(r'[\u0600-\u06FF]{3,}', title))
    en = set(w.lower() for w in re.findall(r'[a-zA-Z]{3,}', title))
    return ar | en

def is_title_duplicate(title):
    fp = title_fingerprint(title)
    if len(fp) < 3: return False
    for seen in _title_fps:
        if fp & seen:
            union = fp | seen
            if len(fp & seen) / len(union) >= TITLE_DEDUP_THRESHOLD:
                return True
    _title_fps.append(fp)
    return False

def reset_title_dedup():
    _title_fps.clear()

def evict_old_urls():
    cutoff = datetime.now() - timedelta(hours=SEEN_URL_TTL_HOURS)
    stale = [u for u, ts in seen_urls.items() if ts < cutoff]
    for u in stale: del seen_urls[u]
    if stale: logging.info(f"🧹 Evicted {len(stale)} stale URLs.")

from dateutil import parser as dateutil_parser

_date_parse_failures = {'count': 0, 'by_feed': {}}

# v13.2: Le Parisien "Faits divers" reliably publishes fresh content but its
# RSS entries don't carry a field dateutil can parse — every single item was
# being FAIL-CLOSED rejected as "too old" (100/run, the single worst offender
# in the unparseable-dates log line). Per editorial call: this feed is known-
# fresh, so an unparseable date here should NOT block the story — let Claude
# score it on merit instead of silently discarding it. Scoped by URL, not
# feed_name (feed_name comes from the feed's own <title>, less stable than
# the URL we control in sources_rss.csv).
DATE_CHECK_EXEMPT_URLS = [
    'feeds.leparisien.fr/leparisien/rss/faits-divers',
]
def _is_date_exempt(url):
    return any(u in url for u in DATE_CHECK_EXEMPT_URLS)

def parse_entry_date(entry, feed_name=""):
    for field in ['published_parsed', 'updated_parsed', 'created_parsed']:
        parsed = entry.get(field)
        if parsed:
            try: return datetime.fromtimestamp(calendar.timegm(parsed))
            except: pass
    # v9.6: fallback to dateutil for the many feeds (esp. Google News queries)
    # that don't populate feedparser's structured date fields.
    # v10: added dc:date / date / issued — some feeds (e.g. certain French
    # outlets) use Dublin Core or non-standard field names feedparser exposes
    # under these keys, which is why ~100 entries/run were failing silently.
    for field in ['published', 'updated', 'pubDate', 'date', 'dc:date', 'issued', 'created']:
        raw = str(entry.get(field, '')).strip()
        if raw:
            try:
                return dateutil_parser.parse(raw, fuzzy=True).replace(tzinfo=None)
            except Exception:
                pass
    # Genuinely unparseable — count it so bad feeds are visible, and FAIL CLOSED.
    _date_parse_failures['count'] += 1
    _date_parse_failures['by_feed'][feed_name] = _date_parse_failures['by_feed'].get(feed_name, 0) + 1
    return None

def get_publish_date(entry, url=""):
    for field in ['published_parsed', 'updated_parsed', 'created_parsed']:
        parsed = entry.get(field)
        if parsed:
            try: return time.strftime('%Y-%m-%d %H:%M', parsed)
            except: pass
    for field in ['published', 'updated', 'pubDate', 'date', 'dc:date', 'issued', 'created']:
        raw = str(entry.get(field, '')).strip()
        if raw:
            try:
                return dateutil_parser.parse(raw, fuzzy=True).strftime('%Y-%m-%d %H:%M')
            except Exception:
                pass
    if _is_date_exempt(url):
        # No real date to report — stamp "now" rather than leaving it blank,
        # so this doesn't silently sort as the oldest story in the digest
        # (recency tie-breaks and the freshness-aware ranking pass both treat
        # a blank/unparseable pub_date as maximally stale otherwise).
        return datetime.now().strftime('%Y-%m-%d %H:%M')
    return ''

def is_too_old(entry, feed_name="", url=""):
    dt = parse_entry_date(entry, feed_name)
    if dt is None:
        if _is_date_exempt(url):
            # Known-fresh feed, undateable entries — admit it and let Claude
            # judge it on content instead of discarding it on a technicality.
            return False
        return True   # v9.6: FAIL CLOSED — unparseable date means we can't verify
                       # freshness, so treat as too old rather than silently admitting it
    return (datetime.now() - dt).total_seconds() > MAX_STORY_AGE_HOURS * 3600

def is_html_not_rss(content_bytes):
    head = content_bytes[:2000].decode('utf-8', errors='ignore').lower()
    if any(m in head for m in ['<!doctype html', '<html', 'cloudflare', 'challenge-platform',
                                'just a moment', 'access denied']): return True
    if any(m in head for m in ['<rss', '<feed', '<channel', '<?xml', '<item', '<entry']): return False
    return '<html' in head


# ==========================================
# 7. HARVESTER
# ==========================================
def create_session():
    s = requests.Session()
    # v13.3: connect=0 — connection-level failures (including SSL cert errors
    # like "certificate has expired") used to draw from the same `total` retry
    # budget as everything else, so a permanently-dead cert got retried twice
    # with backoff delay before giving up, on every single run, forever (an
    # expired cert doesn't fix itself between attempts — the retry was pure
    # wasted time). connect=0 makes those fail immediately. status_forcelist
    # retries (429/500/502/503 — genuinely transient) and a read-timeout retry
    # are unaffected.
    retry = Retry(total=2, connect=0, read=1, backoff_factor=1,
                  status_forcelist=[429, 500, 502, 503], allowed_methods=["GET"])
    a = HTTPAdapter(max_retries=retry)
    s.mount("http://", a); s.mount("https://", a)
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,ar;q=0.8,fr;q=0.7,es;q=0.6',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none', 'Sec-Fetch-User': '?1',
        'DNT': '1', 'Cache-Control': 'max-age=0',
    })
    return s

def get_domain_headers(url):
    h = {}
    if 'alarabiya.net' in url: h['Referer'] = 'https://www.alarabiya.net'
    if 'reddit.com' in url: h['User-Agent'] = 'ViralEngine/1.0 (RSS reader)'
    if 'dailymail.co.uk' in url or 'mirror.co.uk' in url: h['Referer'] = 'https://www.google.com/'
    if 'liberation.fr' in url or 'leparisien.fr' in url: h['Referer'] = 'https://www.google.fr/'
    if 'elpais.com' in url: h['Referer'] = 'https://www.google.es/'
    return h

http_session = create_session()


def harvest_data():
    stories = []
    blocked = failed = deduped = too_old_count = already_seen = cloudflare = 0
    failed_feeds = []   # v9.6: track WHICH feeds fail, not just how many

    logging.info(f"📡 Harvesting {len(rss_feed_list)} feeds...")

    for raw_url in rss_feed_list:
        url = re.sub(r'\?itid=.*$', '', raw_url.strip())
        try:
            resp = http_session.get(url, headers=get_domain_headers(url), timeout=15)
            if resp.status_code != 200:
                blocked += 1; continue
            if is_html_not_rss(resp.content):
                cloudflare += 1; continue

            feed = feedparser.parse(resp.content)
            feed_name = feed.feed.get('title', url)[:50]

            for entry in feed.entries:
                link = entry.get('link', '').strip()
                if not link: continue
                if link in seen_urls: already_seen += 1; continue
                if is_too_old(entry, feed_name, url): too_old_count += 1; continue

                title = clean_text(entry.get('title', ''))
                if is_title_duplicate(title): deduped += 1; continue

                desc = clean_text(entry.get('description', ''))
                stories.append({
                    'source': feed_name, 'title': title,
                    'text': f"{title} - {desc}",
                    'link': link,
                    'lang': detect_language(title),
                    'pub_date': get_publish_date(entry, url),
                    'origin': 'rss',   # v9.9: explicit origin tag for the RSS/X split digest
                })

        except Exception as e:
            failed += 1
            failed_feeds.append((url, str(e)[:80]))

    logging.info(
        f"📥 {len(stories)} stories | {too_old_count} old | "
        f"{deduped} deduped | {already_seen} seen | "
        f"{cloudflare} cloudflare | {blocked} blocked | {failed} failed."
    )
    if failed_feeds:
        logging.info(f"💀 Dead/failing feeds this run ({len(failed_feeds)}):")
        for url, err in failed_feeds:
            logging.info(f"     {url}  →  {err}")
    if _date_parse_failures['count'] > 0:
        top_offenders = sorted(_date_parse_failures['by_feed'].items(), key=lambda x: -x[1])[:5]
        logging.info(
            f"📅 {_date_parse_failures['count']} entries had unparseable dates "
            f"(now rejected as too-old rather than silently admitted). "
            f"Worst feeds: {top_offenders}"
        )
    return stories


# ==========================================
# 8. CLAUDE RANKING
# ==========================================
def final_ranking_pass(winners, max_per_cluster=2, max_per_family=5):
    """v9.5: Score-compression fix — rank stories head-to-head instead of
    trusting flat per-batch scores.

    v9.9: REPETITION FIX. Asking the model to "avoid stacking duplicates" as
    a free-text instruction was NOT robust enough — a digest still shipped
    with ~10 variants of one story (a red card and its reversal) despite the
    dedup pass having an explicit worked example of exactly that scenario.
    Free-text compliance degrades across large lists; it cannot be trusted
    as the only line of defense against repetition.

    So this pass now asks for a short CLUSTER TAG per story (its underlying
    event, e.g. "balogun_red_card"), and the cap is enforced in PYTHON below
    — mechanically, not by asking nicely. No matter how the model reasons,
    at most `max_per_cluster` stories sharing a cluster tag can survive.

    v12: FAMILY TIER. The cluster cap alone missed a real case: a live
    breaking crisis (a war escalation) generating 10+ genuinely DISTINCT
    quotes/facts over 18 hours — each correctly got its own cluster tag
    (they ARE different facts), so the cluster cap never fired, and one
    story dominated 22% of a digest. Clustering "is this the same incident"
    is the wrong granularity for that case; what's needed is a coarser
    "story family" tag ("iran_us_conflict") capping TOTAL volume from one
    ongoing situation, independent of how many distinct sub-events it has.

    Falls back to the original score/region/recency sort if the call fails
    or the response can't be parsed — never blocks the digest on this.
    """
    if len(winners) < 3:
        return winners  # not enough to meaningfully rank

    def _age_hours(w):
        try:
            dt = datetime.strptime(w.get('pub_date', '')[:16], '%Y-%m-%d %H:%M')
            return round((datetime.now() - dt).total_seconds() / 3600, 1)
        except Exception:
            return 24.0   # unknown age — treat as borderline, not favored

    lines = "\n".join(
        f"{i}. [{w['score']}] [{_age_hours(w)}h old]{_signal_tag(w)} {(w.get('title_ar') or w['title'])[:140]}"
        for i, w in enumerate(winners)
    )

    system = """You are ranking a shortlist of stories that ALL already passed a viral-heat
bar for a video-first Arabic news channel covering the whole world. Every story listed
already qualified — your job is to put them in the TRUE order of viral strength, from
the single most-shareable story today down to the weakest of the qualifiers.

Judge on: raw shareability worldwide, whether there's a real video/visual moment,
whether the moment is confirmed (not speculation), and whether it's legible without
local context. Topic and region do NOT earn extra rank on their own — a strong Egypt
story does not automatically outrank a strong Venezuela story.

FRESHNESS IS PART OF VIRAL STRENGTH, not a tiebreaker. Each headline shows its age in
hours. A story past ~30 hours old should generally rank BELOW an equally-strong fresh
story, because the audience has likely already seen it circulate. Distinguish:
  - a NEW SPECIFIC DEVELOPMENT in an ongoing saga (an arrest, a death, a reversal,
    a new video) — this is fresh and should rank on its own merits
  - GENERIC CONTINUATION coverage of an old saga (funeral proceedings day 5, "war
    continues", reaction pieces with no new fact) — rank this well below fresh
    developments even if the underlying topic is important
An old story should only rank highly if it is dramatically stronger than every fresh
alternative — freshness is a real factor, not an absolute veto.

TWO LEVELS OF TAGGING (mandatory — this is how repetition gets removed downstream):

1. "cluster" — the SPECIFIC incident. 2-4 lowercase English words with underscores,
   e.g. "balogun_red_card", "trump_2to3_week_timeline_claim". Two stories about the
   EXACT same incident — even with different specific facts, quotes, or angles —
   MUST get the identical cluster key. Be generous about merging near-identical
   angles of one incident.

2. "family" — the BROADER ongoing situation this incident belongs to, e.g.
   "iran_us_conflict", "gaza_war", "khamenei_succession". Multiple DIFFERENT
   clusters can and often should share the same family — e.g. "Trump threatens
   strike" and "Iran attacks tankers" are different clusters (different specific
   facts) but the SAME family (one ongoing crisis). Use a family tag whenever a
   story is part of a recognizable ongoing situation with more than one
   sub-development; if a story is fully standalone with no ongoing arc, family
   can equal cluster.

Return ONLY a JSON array of objects, most viral first, including every story exactly
once: [{"index": N, "cluster": "short_key", "family": "broader_key"}, ...].
No commentary. Start with ["""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
            json={
                "model": CLAUDE_MODEL_SMART, "max_tokens": 16000,
                "system": system,
                "messages": [{"role": "user", "content": f"Rank and tag (cluster + family) these {len(winners)} stories:\n\n{lines}"}],
            },
            timeout=90,
        )
        if resp.status_code != 200:
            logging.warning(f"🏆 Ranking pass HTTP {resp.status_code} — falling back to score/region sort.")
            return None
        data = resp.json()
        text = '\n'.join(b['text'] for b in data.get('content', []) if b.get('type') == 'text').strip()
        order = extract_json_array(text)
        if not order:
            logging.warning("🏆 Ranking pass: couldn't parse response — falling back to score/region sort.")
            return None

        seen = set()
        ranked = []   # list of (story, cluster_key, family_key), in model's ranked order
        for entry in order:
            if isinstance(entry, dict):
                idx = entry.get('index')
                cluster = entry.get('cluster', '')
                family = entry.get('family') or cluster   # v12: default family to cluster if unset
            elif isinstance(entry, int):
                idx, cluster, family = entry, None, None   # old plain-index format, no tagging available
            else:
                continue
            if isinstance(idx, int) and 0 <= idx < len(winners) and idx not in seen:
                ranked.append((winners[idx], cluster, family))
                seen.add(idx)
        # Safety net: append anything the model missed, in original order, uncapped
        for i, w in enumerate(winners):
            if i not in seen:
                ranked.append((w, None, None))

        # v9.9 + v12: MECHANICAL two-tier cap enforcement — this is the actual fix,
        # not the prompt above. Walk the ranked list top to bottom:
        #   - cluster cap: same specific incident, max_per_cluster survives (catches
        #     literal repeats, e.g. Balogun's red card reported 10 ways)
        #   - family cap: same broader ongoing situation, max_per_family survives
        #     (catches a fast-breaking crisis generating many DISTINCT but related
        #     facts — the gap the cluster cap alone missed on the Iran/US story)
        # A story with no tag (fallback/unparsed) is never capped by either tier.
        cluster_counts = {}
        family_counts = {}
        final = []
        dropped_cluster = 0
        dropped_family = 0
        for story, cluster, family in ranked:
            if cluster:
                n = cluster_counts.get(cluster, 0)
                if n >= max_per_cluster:
                    dropped_cluster += 1
                    continue
            if family:
                n = family_counts.get(family, 0)
                if n >= max_per_family:
                    dropped_family += 1
                    continue
            if cluster:
                cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
            if family:
                family_counts[family] = family_counts.get(family, 0) + 1
            final.append(story)

        logging.info(f"🏆 Ranking pass: reordered {len(final)} stories by true viral strength "
                     f"(dropped {dropped_cluster} beyond {max_per_cluster}-per-event cap, "
                     f"{dropped_family} beyond {max_per_family}-per-story-family cap).")
        return final

    except Exception as e:
        logging.warning(f"🏆 Ranking pass error: {e} — falling back to score/region sort.")
        return None


def extract_json_array(text):
    if not text: return None
    cleaned = text.replace('```json', '').replace('```', '').strip()
    start = cleaned.find('[')
    if start == -1: return None
    depth = in_str = esc = 0
    for i in range(start, len(cleaned)):
        c = cleaned[i]
        if esc: esc = False; continue
        if in_str:
            if c == '\\': esc = True
            elif c == '"': in_str = False
            continue
        if c == '"': in_str = True
        elif c == '[': depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                try: return json.loads(cleaned[start:i+1])
                except:
                    fixed = re.sub(r',(\s*[\]}])', r'\1', cleaned[start:i+1])
                    try: return json.loads(fixed)
                    except: return None
    return None


def _signal_tag(story):
    """v10: surface measured signals to the scorer instead of throwing them away.
    X stories carry a real like count and confirmed-video flag; the scorer was
    guessing virality from text alone while this ground truth sat unused."""
    parts = []
    eng = story.get('engagement')
    if isinstance(eng, (int, float)) and eng > 0:
        if eng >= 1000:
            parts.append(f"♥{eng/1000:.0f}K")
        else:
            parts.append(f"♥{int(eng)}")
    if story.get('has_video'):
        parts.append("📹")
    return f" [{' '.join(parts)}]" if parts else ""


def claude_ranking(stories):
    if not stories: return []
    logging.info(f"🤖 Claude ranking {len(stories)} stories...")

    indexed = list(enumerate(stories))
    all_scores = {}
    all_flags = {}   # v11: index -> True when scorer classifies as a video statement
    all_categories = {}   # v14: index -> "crime" | "bizarre" | "other"
    rag_hit_batches = 0   # v10: track how often the RAG layer actually fires

    def _score_line(i, s):
        # v10: for RSS stories, append a short description snippet when the
        # headline alone is thin — sharpens judgment on ambiguous titles.
        base = f"{i}. [{s['lang']}]{_signal_tag(s)} {s['title']}"
        if s.get('origin') == 'rss':
            desc = s.get('text', '')
            # 'text' is "title - description"; extract the description tail if it adds info
            if ' - ' in desc:
                tail = desc.split(' - ', 1)[1].strip()
                if tail and tail[:40] not in s.get('title', ''):
                    base += f"  ⟨{tail[:120]}⟩"
        return base

    for batch_start in range(0, len(indexed), BATCH_SIZE):
        batch = indexed[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(indexed) + BATCH_SIZE - 1) // BATCH_SIZE

        headlines = "\n".join(_score_line(i, s) for i, s in batch)
        rag_block = build_rag_examples([s['title'] for _, s in batch])
        if rag_block:
            rag_hit_batches += 1
        prompt = CLAUDE_USER_PROMPT.format(headlines=headlines)
        if rag_block:
            prompt = rag_block + "\n" + prompt

        logging.info(f"  📤 Batch {batch_num}/{total_batches}: {len(batch)} titles")

        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": CLAUDE_MODEL,
                    "max_tokens": 4096,
                    "system": CLAUDE_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=90,
            )

            if resp.status_code != 200:
                logging.error(f"  ❌ API {resp.status_code}: {resp.text[:150]}")
                for idx, _ in batch: all_scores[idx] = 5
                continue

            data = resp.json()
            text = '\n'.join(b['text'] for b in data.get('content', []) if b.get('type') == 'text').strip()
            scores_list = extract_json_array(text)

            if not scores_list:
                logging.error(f"  ❌ No JSON in response")
                for idx, _ in batch: all_scores[idx] = 5
                continue

            for entry in scores_list:
                idx = entry.get('index')
                score = entry.get('score', 5)
                if idx is not None:
                    all_scores[idx] = score
                    # v11: capture video-statement classification (only trust it
                    # for Twitter stories; RSS statements aren't confirmed video)
                    if entry.get('is_video_statement') and 0 <= idx < len(stories):
                        if stories[idx].get('origin') in ('twitter', 'statement_video'):
                            all_flags[idx] = True
                    # v14: capture crime/bizarre/other classification
                    cat = entry.get('category')
                    if cat in ('crime', 'bizarre') and 0 <= idx < len(stories):
                        all_categories[idx] = cat

            top = max((e.get('score', 0) for e in scores_list), default=0)
            logging.info(f"  ✅ Batch {batch_num} done. Top: {top}")

        except Exception as e:
            logging.error(f"  ❌ Error: {e}")
            for idx, _ in batch: all_scores[idx] = 5

        if batch_start + BATCH_SIZE < len(indexed):
            time.sleep(BATCH_DELAY)

    total_batches = (len(indexed) + BATCH_SIZE - 1) // BATCH_SIZE
    logging.info(f"🎯 RAG calibration fired on {rag_hit_batches}/{total_batches} batches "
                 f"({100*rag_hit_batches/max(total_batches,1):.0f}%).")

    for i, story in enumerate(stories):
        story['score'] = all_scores.get(i, 0)
        # v11: mark video statements. The statement-video lane pre-sets this True
        # (guaranteed video); the scorer can also promote a general Twitter story.
        if all_flags.get(i) or story.get('is_video_statement'):
            story['is_video_statement'] = True
        # v14: crime/bizarre classification, defaults to 'other' when unset
        story['category'] = all_categories.get(i, 'other')

    return sorted(stories, key=lambda s: s['score'], reverse=True)


# ==========================================
# 9. EVENT DEDUP
# ==========================================
def dedup_events(stories):
    if len(stories) <= 10: return stories
    logging.info(f"🔗 Dedup: grouping {len(stories)} stories...")
    # v9.1: sort by (translated) title so same-event stories land in the same batch
    stories = sorted(stories, key=lambda s: (s.get('title_ar') or s['title'])[:60])

    DEDUP_BATCH = PLATFORM_MAX_STORIES + 50   # v9.9: tracks the per-platform cap now, same margin logic
    all_kept = []

    for batch_start in range(0, len(stories), DEDUP_BATCH):
        batch = stories[batch_start:batch_start + DEDUP_BATCH]
        lines = "\n".join(
            f"{j}. [{s['lang']}] {(s.get('title_ar') or s['title'])[:100]}"
            for j, s in enumerate(batch)
        )

        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                json={
                    "model": CLAUDE_MODEL_SMART, "max_tokens": 8000,
                    "system": """You remove DUPLICATE headlines — same event reported by different outlets,
AND collapse multi-angle coverage of a single incident into one story.

DUPLICATES (group together — keep only one):
- Exact same event, different outlet: "Ben Gvir storms Al-Aqsa - Al Jazeera" = "Ben Gvir storms Al-Aqsa - BBC"
- Same event in different languages: Arabic + English + French version of identical story
- Same quote rephrased: "سانشيز: مقاطعة يوروفيجن" = "رئيس الوزراء الإسباني يعلن مقاطعة يوروفيجن"
- SAME SINGLE INCIDENT, MULTIPLE FOLLOW-UP ANGLES from the same news cycle (this is the category most
  often missed — watch for it carefully): if several headlines are all reactions to, developments in, or
  commentary on ONE specific incident/decision that just happened, collapse them into ONE, even though the
  specific fact or quote in each differs. Example — all of these are the SAME incident (a red card given to
  one player, and its reversal) and should become ONE story, keeping only the single best headline:
    "Trump calls FIFA to overturn Balogun's red card"
    "FIFA rescinds Balogun's red card"
    "Belgium eliminates USA after Balogun sent off"
    "Why FIFA lifted the red card"
    "MAGA agenda criticized over Balogun decision"
    "Trump thanks Infantino for the reversal"
  Watch also for the SAME entity spelled differently across translations (e.g. "ترامب"/"ترمب" are both
  Trump; "بالوغون"/"بالوجون"/"بالوغن" are all the same player) — do not let spelling variation hide an
  obvious duplicate.

NOT DUPLICATES (keep both — these are SEPARATE stories, different incidents even if same broad topic):
- Different aspects of Eurovision: "Spain boycotts Eurovision" ≠ "Israel booed at Eurovision" ≠ "Eurovision vote manipulation" ≠ "Vienna hosts alternative concert"
- Different Ben Gvir actions ON DIFFERENT DAYS/OCCASIONS: "storms Al-Aqsa" ≠ "announces settlement plans" ≠ "waves flag on Temple Mount"
- Different solidarity actions by different people: "Yamal raises flag" ≠ "Sanchez defends Yamal" ≠ "Galatasaray player raises flag" ≠ "Eiffel Tower flag"
- Different boycott stories in different places: "Spain boycotts" ≠ "academic boycott rises 150%" ≠ "Carrefour boycott week" ≠ "Ireland boycotts"
- Different protests in different cities: "Vienna protest" ≠ "Morocco protest" ≠ "Amman protest"
- Different crimes: each crime is its own event

THE TEST: ask "are these describing reactions to/developments in the SAME single incident that happened once,
within the same news cycle?" If yes → collapse to one. If they are different occurrences/actions/incidents
that merely share a topic or person → keep separate.

Return JSON array of index groups: [[0,5],[1],[2,8],[3],...]""",
                    "messages": [{"role": "user", "content": f"Group these {len(batch)} headlines. Remove only TRUE duplicates. Start with [\n\n{lines}"}],
                },
                timeout=90,
            )

            if resp.status_code != 200:
                logging.warning(f"⚠️ Dedup API {resp.status_code}")
                all_kept.extend(batch)
                continue

            data = resp.json()
            text = '\n'.join(b['text'] for b in data.get('content', []) if b.get('type') == 'text').strip()
            groups = extract_json_array(text)

            if not groups:
                logging.warning("⚠️ Dedup: no JSON")
                all_kept.extend(batch)
                continue

            seen_idx = set()
            for group in groups:
                if not isinstance(group, list): continue
                group = [idx for idx in group if isinstance(idx, int) and 0 <= idx < len(batch) and idx not in seen_idx]
                if not group: continue
                group.sort(key=lambda idx: (0 if batch[idx].get('lang') == 'ar' else 1, -batch[idx].get('score', 0)))
                all_kept.append(batch[group[0]])
                for idx in group: seen_idx.add(idx)

            for i, s in enumerate(batch):
                if i not in seen_idx: all_kept.append(s)

        except Exception as e:
            logging.warning(f"⚠️ Dedup error: {e}")
            all_kept.extend(batch)

        if batch_start + DEDUP_BATCH < len(stories):
            time.sleep(BATCH_DELAY)

    # Cross-batch dedup: simple title-token overlap to catch remaining dupes
    final = []
    final_fps = []
    for s in all_kept:
        _t = f"{s.get('title_ar') or ''} {s['title']}"   # v9.1: compare in Arabic when available
        fp = set(re.findall(r'[\u0600-\u06FF]{3,}', _t))
        fp |= set(w.lower() for w in re.findall(r'[a-zA-Z]{3,}', _t))
        is_dupe = False
        for existing_fp in final_fps:
            if fp and existing_fp:
                overlap = len(fp & existing_fp) / len(fp | existing_fp)
                if overlap >= 0.5:
                    is_dupe = True
                    break
        if not is_dupe:
            final.append(s)
            final_fps.append(fp)

    logging.info(f"🔗 Dedup: {len(stories)} → {len(final)} unique events.")
    return final


# ==========================================
# 10. TRANSLATION
# ==========================================
def translate_titles(stories):
    non_ar = [(i, s) for i, s in enumerate(stories) if s.get('lang') != 'ar']
    if not non_ar: return

    logging.info(f"🌐 Translating {len(non_ar)} titles...")
    TRANS_BATCH = 40          # v9.1: smaller batches — one bad batch loses less
    translated = 0

    def parse_translations(text):
        """Strict JSON first; regex fallback rescues entries from broken JSON."""
        arr = extract_json_array(text)
        if arr and all(isinstance(e, dict) for e in arr): return arr
        out = []
        for m in re.finditer(r'"index"\s*:\s*(\d+)\s*,\s*"ar"\s*:\s*"((?:[^"\\]|\\.)*)"', text or ""):
            try: out.append({"index": int(m.group(1)), "ar": m.group(2).replace('\\"', '"')})
            except Exception: pass
        return out or None

    for batch_start in range(0, len(non_ar), TRANS_BATCH):
        batch = non_ar[batch_start:batch_start + TRANS_BATCH]
        lines = "\n".join(f"{i}. {s['title']}" for i, s in batch)

        for attempt in (1, 2):                     # v9.1: one retry per batch
            try:
                resp = requests.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"},
                    json={
                        "model": CLAUDE_MODEL, "max_tokens": 8000,
                        "system": "Translate each headline to Modern Standard Arabic. Concise, natural, news-like. Transliterate names to Arabic. Escape any double quotes inside translations. Return JSON: {\"index\": N, \"ar\": \"...\"}",
                        "messages": [{"role": "user", "content": f"Translate to Arabic. Start with [\n\n{lines}"}],
                    },
                    timeout=90,
                )
                if resp.status_code != 200:
                    logging.warning(f"🌐 Translation batch HTTP {resp.status_code} (attempt {attempt}): {resp.text[:200]}")
                    time.sleep(5); continue
                data = resp.json()
                text = '\n'.join(b['text'] for b in data.get('content', []) if b.get('type') == 'text').strip()
                translations = parse_translations(text)
                if not translations:
                    logging.warning(f"🌐 Translation parse failed (attempt {attempt}). Response head: {text[:200]}")
                    time.sleep(5); continue
                for entry in translations:
                    idx = entry.get('index'); ar = entry.get('ar', '')
                    if idx is not None and ar and 0 <= idx < len(stories):
                        stories[idx]['title_ar'] = ar
                        translated += 1
                break
            except Exception as e:
                logging.warning(f"🌐 Translation batch error (attempt {attempt}): {e}")
                time.sleep(5)

        if batch_start + TRANS_BATCH < len(non_ar):
            time.sleep(BATCH_DELAY)

    logging.info(f"🌐 Translated {translated}/{len(non_ar)} titles.")


# ==========================================
# 11. DIGEST EMAIL
# ==========================================
def send_digest_email(statement_stories, rss_stories, twitter_stories,
                       crime_stories=None, bizarre_stories=None, featured_stories=None):
    """v9.9: RSS and Twitter are separate pools, scored/deduped/ranked
    independently so one platform can't crowd out the other.
    v11: plus a dedicated Video Statements section at the top.
    v14: plus CRIME and BIZARRE sections, pulled from all three platforms,
    rendered FIRST (before statements) for fast scanning — these two content
    types are usually the most-shared regardless of which platform they came
    from. The RSS section (crime/bizarre already removed from it) is grouped
    by region with a plain divider — no region name printed, per editorial
    call — instead of the flat TOP/MORE list the other sections use.
    v15: plus أبرز القصص (Featured) — the highest-scoring stories across the
    WHOLE digest, rendered above even Crime/Bizarre. Unlike those two, this
    is NOT an extraction — the same story still appears in its normal
    section too, this is a highlight reel on top, not a fourth bucket."""
    crime_stories = crime_stories or []
    bizarre_stories = bizarre_stories or []
    featured_stories = featured_stories or []
    all_stories = (statement_stories + rss_stories + twitter_stories
                   + crime_stories + bizarre_stories)   # for total count only (featured excluded - duplicates)

    msg = MIMEMultipart("mixed")
    msg["Subject"] = (f"🔴 VIRAL DIGEST — 🌟{len(featured_stories)} · 🔪{len(crime_stories)} · 🤯{len(bizarre_stories)} · "
                       f"{len(statement_stories)}🎥 · {len(rss_stories)} RSS · {len(twitter_stories)} X")
    msg["From"] = EMAIL_SENDER
    msg["To"] = EMAIL_RECEIVER
    now = datetime.now().strftime('%B %d, %Y · %H:%M')

    html = f"""\
    <html><body style="font-family:-apple-system,Arial,sans-serif;line-height:1.6;max-width:700px;margin:0 auto;padding:20px;color:#222;">
    <h2 style="margin-bottom:4px;">🔴 VIRAL DIGEST</h2>
    <p style="color:#999;font-size:13px;margin-top:0;">{now} · {len(featured_stories)} featured · {len(crime_stories)} crime · {len(bizarre_stories)} bizarre · {len(statement_stories)} video statements · {len(rss_stories)} RSS · {len(twitter_stories)} X</p>
    <hr style="border:none;border-top:1px solid #ddd;margin:16px 0;">"""

    def render(s):
        title = s.get('title', '')
        title_ar = s.get('title_ar', '')
        source = s.get('source', '')
        link = s.get('link', '#')
        pub = s.get('pub_date', '')
        lang = s.get('lang', 'ar')
        score = s.get('score', 0)
        date_str = f" · {pub}" if pub else ""
        flags = get_country_flags(s)

        b = '<div style="margin-bottom:16px;padding-bottom:16px;border-bottom:1px solid #f0f0f0;">'
        if lang == 'ar':
            b += f'<a href="{link}" dir="rtl" style="font-size:15px;font-weight:600;color:#1a1a1a;text-decoration:none;display:block;text-align:right;" target="_blank">{flags} {title}</a>'
        elif title_ar:
            b += f'<a href="{link}" dir="rtl" style="font-size:15px;font-weight:600;color:#1a1a1a;text-decoration:none;display:block;text-align:right;" target="_blank">{flags} {title_ar}</a>'
            b += f'<div style="color:#888;font-size:12px;margin-top:3px;font-style:italic;">{title}</div>'
        else:
            b += f'<a href="{link}" style="font-size:15px;font-weight:600;color:#1a1a1a;text-decoration:none;" target="_blank">{flags} {title}</a>'
        b += f'<div style="color:#aaa;font-size:11px;margin-top:4px;">{source}{date_str}</div></div>'
        return b

    REGION_DIVIDER = '<hr style="border:none;border-top:1px dashed #ccc;margin:18px 0;">'

    def render_items(stories, group_by_region=False):
        out = ""
        prev_region = None
        for s in stories:
            if group_by_region:
                region = is_arab_world(s)
                if prev_region is not None and region != prev_region:
                    out += REGION_DIVIDER
                prev_region = region
            out += render(s)
        return out

    def render_platform_section(platform_label, stories, icon, group_by_region=False):
        section = ""
        if not stories:
            return section
        top_tier = stories[:DIGEST_TOP_TIER]
        rest = stories[DIGEST_TOP_TIER:]
        section += f'<div style="margin:28px 0 8px 0;"><strong style="font-size:16px;color:#111;">{icon} {platform_label}</strong> <span style="color:#999;font-size:12px;">({len(stories)} stories)</span><hr style="border:none;border-top:2px solid #333;margin-top:6px;"></div>'
        section += f'<div style="margin-bottom:8px;"><strong style="font-size:14px;color:#c0392b;text-transform:uppercase;letter-spacing:0.1em;">🔥 TOP {len(top_tier)}</strong><hr style="border:none;border-top:2px solid #e74c3c;margin-top:6px;"></div>'
        section += render_items(top_tier, group_by_region)
        if rest:
            section += f'<div style="margin:24px 0 8px 0;"><strong style="font-size:14px;color:#666;text-transform:uppercase;letter-spacing:0.1em;">📰 {len(rest)} MORE</strong><hr style="border:none;border-top:1px solid #ddd;margin-top:6px;"></div>'
            section += render_items(rest, group_by_region)
        return section

    # v15: أبرز القصص (Featured) goes FIRST — the best of the whole digest,
    # for immediate scanning. v14: Crime and Bizarre next — pulled from all
    # platforms. Not region-grouped (small, mixed-origin lists; a divider
    # would fragment them more than it would help).
    html += render_platform_section("أبرز القصص · FEATURED", featured_stories, "🌟")
    html += render_platform_section("CRIME", crime_stories, "🔪")
    html += render_platform_section("BIZARRE", bizarre_stories, "🤯")

    html += render_platform_section("🎥 VIDEO STATEMENTS", statement_stories, "🎥")
    html += render_platform_section("RSS / NEWS SOURCES", rss_stories, "📡", group_by_region=True)
    html += render_platform_section("X / TWITTER", twitter_stories, "🐦")

    html += "</body></html>"
    hp = MIMEMultipart("alternative")
    hp.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(hp)

    # CSV — one file, all sections, with a Platform column plus per-section Tier
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['Platform', 'Tier', 'Region', 'Flags', 'Score', 'Language', 'Title', 'Title_AR', 'Source', 'Date', 'Link'])
    for platform_label, stories in [('FEATURED', featured_stories), ('CRIME', crime_stories), ('BIZARRE', bizarre_stories),
                                     ('STATEMENT', statement_stories), ('RSS', rss_stories),
                                     ('X', twitter_stories)]:
        for i, s in enumerate(stories):
            lang = s.get('lang', '')
            w.writerow([
                platform_label,
                'TOP' if i < DIGEST_TOP_TIER else 'MORE',
                'ARAB' if is_arab_world(s) else 'GLOBAL',
                get_country_flags(s),
                s.get('score', 0), lang, s.get('title', ''),
                s.get('title_ar', '') if lang != 'ar' else s.get('title', ''),
                s.get('source', ''), s.get('pub_date', ''), s.get('link', ''),
            ])
    att = MIMEBase('text', 'csv')
    att.set_payload(buf.getvalue().encode('utf-8-sig'))
    encoders.encode_base64(att)
    digest_filename = f"viral_digest_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    att.add_header('Content-Disposition', 'attachment', filename=digest_filename)
    msg.attach(att)

    # v14.1: ALSO write the CSV to disk, not just attach it to the email.
    # run.yml's "Publish latest digest" step looks for viral_digest_*.csv in
    # the working directory to copy into latest_digest.csv and push — the
    # email attachment lives only in the SMTP payload and was never visible
    # to that step, so the mobile app had nothing to fetch even on a fully
    # successful run (confirmed: engine ran clean, email sent, but "ls -t
    # viral_digest_*.csv" in the workflow found nothing).
    try:
        with open(digest_filename, 'w', encoding='utf-8-sig', newline='') as f:
            f.write(buf.getvalue())
        logging.info(f"💾 Saved {digest_filename} for the mobile app / run artifact.")
    except Exception as e:
        logging.warning(f"⚠️  Could not write {digest_filename} to disk: {e}")

    ctx = ssl._create_unverified_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        logging.info(f"📩 Digest sent (🌟{len(featured_stories)} · 🔪{len(crime_stories)} · 🤯{len(bizarre_stories)} · "
                     f"🎥{len(statement_stories)} · RSS {len(rss_stories)} · X {len(twitter_stories)} + CSV).")
    except Exception as e:
        logging.error(f"❌ Email failed: {e}")


# ==========================================
# 12. MAIN
# ==========================================
def run_engine():
    logging.info(f"🚀 Viral Engine v12. {len(rss_feed_list)} sources.")
    load_rag_database()

    logging.info("=" * 60)
    logging.info(f"🕒 Manual run at {time.strftime('%X')}")
    t0 = time.time()

    load_seen_urls()      # v10: restore cross-run dedup memory
    evict_old_urls()
    reset_title_dedup()

    # LAYER 1: HARVEST RSS
    stories = harvest_data()

    # LAYER 1b: HARVEST X/TWITTER (via twitterapi.io — manual, run when triggered)
    if TWITTER_AVAILABLE:
        try:
            twitter_stories = harvest_twitter_stories()
            existing_links = {s['link'] for s in stories}
            new_from_twitter = [s for s in twitter_stories if s['link'] not in existing_links]
            if new_from_twitter:
                stories.extend(new_from_twitter)
                logging.info(f"🐦 Added {len(new_from_twitter)} unique stories from X.")
        except Exception as e:
            logging.warning(f"🐦 X harvest error (continuing without it): {e}")
    else:
        logging.info("🐦 twitter_source.py not found — skipping X harvest.")

    # CLAUDE RANKING (all stories, RAG-calibrated)
    ranked = claude_ranking(stories)

    # RECENCY BOOST — fresh stories get +1 so they rise above stale ones
    from datetime import datetime as _dt
    def parse_pub(s):
        try: return _dt.strptime(s.get('pub_date', '')[:16], '%Y-%m-%d %H:%M')
        except: return _dt(2000, 1, 1)

    # v9: recency score boost REMOVED — it was laundering 7-scored stories
    # into the digest. Freshness is already enforced by MAX_STORY_AGE_HOURS
    # at harvest and by recency in the sort order below.

    # WINNERS (score >= 8)
    winners = [s for s in ranked if s['score'] >= 8]

    for w in winners:
        seen_urls[w['link']] = datetime.now()

    # v13: GLOBAL family/cluster cap — moved BEFORE the platform split.
    # Previously final_ranking_pass() ran separately inside each platform's
    # pool, so a single saga (e.g. a war escalation) could hit its 5-story
    # family cap in the RSS lane, ANOTHER 5 in the X lane, and ANOTHER 5 in
    # the statement lane — 15 stories from one story, 28% of a digest, while
    # the cap "worked" in the sense that it fired in every lane. The cap has
    # to see all origins at once to mean anything. So: rank + cluster-tag +
    # family-cap the WHOLE winners pool in one pass, THEN split by platform.
    GLOBAL_PRECAP = 300   # bound the ranking call's size regardless of how
                           # many winners a very hot sweep produces. v13.1: was
                           # 500 with max_tokens=6000 — a real sweep (~250
                           # winners) produces a JSON response too long for that
                           # ceiling, gets truncated mid-object, fails to parse,
                           # and silently falls back to plain score/region sort
                           # — meaning the global family cap never actually ran.
                           # 300 items × ~25 tokens/object ≈ 7,500 output tokens,
                           # safely inside the now-16,000 max_tokens ceiling.
    winners.sort(key=lambda s: -s['score'])
    winners_for_ranking = winners[:GLOBAL_PRECAP]

    globally_ranked = final_ranking_pass(winners_for_ranking)
    if globally_ranked:
        winners_for_ranking = globally_ranked
    else:
        winners_for_ranking.sort(key=lambda s: (
            0 if is_arab_world(s) else 1,
            -s['score'],
            -parse_pub(s).timestamp(),
        ))

    # v9.9: SPLIT BY PLATFORM. Twitter was crowding out RSS in the combined
    # digest — X naturally produces more raw volume per sweep, so a single
    # merged top-N let it dominate regardless of quality. Each platform still
    # gets its own translate + phrasing-level dedup + own cap — but the
    # cluster/family volume cap above is now GLOBAL and already applied, so
    # no single saga can dominate by spreading itself across lanes.
    def process_platform(stories, label, max_stories=PLATFORM_MAX_STORIES):
        if not stories:
            logging.info(f"📉 No {label} winners this round.")
            return []
        stories = stories[:max_stories]
        stories.sort(key=lambda s: (
            0 if is_arab_world(s) else 1,
            -s['score'],
            -parse_pub(s).timestamp(),
        ))
        translate_titles(stories)
        stories = dedup_events(stories)
        # v14: RE-SORT after dedup. dedup_events() returns one representative
        # per event cluster in cluster order, NOT input order — so the
        # Arab-first sort above was being scrambled before it reached the
        # digest (measured: 32 region flips across 74 RSS stories). The email's
        # region divider and the app's region grouping both depend on this list
        # actually being region-contiguous, so sort again once dedup is done.
        stories.sort(key=lambda s: (
            0 if is_arab_world(s) else 1,
            -s['score'],
            -parse_pub(s).timestamp(),
        ))
        stories = stories[:max_stories]
        logging.info(f"📦 {label}: {len(stories)} stories ready for digest")
        return stories

    # v11: THREE sections. Video statements are skimmed out of the Twitter pool
    # first (a qualifying tweet appears ONLY in the statements section, not the
    # general X section), then RSS and the remaining X stories each get their own.
    # v13: now splitting the GLOBALLY ranked/capped list, not raw `winners`.
    statement_pool = [s for s in winners_for_ranking
                      if s.get('origin') == 'statement_video' or s.get('is_video_statement')]
    rss_pool       = [s for s in winners_for_ranking if s.get('origin') == 'rss']
    twitter_pool   = [s for s in winners_for_ranking
                      if s.get('origin') == 'twitter' and not s.get('is_video_statement')]

    statement_winners = process_platform(statement_pool, "🎥 Video Statements", STATEMENT_MAX_STORIES)
    rss_winners       = process_platform(rss_pool, "RSS")
    twitter_winners   = process_platform(twitter_pool, "X/Twitter")

    # v14: CRIME / BIZARRE — pulled out of all three platform pools into their
    # own sections at the very top of the digest (per editorial call: these
    # two content types are usually the most-shared regardless of platform,
    # so they should be scannable first rather than buried inside RSS/X).
    def _extract_category(pool, category):
        keep, extracted = [], []
        for s in pool:
            (extracted if s.get('category') == category else keep).append(s)
        return keep, extracted

    statement_winners, stmt_crime   = _extract_category(statement_winners, 'crime')
    statement_winners, stmt_bizarre = _extract_category(statement_winners, 'bizarre')
    rss_winners, rss_crime          = _extract_category(rss_winners, 'crime')
    rss_winners, rss_bizarre        = _extract_category(rss_winners, 'bizarre')
    twitter_winners, tw_crime       = _extract_category(twitter_winners, 'crime')
    twitter_winners, tw_bizarre     = _extract_category(twitter_winners, 'bizarre')

    def _score_recency_sort(stories):
        stories.sort(key=lambda s: (-s['score'], -parse_pub(s).timestamp()))
        return stories

    crime_winners   = _score_recency_sort(stmt_crime + rss_crime + tw_crime)
    bizarre_winners = _score_recency_sort(stmt_bizarre + rss_bizarre + tw_bizarre)
    logging.info(f"📦 🔪 Crime: {len(crime_winners)} stories ready for digest "
                 f"(🎥{len(stmt_crime)} · RSS {len(rss_crime)} · X {len(tw_crime)})")
    logging.info(f"📦 🤯 Bizarre: {len(bizarre_winners)} stories ready for digest "
                 f"(🎥{len(stmt_bizarre)} · RSS {len(rss_bizarre)} · X {len(tw_bizarre)})")

    # v15: أبرز القصص (Featured) - a highlight reel of the single best stories
    # across the ENTIRE digest, listed above everything else. Drawn from all
    # five sections combined (crime, bizarre, statements, RSS, X) by score
    # then recency. Deliberately NOT removed from their original section -
    # this is a "read these first" shortcut, not a fourth extraction lane;
    # someone browsing the RSS section shouldn't find its best story missing.
    FEATURED_COUNT = 20
    featured_pool = crime_winners + bizarre_winners + statement_winners + rss_winners + twitter_winners
    featured_winners = sorted(featured_pool, key=lambda s: (-s['score'], -parse_pub(s).timestamp()))[:FEATURED_COUNT]
    logging.info(f"📦 🌟 Featured: {len(featured_winners)} stories (drawn from all sections, "
                 f"still present in their own section too)")

    if statement_winners or rss_winners or twitter_winners or crime_winners or bizarre_winners:
        send_digest_email(statement_winners, rss_winners, twitter_winners,
                           crime_winners, bizarre_winners, featured_winners)
        update_source_stats(statement_winners, rss_winners, twitter_winners,
                             crime_winners, bizarre_winners)   # v10.1: evidence-based pruning data
    else:
        logging.info("📉 No viral stories this round.")

    save_seen_urls()      # v10: persist dedup memory for the next run
    logging.info(f"✅ Done in {time.time()-t0:.0f}s.")



if __name__ == "__main__":
    run_engine()
