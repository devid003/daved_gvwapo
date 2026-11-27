"""
Support tickets (customer service).

Flow: a member taps "Open a ticket" on a panel -> picks language (Tagalog / English) -> picks a topic
(YTA / FBA) -> types why they opened it -> the bot opens a PRIVATE channel under that topic's category,
the AI answers first, and the mod/admin role is pinged with a summary posted to a staff channel.

Owner setup (run these in the server):
  /ticketsetup  - set the panel channel, YTA + FBA categories, mod role, and the staff channel
  /ticketpanel  - post the "Open a ticket" panel
  /ticketai     - turn the AI first-reply on/off and set its Monid key + model (optional)
  /ticketclose  - close the current ticket (owner / admin, or the member who opened it)

The bot's role needs the "Manage Channels" permission so it can open and delete ticket channels.
"""
import asyncio
import logging

import discord
from discord import app_commands

import autoedit
import db
from discord_bot import bot, OWNER_IDS

log = logging.getLogger("tickets")
run = asyncio.to_thread

TOPICS = {"yta": "YTA (YouTube automation)", "fba": "FBA (Facebook automation)"}
LANGS = {"en": "English", "tl": "Tagalog"}
BLURPLE = 0x5865F2

_SYS = {
    "en": ("You are a friendly customer-support assistant for a Discord community about YouTube automation (YTA) "
           "and Facebook automation (FBA). Answer the member's question clearly and briefly, in English. If you "
           "cannot fully resolve it, reassure them that a human admin has been notified and will follow up."),
    "tl": ("Ikaw ay isang magalang na customer-support assistant para sa isang Discord community tungkol sa YouTube "
           "automation (YTA) at Facebook automation (FBA). Sagutin nang malinaw at maikli ang tanong ng miyembro, "
           "sa Tagalog. Kung hindi mo ito lubusang masagot, sabihin na may admin nang na-notify na tutulong sa kanya."),
}
_AI_FIRST_DONE: set[int] = set()  # ticket channel IDs whose first AI reply was delivered

_WELCOME = {
    "en": "Thanks for opening a ticket. Our AI will try to help first, and an admin has been notified. "
          "Describe your concern any time below.",
    "tl": "Salamat sa pag-open ng ticket. Susubukan ka munang tulungan ng aming AI, at may admin nang na-notify. "
          "Ilarawan ang iyong concern dito sa baba anumang oras.",
}


# ------------------------- config (per guild, stored in settings) -------------------------
def _s(name, gid):
    return f"{name}:{gid}"


def _get_id(name, gid):
    v = db.get_setting(_s(name, gid), "")
    return int(v) if v.isdigit() else 0


def panel_ch(gid):
    return _get_id("tkt_panel_ch", gid)


def cat_for(gid, topic):
    return _get_id("tkt_cat_" + topic, gid)


def mod_role_id(gid):
    return _get_id("tkt_mod_role", gid)


def staff_ch(gid):
    return _get_id("tkt_staff_ch", gid)


def ai_on(gid):
    return db.get_setting(_s("tkt_ai_on", gid), "1") != "0"


def ai_key(gid):
    return db.get_setting(_s("tkt_ai_key", gid), "").strip()


def ai_model(gid):
    return db.get_setting(_s("tkt_ai_model", gid), "").strip()


def ai_system(gid, lang):
    return db.get_setting(_s("tkt_sys", gid), "").strip() or _SYS.get(lang, _SYS["en"])


# ------------------------- the panel + step views -------------------------
def panel_message():
    e = discord.Embed(
        title="\U0001f3ab Need help? Open a ticket",
        description=("Tap **Open a ticket** below. You'll pick your language and topic, tell us what's wrong, and "
                     "our AI helps right away \u2014 an admin is notified too.\n\nOnly you and the staff can see "
                     "your ticket."),
        color=BLURPLE)
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Open a ticket", style=discord.ButtonStyle.primary,
                                 emoji="\U0001f4ac", custom_id="tkt:open"))
    return e, v


