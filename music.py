"""
Music: play audio from YouTube (or a direct link) in a voice channel, with a per-server queue.

Commands: /music <song or link>, /skip, /stop, /queue, /pause, /resume, /leave.

Clean audio: the track is streamed with ffmpeg straight to Opus at the voice channel's own bitrate (no volume
boost, so it never clips/distorts), with auto-reconnect so a hiccup doesn't cut it off.

Needs on Render (added to requirements.txt): PyNaCl (voice), yt-dlp (finds the audio), imageio-ffmpeg (ffmpeg
binary). The heavy imports are guarded so the bot still runs if a deploy hasn't picked them up yet -- the
commands just say music isn't set up.
"""
import asyncio
import logging
from collections import deque

import discord
from discord import app_commands

import discord_bot
from discord_bot import bot

log = logging.getLogger("music")

# ffmpeg binary (bundled, static) -- resolved once, guarded
try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:                                # noqa: BLE001
    FFMPEG = None

_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
_OPTS = "-vn"
_YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "cachedir": False,
    "skip_download": True,
}

_queues: dict[int, deque] = {}       # guild_id -> deque[Track]
_now: dict[int, "Track"] = {}        # guild_id -> currently playing Track


class Track:
    def __init__(self, title, stream, webpage, duration, requester_id, text_channel_id):
        self.title = title
        self.stream = stream
        self.webpage = webpage
        self.duration = duration
        self.requester_id = requester_id
        self.text_channel_id = text_channel_id


def ready() -> bool:
    """Is music set up? (ffmpeg present and yt-dlp importable)"""
    if FFMPEG is None:
        return False
    try:
        import yt_dlp  # noqa: F401
    except Exception:                            # noqa: BLE001
        return False
    return True


def _dur(seconds) -> str:
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ------------------------- resolving -------------------------
def _extract(query: str) -> dict:
    import yt_dlp
    with yt_dlp.YoutubeDL(_YDL_OPTS) as y:
        info = y.extract_info(query, download=False)
    if info and "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise ValueError("no results")
        info = entries[0]
    return info


async def resolve(query: str, requester_id: int, text_channel_id: int) -> Track:
    info = await asyncio.to_thread(_extract, query)
    stream = info.get("url")
    if not stream:
        raise ValueError("no stream url")
    return Track(info.get("title") or "(unknown)", stream, info.get("webpage_url") or query,
                 info.get("duration"), requester_id, text_channel_id)


