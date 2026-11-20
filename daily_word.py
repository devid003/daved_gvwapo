"""Daily word: a short motivation, a Bible verse and a prayer, posted at 6:00 AM, 3:00 PM and 10:00 PM.

The owner picks the channel on the dashboard (Daily word page). Verses are fetched word for word from
bible-api.com (World English Bible and/or Ang Dating Biblia, both public domain) so the text is never
typed from memory. If the Bible site can't be reached, the post still goes out with the verse reference.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import random
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord.ext import tasks

import db
import discord_bot
from discord_bot import bot

log = logging.getLogger("daily_word")
run = asyncio.to_thread
TZ = ZoneInfo(os.getenv("DISPLAY_TZ", "Asia/Manila"))
BIBLE_API = os.getenv("BIBLE_API_URL", "https://bible-api.com").rstrip("/")
GRACE = dt.timedelta(minutes=30)      # a post missed by a restart still goes out if the bot is back within this

SLOTS = {
    "morning": {"at": dt.time(6, 0), "label": "6:00 AM", "title": "Magandang umaga ☀️", "color": 0xF2B84B},
    "afternoon": {"at": dt.time(15, 0), "label": "3:00 PM", "title": "Laban lang ngayong hapon \U0001F4AA", "color": 0x3987E5},
    "night": {"at": dt.time(22, 0), "label": "10:00 PM", "title": "Pahinga na, pero magpasalamat muna \U0001F319", "color": 0x6250D6},
}

LINES = {
    "morning": [
        "Bagong araw, bagong pagkakataon. Hindi mo kailangang maging perpekto ngayon, kailangan mo lang magsimula.",
        "Bago mo hawakan ang phone para mag-scroll, hawakan mo muna ang plano mo para sa araw na ito.",
        "Ang disiplina ay pangakong tinutupad mo sa sarili mo kahit walang nakakakita.",
        "Hindi ka gumising ngayon nang walang dahilan. May gagawin pa ang Diyos sa buhay mo.",
        "Isang maliit na hakbang ngayong umaga ay mas malaki kaysa sa isang perpektong plano na hindi sinimulan.",
        "Huwag mong hintayin ang motivation. Kumilos ka, at susunod ang lakas ng loob.",
        "Ang umaga ay para sa mga taong may pangarap. Bumangon ka at harapin mo ito.",
        "Kahapon ay tapos na. Ang mahalaga ay kung ano ang gagawin mo sa araw na ibinigay sa iyo ngayon.",
        "Ipagkatiwala mo sa Diyos ang resulta. Ang trabaho mo ay ibigay ang iyong buong makakaya.",
        "Hindi mo kailangang makita ang buong hagdan. Akyatin mo lang ang unang baitang.",
        "Ang taong consistent ay laging mananalo sa taong magaling lang paminsan-minsan.",
        "Simulan mo ang araw sa pasasalamat. Mahirap matalo ang pusong marunong magpasalamat.",
        "May dahilan kung bakit hindi ka pa sumusuko. Ituloy mo.",
        "Ang pangarap mo ay hindi matutupad ng pagtulog. Bangon na, champion.",
        "Hindi ka nag-iisa sa laban na ito. Kasama mo ang Diyos sa bawat hakbang.",
        "Ngayon ang araw para patunayan sa sarili mo na kaya mo.",
    ],
    "afternoon": [
        "Kalahati na ng araw. Hindi pa huli para tapusin ang sinimulan mo kaninang umaga.",
        "Pagod ka na? Normal lang iyan. Huminga ka nang malalim, uminom ng tubig, at ituloy mo.",
        "Ang tagumpay ay hindi nangyayari sa isang araw, pero nangyayari ito araw-araw.",
        "Kung mabagal ang progreso, progreso pa rin iyan. Huwag kang titigil.",
        "Huwag mong ikumpara ang simula mo sa gitna ng iba. Iba-iba ang oras ng bawat isa.",
        "Ang hirap ngayon ay puhunan para sa gaan ng bukas.",
        "Isa pang upload, isa pang subok, isa pang hakbang. Doon nabubuo ang tagumpay.",
        "Kapag gusto mo nang sumuko, alalahanin mo kung bakit ka nagsimula.",
        "Hindi sukatan ng halaga mo ang views o likes ngayong araw. Mahalaga ka sa Diyos.",
        "Focus. Isang gawain lang muna, tapusin mo nang maayos, saka lumipat sa susunod.",
        "Ang lakas na kailangan mo ngayong hapon ay hindi galing sa kape. Galing ito sa Diyos.",
        "Huwag mong sayangin ang natitirang oras sa pag-aalala. Gamitin mo sa paggawa.",
        "Hindi ka napag-iiwanan. Nasa sarili mong takbo ka.",
        "Ang bawat pagkakamali ngayon ay leksyon, hindi katapusan.",
        "Tuloy lang. Ang mga taong nagtatagumpay ay mga taong hindi tumigil.",
        "Magpahinga kung kailangan, pero huwag kang susuko.",
    ],
    "night": [
        "Tapos na ang araw. Anuman ang nangyari, salamat at nakaraos ka.",
        "Hindi mo natapos lahat? Ayos lang. Bukas ay may bagong biyaya.",
        "Bago ka matulog, bilangin mo ang mga biyaya, hindi ang mga problema.",
        "Ipagpasa-Diyos mo na ang mga alalahanin mo. Hindi mo kailangang buhatin lahat.",
        "Proud ako sa iyo. Lumaban ka ngayong araw kahit mahirap.",
        "Ang pahinga ay hindi pagsuko. Bahagi ito ng paghahanda para sa bukas.",
        "Tanungin mo ang sarili mo: ano ang natutunan ko ngayong araw? Doon ka lalago.",
        "Patawarin mo ang sarili mo sa mga pagkakamali ngayon. Pinapatawad ka rin ng Diyos.",
        "Hindi nasusukat ang araw sa kung gaano ka kaabala, kundi sa kung gaano ka naging tapat.",
        "Matulog kang may pag-asa. Hindi ka pinababayaan ng Diyos.",
        "Bukas ay isa na namang pagkakataon na mas maging mabuting bersyon ng sarili mo.",
        "Ilagay mo sa kamay ng Diyos ang mga pangarap mo bago ka pumikit.",
        "Salamat sa lakas na ibinigay sa iyo ngayong araw. Hindi iyon maliit na bagay.",
        "Ang kapayapaan ay hindi kawalan ng problema. Ito ay pagtitiwala sa Diyos sa gitna nito.",
        "Huwag mong dalhin sa pagtulog ang galit at bigat ng loob. Bitawan mo na.",
        "Pahinga ka na. Ang Diyos na nagbantay sa iyo ngayon ay magbabantay din ngayong gabi.",
    ],
}

PRAYERS = {
    "morning": [
        "Panginoon, salamat po sa bagong araw. Gabayan N'yo po ang bawat hakbang ko. Amen.",
        "Lord, ibigay N'yo po sa akin ang lakas at karunungan para sa araw na ito. Amen.",
        "Ama, ingatan N'yo po ang pamilya ko at pagpalain ang gawa ng aking mga kamay. Amen.",
        "Panginoon, gamitin N'yo po ako ngayong araw para maging pagpapala sa iba. Amen.",
        "Lord, ituro N'yo po sa akin ang tamang daan at bigyan ako ng tapang. Amen.",
        "Ama, inaalay ko po sa Inyo ang lahat ng plano ko ngayong araw. Amen.",
    ],
    "afternoon": [
        "Panginoon, bigyan N'yo po ako ng panibagong lakas para matapos ko ang araw na ito. Amen.",
        "Lord, ilayo N'yo po ako sa pagod na nauuwi sa pagsuko. Kayo po ang lakas ko. Amen.",
        "Ama, bigyan N'yo po ako ng pasensya at linaw ng isip ngayong hapon. Amen.",
        "Panginoon, salamat po sa mga nagawa ko na. Tulungan N'yo po ako sa natitira. Amen.",
        "Lord, ipaalala N'yo po sa akin na hindi ako nag-iisa sa laban na ito. Amen.",
        "Ama, pagpalain N'yo po ang trabaho at negosyo ng bawat isa rito. Amen.",
    ],
    "night": [
        "Panginoon, salamat po sa araw na ito. Patawarin N'yo po ako sa aking mga pagkukulang. Amen.",
        "Lord, ipinagkakatiwala ko po sa Inyo ang aking pamilya at ang aking bukas. Amen.",
        "Ama, bigyan N'yo po ako ng mahimbing na tulog at kapayapaan ng isip. Amen.",
        "Panginoon, salamat po sa lahat ng biyaya, maliit man o malaki. Amen.",
        "Lord, Kayo na po ang bahala sa mga hindi ko kayang ayusin. Amen.",
        "Ama, ingatan N'yo po kami ngayong gabi at gisingin kaming may bagong pag-asa. Amen.",
    ],
}

# References only: the words are fetched from bible-api.com when the post goes out.
VERSES = {
    "morning": ["Lamentations 3:22-23", "Psalm 118:24", "Proverbs 3:5-6", "Jeremiah 29:11", "Isaiah 40:31",
                "Joshua 1:9", "Philippians 4:13", "Proverbs 16:3", "Psalm 5:3", "Matthew 6:33", "Colossians 3:23",
                "Psalm 143:8", "Isaiah 41:10", "2 Timothy 1:7", "Psalm 90:14", "Deuteronomy 31:6", "Psalm 27:1",
                "Romans 12:2", "1 Corinthians 16:13", "Micah 6:8"],
    "afternoon": ["Galatians 6:9", "Isaiah 40:29", "2 Corinthians 12:9", "Matthew 11:28", "Philippians 4:6-7",
                  "1 Corinthians 15:58", "Psalm 46:1", "Romans 8:28", "Hebrews 12:1", "James 1:2-3",
                  "Romans 8:31", "Psalm 121:1-2", "John 16:33", "Habakkuk 3:19", "Psalm 31:24", "Mark 9:23",
                  "Luke 1:37", "Romans 5:3-4", "Hebrews 10:36", "Psalm 37:5"],
    "night": ["Psalm 4:8", "Matthew 11:29", "1 Peter 5:7", "Psalm 121:7-8", "John 14:27", "Psalm 55:22",
              "Isaiah 26:3", "Psalm 34:18", "Zephaniah 3:17", "Romans 15:13", "Psalm 91:1-2", "Nahum 1:7",
              "Psalm 23:1-3", "Hebrews 13:5", "1 John 1:9", "Psalm 139:14", "Romans 5:8", "John 3:16",
              "Psalm 30:5", "Numbers 6:24-26"],
}

TRANSLATIONS = {"en": [("web", "WEB")], "tl": [("tagalog", "ADB")], "both": [("web", "WEB"), ("tagalog", "ADB")]}
STATE = {"last_error": ""}


# ---------- settings ----------
def channel_id(gid: int) -> int:
    v = db.get_setting(f"dw_ch:{gid}", "") or ""
    return int(v) if v.isdigit() else 0


def is_on(gid: int) -> bool:
    return db.get_setting(f"dw_on:{gid}", "0") == "1"


def language() -> str:
    v = db.get_setting("dw_lang", "both") or "both"
    return v if v in TRANSLATIONS else "both"


def history(limit: int = 12) -> list:
    try:
        return json.loads(db.get_setting("dw_log", "[]") or "[]")[:limit]
    except ValueError:
        return []


def _log_sent(entry: dict) -> None:
    db.set_setting("dw_log", json.dumps([entry] + history(30))[:20000])


def _pick(kind: str, items: list) -> int:
    """A random item that wasn't used recently (cycles through the whole list before repeating)."""
    key = f"dw_used:{kind}"
    try:
        used = [i for i in json.loads(db.get_setting(key, "[]") or "[]") if isinstance(i, int) and 0 <= i < len(items)]
    except ValueError:
        used = []
    free = [i for i in range(len(items)) if i not in used] or list(range(len(items)))
    if len(free) == len(items):
        used = used[-1:]                    # starting over: just don't repeat the very last one
        free = [i for i in free if i not in used] or free
    i = random.choice(free)
    db.set_setting(key, json.dumps((used + [i])[-len(items):]))
    return i