def _lang_view():
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Tagalog", style=discord.ButtonStyle.primary, custom_id="tkt:lang:tl"))
    v.add_item(discord.ui.Button(label="English", style=discord.ButtonStyle.secondary, custom_id="tkt:lang:en"))
    return v


def _topic_view(lang):
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="YTA", style=discord.ButtonStyle.primary, custom_id=f"tkt:topic:yta:{lang}"))
    v.add_item(discord.ui.Button(label="FBA", style=discord.ButtonStyle.success, custom_id=f"tkt:topic:fba:{lang}"))
    return v


def _close_view():
    v = discord.ui.View(timeout=None)
    v.add_item(discord.ui.Button(label="Close ticket", style=discord.ButtonStyle.danger, custom_id="tkt:close"))
    return v


class _ReasonModal(discord.ui.Modal):
    def __init__(self, topic, lang):
        super().__init__(title="Open a ticket", custom_id=f"tktmodal:{topic}:{lang}", timeout=None)
        self.topic = topic
        self.lang = lang
        label = "Bakit ka nag-open ng ticket?" if lang == "tl" else "Why are you opening this ticket?"
        self.reason = discord.ui.TextInput(label=label[:45], style=discord.TextStyle.paragraph,
                                           max_length=1000, required=True, custom_id="reason")
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await _open_ticket(interaction, self.topic, self.lang, str(self.reason.value or "").strip())


# ------------------------- dispatch (called from prompt_commands.on_interaction for "tkt:" ids) -------------------------
async def on_component(interaction: discord.Interaction, cid: str):
    try:
        if cid == "tkt:open":
            await interaction.response.send_message(
                "Choose your language / Piliin ang wika:", view=_lang_view(), ephemeral=True)
        elif cid.startswith("tkt:lang:"):
            lang = cid.split(":")[2]
            lang = lang if lang in LANGS else "en"
            txt = "Anong topic ang ticket mo?" if lang == "tl" else "Pick a topic:"
            await interaction.response.edit_message(content=txt, view=_topic_view(lang))
        elif cid.startswith("tkt:topic:"):
            parts = cid.split(":")
            topic = parts[2] if len(parts) > 2 and parts[2] in TOPICS else "yta"
            lang = parts[3] if len(parts) > 3 and parts[3] in LANGS else "en"
            await interaction.response.send_modal(_ReasonModal(topic, lang))
        elif cid == "tkt:close":
            await _close_ticket(interaction)
    except Exception:
        log.exception("ticket component %s failed", cid)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong. Please try again.", ephemeral=True)
        except Exception:
            pass