# ------------------------- playback -------------------------
def _source(track: Track, channel) -> discord.AudioSource:
    # match the channel's own bitrate so audio is clean and never over/under-driven
    kbps = 128
    try:
        kbps = max(64, min(256, int(getattr(channel, "bitrate", 128000) // 1000)))
    except Exception:                            # noqa: BLE001
        pass
    # Let discord.py encode PCM to Opus through PyNaCl. The bundled imageio-ffmpeg
    # binary is not guaranteed to include libopus, so FFmpegOpusAudio can fail on
    # hosts where that encoder is absent.
    return discord.FFmpegPCMAudio(track.stream, executable=FFMPEG,
                                  before_options=_BEFORE, options=_OPTS)


def _after(gid: int):
    def cb(err):
        if err:
            log.warning("Playback error in %s: %s", gid, err)
        try:
            asyncio.run_coroutine_threadsafe(play_next(gid), bot.loop)
        except Exception:                        # noqa: BLE001
            log.exception("Could not advance the queue in %s", gid)
    return cb


async def play_next(gid: int) -> None:
    guild = bot.get_guild(gid)
    vc = guild.voice_client if guild else None
    if vc is None:
        _now.pop(gid, None)
        return
    q = _queues.get(gid)
    if not q:
        _now.pop(gid, None)
        return
    track = q.popleft()
    _now[gid] = track
    try:
        vc.play(_source(track, vc.channel), after=_after(gid))
    except Exception:                            # noqa: BLE001
        log.exception("Failed to start a track in %s", gid)
        asyncio.run_coroutine_threadsafe(play_next(gid), bot.loop)
        return
    ch = guild.get_channel(track.text_channel_id) if guild else None
    if ch is not None:
        try:
            await ch.send(embed=_now_embed(track))
        except discord.HTTPException:
            pass


def _now_embed(track: Track) -> discord.Embed:
    e = discord.Embed(title="\U0001f3b5 Now playing", color=0x5865F2,
                      description=f"**[{track.title}]({track.webpage})**")
    d = _dur(track.duration)
    if d:
        e.add_field(name="Length", value=d, inline=True)
    e.add_field(name="Requested by", value=f"<@{track.requester_id}>", inline=True)
    return e


# ------------------------- commands -------------------------
def _not_ready_msg() -> str:
    return ("Music isn't set up yet. The server still needs to deploy PyNaCl + yt-dlp + ffmpeg "
            "(already in requirements.txt). Wait for it to go Live again, then try once more.")


@bot.tree.command(name="music", description="Play a song in your voice channel (YouTube or a link)")
@app_commands.guild_only()
@app_commands.describe(song="Song name to search, or a YouTube / audio link")
async def music_cmd(interaction: discord.Interaction, song: str):
    # Acknowledge the slash command immediately; yt-dlp import, voice connect,
    # and stream extraction can take longer than Discord's 3-second deadline.
    await interaction.response.defer(thinking=True)
    if not ready():
        await interaction.followup.send(_not_ready_msg(), ephemeral=True)
        return
    vs = getattr(interaction.user, "voice", None)
    if vs is None or vs.channel is None:
        await interaction.followup.send("Join a voice channel first, then try again.",
                                        ephemeral=True)
        return
    guild = interaction.guild
    vc = guild.voice_client
    try:
        if vc is None:
            vc = await vs.channel.connect()
        elif vc.channel.id != vs.channel.id:
            await vc.move_to(vs.channel)
    except discord.ClientException:
        await interaction.followup.send("I'm already in another voice channel. Use /leave first, then try again.")
        return
    except Exception:                            # noqa: BLE001
        await interaction.followup.send("I can't join that voice channel. Check my Connect and Speak permissions.")
        return
    try:
        track = await resolve(song, interaction.user.id, interaction.channel_id)
    except Exception:                            # noqa: BLE001
        await interaction.followup.send(f"I couldn't find or play that: **{song}**")
        return
    q = _queues.setdefault(guild.id, deque())
    busy = vc.is_playing() or vc.is_paused() or guild.id in _now
    if busy:
        q.append(track)
        await interaction.followup.send(f"\u2795 Added to the queue (#{len(q)}): **{track.title}**")
    else:
        q.append(track)
        await play_next(guild.id)
        await interaction.followup.send(f"\u25b6\ufe0f Now playing: **{track.title}**")


@bot.tree.command(name="skip", description="Skip the song that's playing")
@app_commands.guild_only()
async def skip_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc is None or not (vc.is_playing() or vc.is_paused()):
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    vc.stop()                                     # triggers the after-callback -> next track
    await interaction.response.send_message("\u23ed\ufe0f Skipped.")


@bot.tree.command(name="stop", description="Stop the music, clear the queue, and leave")
@app_commands.guild_only()
async def stop_cmd(interaction: discord.Interaction):
    gid = interaction.guild_id
    vc = interaction.guild.voice_client
    _queues.pop(gid, None)
    _now.pop(gid, None)
    if vc is not None:
        try:
            await vc.disconnect(force=True)
        except Exception:                        # noqa: BLE001
            pass
    await interaction.response.send_message("\u23f9\ufe0f Stopped and cleared the queue.")


@bot.tree.command(name="leave", description="Make the bot leave the voice channel")
@app_commands.guild_only()
async def leave_cmd(interaction: discord.Interaction):
    gid = interaction.guild_id
    vc = interaction.guild.voice_client
    _queues.pop(gid, None)
    _now.pop(gid, None)
    if vc is None:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        return
    try:
        await vc.disconnect(force=True)
    except Exception:                            # noqa: BLE001
        pass
    await interaction.response.send_message("\U0001f44b Left the voice channel.")


@bot.tree.command(name="pause", description="Pause the music")
@app_commands.guild_only()
async def pause_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc is None or not vc.is_playing():
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        return
    vc.pause()
    await interaction.response.send_message("\u23f8\ufe0f Paused. Use /resume to continue.")


@bot.tree.command(name="resume", description="Resume the paused music")
@app_commands.guild_only()
async def resume_cmd(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc is None or not vc.is_paused():
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        return
    vc.resume()
    await interaction.response.send_message("\u25b6\ufe0f Resumed.")


@bot.tree.command(name="queue", description="Show the music queue")
@app_commands.guild_only()
async def queue_cmd(interaction: discord.Interaction):
    gid = interaction.guild_id
    now = _now.get(gid)
    q = _queues.get(gid) or deque()
    if now is None and not q:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return
    lines = []
    if now is not None:
        d = _dur(now.duration)
        lines.append(f"\u25b6\ufe0f **{now.title}**" + (f" ({d})" if d else ""))
    for i, t in enumerate(list(q)[:20], 1):
        d = _dur(t.duration)
        lines.append(f"{i}. {t.title}" + (f" ({d})" if d else ""))
    extra = len(q) - 20
    if extra > 0:
        lines.append(f"\u2026 and {extra} more")
    e = discord.Embed(title="\U0001f3b6 Queue", color=0x5865F2, description="\n".join(lines))
    await interaction.response.send_message(embed=e)