# ---------- the message ----------
async def fetch_verse(ref: str, translation: str) -> tuple | None:
    """(text, reference) word for word from bible-api.com, or None if it can't be reached."""
    url = f"{BIBLE_API}/{quote(ref.lower())}?translation={translation}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.json(content_type=None)
    except Exception:
        return None
    text = " ".join(str(data.get("text") or "").split())
    if not text:
        return None
    return text[:900], str(data.get("reference") or ref)


async def compose(slot: str, lang: str | None = None) -> discord.Embed:
    s = SLOTS[slot]
    lang = lang or await run(language)
    line = LINES[slot][await run(_pick, f"line:{slot}", LINES[slot])]
    prayer = PRAYERS[slot][await run(_pick, f"prayer:{slot}", PRAYERS[slot])]
    ref = VERSES[slot][await run(_pick, f"verse:{slot}", VERSES[slot])]
    parts = [f"**{line}**", ""]
    got_any = False
    for tid, short in TRANSLATIONS[lang]:
        v = await fetch_verse(ref, tid)
        if v:
            got_any = True
            parts.append(f"> *{v[0]}*")
            parts.append(f"> — {v[1]} ({short})")
            parts.append("")
    if not got_any:
        parts += [f"\U0001F4D6 Basahin ngayon: **{ref}**", ""]
        STATE["last_error"] = "Couldn't reach the Bible site, so the verse reference was posted instead."
    parts.append(f"\U0001F64F {prayer}")
    e = discord.Embed(title=s["title"], description="\n".join(parts), color=s["color"])
    e.set_footer(text=f"Daily word · {s['label']}")
    return e