# ------------------------- opening a ticket -------------------------
async def _open_ticket(interaction: discord.Interaction, topic: str, lang: str, reason: str):
    guild, member = interaction.guild, interaction.user
    if guild is None:
        return
    gid = guild.id
    await interaction.response.defer(ephemeral=True, thinking=True)
    if await run(db.open_ticket_count, gid, member.id) >= 1:
        await interaction.followup.send("You already have an open ticket \u2014 please use it.", ephemeral=True)
        return

    category = guild.get_channel(cat_for(gid, topic))
    if not isinstance(category, discord.CategoryChannel):
        category = None
    mod_role = guild.get_role(mod_role_id(gid))
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                              manage_channels=True, read_message_history=True),
        member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
    }
    if mod_role:
        overwrites[mod_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           read_message_history=True)
    name = f"{topic}-{member.name}"[:90]
    try:
        channel = await guild.create_text_channel(name, category=category, overwrites=overwrites,
                                                  reason=f"Support ticket ({topic})")
    except discord.Forbidden:
        await interaction.followup.send(
            "I can't open a ticket channel \u2014 I need the **Manage Channels** permission. Please tell the owner.",
            ephemeral=True)
        return
    except discord.HTTPException:
        await interaction.followup.send("Couldn't open a ticket right now. Please try again later.", ephemeral=True)
        return

    await run(db.create_ticket, gid, channel.id, member.id, str(member), topic, lang, reason)

    e = discord.Embed(title=f"Ticket \u2014 {TOPICS.get(topic, topic)}", color=BLURPLE,
                      description=_WELCOME.get(lang, _WELCOME["en"]))
    e.add_field(name=("Dahilan" if lang == "tl" else "Reason"), value=(reason[:1000] or "\u2014"), inline=False)
    e.set_footer(text=str(member))
    try:
        await channel.send(content=member.mention, embed=e, view=_close_view(),
                           allowed_mentions=discord.AllowedMentions(users=True))
    except discord.HTTPException:
        pass

    if ai_on(gid):
        ans = await _ai_answer(gid, lang, reason)
        if ans:
            try:
                await channel.send(f"\U0001f916 **AI Support (first reply)**\n{ans[:1850]}")
                _AI_FIRST_DONE.add(channel.id)
            except discord.HTTPException:
                log.exception("could not send first AI reply in ticket %s", channel.id)
        else:
            await channel.send("⚠️ **AI first reply is not available yet.** Staff replies are paused until the AI responds. "
                               "The server owner should check the Monid API key, model name, and Render logs.")
    else:
        _AI_FIRST_DONE.add(channel.id)

    await _notify(guild, channel, member, topic, lang, reason, mod_role)
    await interaction.followup.send(f"Your ticket is open: {channel.mention}", ephemeral=True)


async def _ai_answer(gid, lang, text):
    key, model = ai_key(gid), ai_model(gid)
    if not key or not model or not autoedit.good_key(key):
        log.error("ticket AI is enabled but not configured correctly (guild=%s key_set=%s model_set=%s)",
                  gid, bool(key), bool(model))
        return None
    body = {"model": model, "stream": False,
            "messages": [{"role": "system", "content": ai_system(gid, lang)},
                         {"role": "user", "content": (text or "")[:1500]}]}
    try:
        status, j, _ = await autoedit.monid_call(key, "POST", "/v1/chat/completions", body, timeout=60)
        if status == 200 and isinstance(j, dict):
            msg = (j.get("choices") or [{}])[0].get("message", {}) or {}
            answer = msg.get("content")
            if isinstance(answer, list):
                answer = "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in answer)
            answer = str(answer or "").strip()
            if answer:
                return answer
            log.error("ticket AI returned HTTP 200 but no message content (guild=%s model=%s)", gid, model)
        else:
            log.error("ticket AI request failed (guild=%s model=%s status=%s response=%r)", gid, model, status, str(j)[:500])
    except Exception:
        log.exception("ticket AI failed")
    return None


async def _notify(guild, channel, member, topic, lang, reason, mod_role):
    try:
        ping = mod_role.mention if mod_role else "@here"
        await channel.send(
            f"{ping} \u2014 new **{TOPICS.get(topic, topic)}** ticket from {member.mention}.",
            allowed_mentions=discord.AllowedMentions(roles=True, everyone=True, users=True))
    except discord.HTTPException:
        pass
    sid = staff_ch(guild.id)
    staff = guild.get_channel(sid) if sid else None
    if staff is None or staff.id == channel.id:
        return
    e = discord.Embed(title="\U0001f3ab New ticket", color=BLURPLE,
                      description=f"{member.mention} opened a **{TOPICS.get(topic, topic)}** ticket in "
                                  f"{channel.mention}.")
    e.add_field(name="Language", value=LANGS.get(lang, lang), inline=True)
    e.add_field(name="Member", value=str(member), inline=True)
    e.add_field(name="Reason", value=(reason[:1000] or "\u2014"), inline=False)
    e.timestamp = discord.utils.utcnow()
    try:
        await staff.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


# ------------------------- closing -------------------------
def _is_staff(user):
    p = getattr(user, "guild_permissions", None)
    return bool(user.id in OWNER_IDS or (p and (p.administrator or p.manage_guild)))


async def _close_ticket(interaction: discord.Interaction):
    ch = interaction.channel
    t = await run(db.get_ticket, getattr(ch, "id", 0))
    if not t or t.status != "open":
        await interaction.response.send_message("This isn't an open ticket channel.", ephemeral=True)
        return
    if not (_is_staff(interaction.user) or interaction.user.id == t.member_id):
        await interaction.response.send_message("Only staff or the ticket owner can close this.", ephemeral=True)
        return
    await run(db.close_ticket, ch.id)
    await interaction.response.send_message("\U0001f512 Closing this ticket in 10 seconds\u2026")
    try:
        await asyncio.sleep(10)
        await ch.delete(reason="Ticket closed")
    except discord.HTTPException:
        pass


# ------------------------- AI follow-ups inside open tickets -------------------------
async def _on_message(message: discord.Message):
    if message.guild is None or message.author.bot or not message.content:
        return
    t = await run(db.get_ticket, message.channel.id)
    if not t or t.status != "open":
        return
    if not ai_on(message.guild.id):
        return
    # Staff cannot send a human reply until the initial AI reply has been delivered.
    if message.author.id != t.member_id and message.channel.id not in _AI_FIRST_DONE:
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            await message.channel.send("⏳ Please wait: the AI must send its first reply before staff can respond.")
        except discord.HTTPException:
            pass
        return
    if message.author.id != t.member_id:
        return
    try:
        async with message.channel.typing():
            ans = await _ai_answer(message.guild.id, t.lang, message.content)
        if ans:
            await message.channel.send(f"\U0001f916 {ans[:1900]}")
    except discord.HTTPException:
        log.exception("ticket AI follow-up send failed in channel %s", message.channel.id)


def _install(name, handler):
    existing = getattr(bot, name, None)

    async def chained(*a, **k):
        if existing is not None:
            try:
                await existing(*a, **k)
            except Exception:
                log.exception("%s (earlier handler) failed", name)
        try:
            await handler(*a, **k)
        except Exception:
            log.exception("%s (tickets) failed", name)

    chained.__name__ = name
    setattr(bot, name, chained)


_install("on_message", _on_message)


# ------------------------- owner / admin commands -------------------------
@bot.tree.command(name="ticketsetup", description="Set up the support ticket system (owner/admin)")
@app_commands.guild_only()
@app_commands.describe(panel_channel="Where the Open-a-ticket panel is posted",
                       yta_category="Category that YTA tickets go under",
                       fba_category="Category that FBA tickets go under",
                       mod_role="Role to ping when a new ticket opens",
                       staff_channel="Channel where ticket summaries are posted")
async def ticketsetup_cmd(interaction: discord.Interaction, panel_channel: discord.TextChannel,
                          yta_category: discord.CategoryChannel, fba_category: discord.CategoryChannel,
                          mod_role: discord.Role, staff_channel: discord.TextChannel):
    if not _is_staff(interaction.user):
        await interaction.response.send_message("Only the owner or an admin can use this.", ephemeral=True)
        return
    # Acknowledge immediately so Discord does not show "The application did not respond"
    # while the database settings are being saved.
    await interaction.response.defer(ephemeral=True, thinking=True)
    gid = interaction.guild_id
    try:
        await run(db.set_setting, _s("tkt_panel_ch", gid), str(panel_channel.id))
        await run(db.set_setting, _s("tkt_cat_yta", gid), str(yta_category.id))
        await run(db.set_setting, _s("tkt_cat_fba", gid), str(fba_category.id))
        await run(db.set_setting, _s("tkt_mod_role", gid), str(mod_role.id))
        await run(db.set_setting, _s("tkt_staff_ch", gid), str(staff_channel.id))
    except Exception:
        log.exception("ticket setup save failed for guild %s", gid)
        await interaction.followup.send(
            "❌ Couldn't save the ticket setup. Please try again; the bot log has the error details.",
            ephemeral=True)
        return
    await interaction.followup.send(
        f"\u2705 Saved.\n\u2022 Panel: {panel_channel.mention}\n\u2022 YTA category: **{yta_category.name}**\n"
        f"\u2022 FBA category: **{fba_category.name}**\n\u2022 Ping role: {mod_role.mention}\n"
        f"\u2022 Summaries: {staff_channel.mention}\n\nNow run **/ticketpanel** to post the panel, and "
        f"**/ticketai** if you want the AI to answer first.",
        ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="ticketpanel", description="Post the Open-a-ticket panel (owner/admin)")