async def post(gid: int, slot: str, by_hand: bool = False) -> str:
    """Send one message to this server's chosen channel. Returns '' or what went wrong."""
    ch_id = await run(channel_id, gid)
    ch = bot.get_channel(ch_id) if ch_id else None
    if ch is None:
        return "Pick a channel first."
    STATE["last_error"] = ""
    embed = await compose(slot)
    try:
        await ch.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        return f"The bot can't post in #{ch.name}. Give it Send Messages and Embed Links there."
    now = dt.datetime.now(TZ)
    await run(_log_sent, {"at": now.strftime("%b %d, %I:%M %p").replace(" 0", " "), "slot": SLOTS[slot]["label"],
                          "channel": ch.name, "line": embed.description.split("\n")[0].strip("*")[:120],
                          "by_hand": by_hand})
    return ""


def due_slot(now: dt.datetime) -> tuple | None:
    """(slot, date key) if a post is due at this moment, else None."""
    for slot, s in SLOTS.items():
        at = dt.datetime.combine(now.date(), s["at"], tzinfo=TZ)
        if at <= now < at + GRACE:
            return slot, f"{now.date().isoformat()}:{slot}"
    return None


async def tick(now: dt.datetime | None = None) -> list:
    """Post anything that's due. Safe to call often: each time of day posts once per server."""
    now = now or dt.datetime.now(TZ)
    due = due_slot(now)
    sent = []
    if due is None or not bot.is_ready():
        return sent
    slot, key = due
    for gid in await run(db.allowed_guild_ids):
        if not await run(is_on, gid) or await run(db.get_setting, f"dw_last:{gid}", "") == key:
            continue
        await run(db.set_setting, f"dw_last:{gid}", key)        # mark first, so a slow send never doubles
        problem = await post(gid, slot)
        if problem:
            log.warning("Daily word for %s not sent: %s", gid, problem)
        else:
            sent.append((gid, slot))
    return sent


@tasks.loop(seconds=30, reconnect=True)
async def daily_loop():
    try:
        await tick()
    except Exception:
        log.exception("Daily word check failed (will retry)")


@daily_loop.before_loop
async def _wait():
    await bot.wait_until_ready()


def _start():
    if not daily_loop.is_running():
        daily_loop.start()


discord_bot.EXTRA_SETUP.append(_start)