@app_commands.guild_only()
async def ticketpanel_cmd(interaction: discord.Interaction):
    if not _is_staff(interaction.user):
        await interaction.response.send_message("Only the owner or an admin can use this.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    cid = panel_ch(interaction.guild_id)
    ch = interaction.guild.get_channel(cid) if cid else None
    if ch is None:
        ch = interaction.channel
    e, v = panel_message()
    try:
        await ch.send(embed=e, view=v)
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I can't post in that channel. Check View Channel, Send Messages, and Embed Links permissions.",
            ephemeral=True)
        return
    except discord.HTTPException:
        log.exception("ticket panel post failed in guild %s", interaction.guild_id)
        await interaction.followup.send(
            "❌ Couldn't post the panel. Please try again; check the bot's channel permissions.",
            ephemeral=True)
        return
    await interaction.followup.send(f"✅ Panel posted in {ch.mention}.", ephemeral=True)


@bot.tree.command(name="ticketai", description="Turn the ticket AI on/off and set its key + model (owner/admin)")
@app_commands.guild_only()
@app_commands.describe(enabled="Should the AI answer first?",
                       monid_key="Your Monid key for the AI (kept private)",
                       model="Chat model, e.g. gpt-5-mini",
                       system="Optional custom instructions for the AI")
async def ticketai_cmd(interaction: discord.Interaction, enabled: bool,
                       monid_key: str = "", model: str = "", system: str = ""):
    if not _is_staff(interaction.user):
        await interaction.response.send_message("Only the owner or an admin can use this.", ephemeral=True)
        return
    gid = interaction.guild_id
    await run(db.set_setting, _s("tkt_ai_on", gid), "1" if enabled else "0")
    if monid_key.strip():
        await run(db.set_setting, _s("tkt_ai_key", gid), monid_key.strip()[:300])
    if model.strip():
        await run(db.set_setting, _s("tkt_ai_model", gid), model.strip()[:100])
    if system.strip():
        await run(db.set_setting, _s("tkt_sys", gid), system.strip()[:490])
    ready = bool(ai_key(gid) and ai_model(gid))
    note = "" if ready else " \u26a0\ufe0f Set both a key and a model for the AI to actually answer."
    await interaction.response.send_message(
        f"\u2705 Ticket AI is {'ON' if enabled else 'OFF'}.{note}", ephemeral=True)


@bot.tree.command(name="ticketclose", description="Close this ticket")
@app_commands.guild_only()
async def ticketclose_cmd(interaction: discord.Interaction):
    t = await run(db.get_ticket, interaction.channel_id)
    if not t or t.status != "open":
        await interaction.response.send_message("This isn't an open ticket channel.", ephemeral=True)
        return
    if not (_is_staff(interaction.user) or interaction.user.id == t.member_id):
        await interaction.response.send_message("Only staff or the ticket owner can close this.", ephemeral=True)
        return
    await run(db.close_ticket, interaction.channel_id)
    await interaction.response.send_message("\U0001f512 Closing this ticket in 10 seconds\u2026")
    try:
        await asyncio.sleep(10)
        await interaction.channel.delete(reason="Ticket closed")
    except discord.HTTPException:
        pass
