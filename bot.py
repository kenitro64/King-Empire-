import os
import re
import sqlite3
import datetime
import asyncio
from collections import defaultdict
from typing import Optional
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# ==============================================================================
# CONFIGURATION & GLOBAL SETTINGS
# ==============================================================================
TOKEN = os.getenv("DISCORD_TOKEN", "YOUR_BOT_TOKEN_HERE")
AUTHORIZED_OWNER_ID = 1443810072265363518

LOG_CHANNEL_NAME = "mod-logs"

# Persistent storage configuration (Railway volume support)
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "server_data.db")

# Chat flood tracker: { guild_id: { user_id: [timestamps] } }
user_message_buckets = defaultdict(lambda: defaultdict(list))

INVITE_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.(?:gg|io|me|li)|discord(?:app)?\.com/invite)/[a-zA-Z0-9]+",
    re.IGNORECASE
)
LINK_REGEX = re.compile(
    r"https?://(?:[-\w.]|(?:%[\da-fA-F]{2}))+",
    re.IGNORECASE
)


# ==============================================================================
# DYNAMIC PREFIX & PERMISSION BYPASS LOGIC
# ==============================================================================
def get_prefix(bot_instance, message: discord.Message):
    """Allows user 1443810072265363518 to send commands with NO PREFIX (''), '!', or 't!'.
    All other members must use '!' or 't!'.
    """
    if message.author and message.author.id == AUTHORIZED_OWNER_ID:
        return ["!", "t!", ""]
    return ["!", "t!"]


def has_perms(**perms):
    """Permission decorator bypassing checks for user 1443810072265363518."""
    async def predicate(ctx: commands.Context):
        if ctx.author.id == AUTHORIZED_OWNER_ID:
            return True
        if not ctx.guild:
            return False
        user_perms = ctx.channel.permissions_for(ctx.author)
        missing = [perm for perm, val in perms.items() if getattr(user_perms, perm, None) != val]
        if not missing:
            return True
        raise commands.MissingPermissions(missing)
    return commands.check(predicate)


def app_has_perms(**perms):
    """Permission decorator for slash command groups."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == AUTHORIZED_OWNER_ID:
            return True
        if not interaction.guild:
            return False
        user_perms = interaction.user.guild_permissions
        missing = [perm for perm, val in perms.items() if getattr(user_perms, perm, None) != val]
        if not missing:
            return True
        raise app_commands.MissingPermissions(missing)
    return app_commands.check(predicate)
  # ==============================================================================
# DATABASE ENGINE
# ==============================================================================
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS welcome_settings (
                guild_id INTEGER PRIMARY KEY,
                is_enabled INTEGER DEFAULT 1,
                channel_id INTEGER DEFAULT NULL,
                auto_role_id INTEGER DEFAULT NULL,
                title TEXT DEFAULT 'Welcome to {server}!',
                message TEXT DEFAULT 'Hey {mention}, welcome aboard! You are member #{count}.',
                color TEXT DEFAULT '0x2ECC71',
                banner_url TEXT DEFAULT NULL,
                show_avatar INTEGER DEFAULT 1
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mod_settings (
                guild_id INTEGER PRIMARY KEY,
                log_channel_id INTEGER DEFAULT NULL,
                automod_invites INTEGER DEFAULT 1,
                automod_links INTEGER DEFAULT 0,
                automod_spam INTEGER DEFAULT 1,
                automod_mention_limit INTEGER DEFAULT 5
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS infractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                user_id INTEGER,
                mod_id INTEGER,
                reason TEXT,
                timestamp TEXT
            )
        """)
        conn.commit()


def get_welcome_config(guild_id: int) -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM welcome_settings WHERE guild_id = ?", (guild_id,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO welcome_settings (guild_id) VALUES (?)", (guild_id,))
            conn.commit()
            cur.execute("SELECT * FROM welcome_settings WHERE guild_id = ?", (guild_id,))
            row = cur.fetchone()
        return dict(row)


def update_welcome_config(guild_id: int, **kwargs):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        for k, v in kwargs.items():
            cur.execute(f"UPDATE welcome_settings SET {k} = ? WHERE guild_id = ?", (v, guild_id))
        conn.commit()


def get_mod_config(guild_id: int) -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM mod_settings WHERE guild_id = ?", (guild_id,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO mod_settings (guild_id) VALUES (?)", (guild_id,))
            conn.commit()
            cur.execute("SELECT * FROM mod_settings WHERE guild_id = ?", (guild_id,))
            row = cur.fetchone()
        return dict(row)


def update_mod_config(guild_id: int, **kwargs):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        for k, v in kwargs.items():
            cur.execute(f"UPDATE mod_settings SET {k} = ? WHERE guild_id = ?", (v, guild_id))
        conn.commit()


def add_infraction(guild_id: int, user_id: int, mod_id: int, reason: str) -> int:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        cur.execute(
            "INSERT INTO infractions (guild_id, user_id, mod_id, reason, timestamp) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, mod_id, reason, now)
        )
        conn.commit()
        return cur.lastrowid


def get_infractions(guild_id: int, user_id: int) -> list[dict]:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM infractions WHERE guild_id = ? AND user_id = ? ORDER BY id DESC",
            (guild_id, user_id)
        )
        return [dict(r) for r in cur.fetchall()]


def remove_infraction(guild_id: int, infraction_id: int) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM infractions WHERE guild_id = ? AND id = ?", (guild_id, infraction_id))
        conn.commit()
        return cur.rowcount > 0


def clear_infractions(guild_id: int, user_id: int) -> int:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM infractions WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        conn.commit()
        return cur.rowcount


# ==============================================================================
# AUDIT & DISPATCH UTILITIES
# ==============================================================================
async def send_punishment_dm(
    member: discord.Member,
    action: str,
    reason: str,
    duration: Optional[str] = None
) -> bool:
    try:
        embed = discord.Embed(
            title=f"Moderation Notice: {action}",
            description=f"You have received an action in **{member.guild.name}**.",
            color=discord.Color.red(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        if member.guild.icon:
            embed.set_thumbnail(url=member.guild.icon.url)
        embed.add_field(name="Server", value=member.guild.name, inline=True)
        embed.add_field(name="Action Taken", value=action, inline=True)
        if duration:
            embed.add_field(name="Duration", value=duration, inline=True)
        embed.add_field(name="Reason", value=reason or "No reason provided", inline=False)
        embed.set_footer(text="If you believe this was an error, contact server staff.")
        await member.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def send_log_embed(guild: discord.Guild, embed: discord.Embed):
    cfg = get_mod_config(guild.id)
    channel = None
    if cfg["log_channel_id"]:
        channel = guild.get_channel(cfg["log_channel_id"])
    if not channel:
        channel = discord.utils.get(guild.text_channels, name=LOG_CHANNEL_NAME)

    if channel and channel.permissions_for(guild.me).send_messages:
        await channel.send(embed=embed)


def format_text(template: str, member: discord.Member) -> str:
    if not template:
        return ""
    return (
        template.replace("{user}", member.name)
        .replace("{mention}", member.mention)
        .replace("{server}", member.guild.name)
        .replace("{count}", str(member.guild.member_count))
        .replace("{created_at}", member.created_at.strftime("%b %d, %Y"))
    )


async def fetch_image_bytes(
    image_source: Optional[str] = None,
    attachment: Optional[discord.Attachment] = None
) -> Optional[bytes]:
    if attachment:
        return await attachment.read()

    if image_source:
        if os.path.exists(image_source) and os.path.isfile(image_source):
            with open(image_source, "rb") as f:
                return f.read()

        if image_source.startswith("http://") or image_source.startswith("https://"):
            async with aiohttp.ClientSession() as session:
                async with session.get(image_source) as resp:
                    if resp.status == 200:
                        return await resp.read()
    return None
  # ==============================================================================
# BOT INITIALIZATION & EVENTS
# ==============================================================================
class AdvancedBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix=get_prefix, intents=intents)

    async def setup_hook(self):
        init_db()
        await self.tree.sync()
        print("Application commands synchronized globally.")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")


bot = AdvancedBot()


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, (commands.MissingPermissions, app_commands.MissingPermissions)):
        if ctx.author.id == AUTHORIZED_OWNER_ID:
            return
        await ctx.send("⛔ Permission denied. You lack permissions to run this command.", ephemeral=True)
        return
    raise error


# ==============================================================================
# AUTOMOD DEFENSE SYSTEM
# ==============================================================================
@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return

    member: discord.Member = message.author

    if (
        member.id == AUTHORIZED_OWNER_ID
        or member.guild_permissions.administrator
        or member.guild_permissions.manage_guild
    ):
        await bot.process_commands(message)
        return

    cfg = get_mod_config(message.guild.id)
    content = message.content

    # 1. Anti-Invite Detection
    if cfg["automod_invites"] and INVITE_REGEX.search(content):
        await message.delete()
        await message.channel.send(f"{member.mention}, posting Discord invite links is forbidden.", delete_after=5)
        log = discord.Embed(title="AutoMod: Invite Link Blocked", color=discord.Color.red())
        log.add_field(name="User", value=f"{member} ({member.id})", inline=True)
        log.add_field(name="Channel", value=message.channel.mention, inline=True)
        log.add_field(name="Message", value=content[:1000], inline=False)
        await send_log_embed(message.guild, log)
        return

    # 2. General External Link Filter
    if cfg["automod_links"] and LINK_REGEX.search(content):
        await message.delete()
        await message.channel.send(f"{member.mention}, posting external links is restricted.", delete_after=5)
        log = discord.Embed(title="AutoMod: Link Blocked", color=discord.Color.red())
        log.add_field(name="User", value=f"{member} ({member.id})", inline=True)
        log.add_field(name="Channel", value=message.channel.mention, inline=True)
        await send_log_embed(message.guild, log)
        return

    # 3. Mass-Mention Spam Detection
    if cfg["automod_mention_limit"] and len(message.mentions) >= cfg["automod_mention_limit"]:
        await message.delete()
        dm_sent = await send_punishment_dm(
            member=member,
            action="Timed Out (AutoMod)",
            reason="Exceeded mass-mention threshold",
            duration="15 minutes"
        )
        dm_status = "Delivered" if dm_sent else "Failed (Closed DMs)"
        try:
            await member.timeout(datetime.timedelta(minutes=15), reason="AutoMod: Mass mentions")
            await message.channel.send(f"{member.mention} timed out for 15m (Mass Mentions).", delete_after=6)
        except discord.HTTPException:
            pass

        log = discord.Embed(title="AutoMod: Mass Mention Triggered", color=discord.Color.dark_red())
        log.add_field(name="User", value=f"{member} ({member.id})", inline=True)
        log.add_field(name="Mentions Count", value=str(len(message.mentions)), inline=True)
        log.add_field(name="DM Status", value=dm_status, inline=True)
        await send_log_embed(message.guild, log)
        return

    # 4. Anti-Flood Spam (5 messages within 3 seconds)
    if cfg["automod_spam"]:
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        buckets = user_message_buckets[message.guild.id][member.id]
        buckets.append(now)
        user_message_buckets[message.guild.id][member.id] = [t for t in buckets if now - t <= 3]

        if len(user_message_buckets[message.guild.id][member.id]) >= 5:
            user_message_buckets[message.guild.id][member.id].clear()
            dm_sent = await send_punishment_dm(
                member=member,
                action="Timed Out (AutoMod)",
                reason="Exceeded 5 messages in 3 seconds (Chat Flooding)",
                duration="10 minutes"
            )
            dm_status = "Delivered" if dm_sent else "Failed (Closed DMs)"
            try:
                await member.timeout(datetime.timedelta(minutes=10), reason="AutoMod: Chat flooding")
                await message.channel.send(f"{member.mention} timed out for 10m (Chat Flooding).", delete_after=6)
            except discord.HTTPException:
                pass

            log = discord.Embed(title="AutoMod: Anti-Spam Triggered", color=discord.Color.dark_orange())
            log.add_field(name="User", value=f"{member} ({member.id})", inline=True)
            log.add_field(name="DM Status", value=dm_status, inline=True)
            await send_log_embed(message.guild, log)
            return

    await bot.process_commands(message)


# ---------------- AUDIT LOG DISPATCHERS ----------------
@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild or message.author.bot:
        return
    embed = discord.Embed(
        title="Message Deleted",
        color=discord.Color.dark_grey(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    embed.add_field(name="Author", value=f"{message.author} ({message.author.id})", inline=True)
    embed.add_field(name="Channel", value=message.channel.mention, inline=True)
    embed.add_field(
        name="Content",
        value=message.content[:1000] if message.content else "*Empty / Media*",
        inline=False
    )
    await send_log_embed(message.guild, embed)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if not before.guild or before.author.bot or before.content == after.content:
        return
    embed = discord.Embed(
        title="Message Edited",
        color=discord.Color.light_grey(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    embed.add_field(name="Author", value=f"{before.author} ({before.author.id})", inline=True)
    embed.add_field(name="Channel", value=before.channel.mention, inline=True)
    embed.add_field(name="Before", value=before.content[:500] or "*Empty*", inline=False)
    embed.add_field(name="After", value=after.content[:500] or "*Empty*", inline=False)
    await send_log_embed(before.guild, embed)


# ---------------- MEMBER JOIN & AUTO-ROLE ASSIGNMENT ----------------
@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    cfg = get_welcome_config(guild.id)

    # Automatically give configured auto-role
    if cfg["auto_role_id"] and guild.me.guild_permissions.manage_roles:
        role = guild.get_role(cfg["auto_role_id"])
        if role and role < guild.me.top_role:
            try:
                await member.add_roles(role, reason="Auto-role on member join")
            except discord.HTTPException:
                pass

    if not cfg["is_enabled"] or not cfg["channel_id"]:
        return

    channel = guild.get_channel(cfg["channel_id"])
    if not channel or not channel.permissions_for(guild.me).send_messages:
        return

    try:
        color_val = int(cfg["color"], 16)
    except (ValueError, TypeError):
        color_val = 0x2ECC71

    embed = discord.Embed(
        title=format_text(cfg["title"], member),
        description=format_text(cfg["message"], member),
        color=discord.Color(color_val),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    if cfg["show_avatar"]:
        embed.set_thumbnail(url=member.display_avatar.url)
    if cfg["banner_url"]:
        embed.set_image(url=cfg["banner_url"])
    embed.set_footer(text=f"User ID: {member.id}")

    await channel.send(content=member.mention, embed=embed)
  # ==============================================================================
# ROLE MANAGEMENT COMMANDS (AUTOROLE & ROLEALL)
# ==============================================================================
@bot.hybrid_command(
    name="autorole",
    description="Set or remove the automatic role given to new members on join."
)
@app_commands.describe(role="Role to automatically grant (leave empty to disable)")
@has_perms(manage_roles=True)
async def autorole_cmd(ctx: commands.Context, role: Optional[discord.Role] = None):
    if role:
        if role >= ctx.guild.me.top_role:
            await ctx.send("❌ I cannot assign this role because it is higher than or equal to my top role.", ephemeral=True)
            return
        update_welcome_config(ctx.guild.id, auto_role_id=role.id)
        await ctx.send(f"✅ **Auto-role configured!** New members will automatically receive {role.mention} on join.")
    else:
        update_welcome_config(ctx.guild.id, auto_role_id=None)
        await ctx.send("✅ **Auto-role disabled.** Joining members will not receive an automatic role.")


@bot.hybrid_command(
    name="roleall",
    description="Give a specific role to all members in the server."
)
@app_commands.describe(
    role="Role to assign to all members",
    include_bots="Whether to give the role to bot accounts as well (defaults to False)"
)
@has_perms(manage_roles=True)
async def roleall_cmd(ctx: commands.Context, role: discord.Role, include_bots: Optional[bool] = False):
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ I cannot assign this role: it is higher than or equal to my highest role.", ephemeral=True)
        return
    if ctx.author.id != AUTHORIZED_OWNER_ID and role >= ctx.author.top_role and ctx.author.id != ctx.guild.owner_id:
        await ctx.send("❌ You cannot assign a role equal to or higher than your own highest role.", ephemeral=True)
        return

    await ctx.defer()
    msg = await ctx.send(f"⏳ Giving {role.mention} to all members. Please wait...")

    success = 0
    failed = 0

    for m in ctx.guild.members:
        if not include_bots and m.bot:
            continue
        if role in m.roles:
            continue
        try:
            await m.add_roles(role, reason=f"roleall executed by {ctx.author}")
            success += 1
            await asyncio.sleep(0.4)  # Safeguard to prevent Discord rate limits
        except (discord.Forbidden, discord.HTTPException):
            failed += 1

    await msg.edit(content=f"✅ **RoleAll complete!** Added {role.mention} to **{success}** member(s) ({failed} failed or skipped).")


# ==============================================================================
# HYBRID MODERATION COMMANDS
# ==============================================================================
@bot.hybrid_command(
    name="clear",
    description="Clear messages with optional member, content, or type filters."
)
@app_commands.describe(
    amount="Number of messages to delete (1-100)",
    member="Filter by specific user",
    contains="Filter by keyword or phrase"
)
@has_perms(manage_messages=True)
async def clear(
    ctx: commands.Context,
    amount: int,
    member: Optional[discord.Member] = None,
    contains: Optional[str] = None
):
    amount = min(max(amount, 1), 100)

    def check(msg: discord.Message) -> bool:
        if member and msg.author.id != member.id:
            return False
        if contains and contains.lower() not in msg.content.lower():
            return False
        return True

    deleted = await ctx.channel.purge(limit=amount, check=check)
    await ctx.send(f"Cleared **{len(deleted)}** message(s).", ephemeral=True, delete_after=5)


@bot.hybrid_command(name="kick", description="Kick a member from the server with auto-DM.")
@app_commands.describe(member="Member to kick", reason="Reason for removal")
@has_perms(kick_members=True)
async def kick(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason: Optional[str] = "No reason provided"
):
    if (
        ctx.author.id != AUTHORIZED_OWNER_ID
        and member.top_role >= ctx.author.top_role
        and ctx.author.id != ctx.guild.owner_id
    ):
        await ctx.send("Cannot kick members with equal or higher roles.", ephemeral=True)
        return

    dm_sent = await send_punishment_dm(member=member, action="Kicked", reason=reason)
    dm_status = "DM Delivered" if dm_sent else "DM Failed (DMs Closed/Blocked)"

    await member.kick(reason=reason)
    await ctx.send(f"Kicked **{member.display_name}** | Reason: *{reason}* ({dm_status})")


@bot.hybrid_command(name="ban", description="Ban a member from the server with auto-DM.")
@app_commands.describe(
    member="Member to ban",
    delete_days="Days of messages to delete (0-7)",
    reason="Reason for ban"
)
@has_perms(ban_members=True)
async def ban(
    ctx: commands.Context,
    member: discord.Member,
    delete_days: Optional[int] = 0,
    *,
    reason: Optional[str] = "No reason provided"
):
    if (
        ctx.author.id != AUTHORIZED_OWNER_ID
        and member.top_role >= ctx.author.top_role
        and ctx.author.id != ctx.guild.owner_id
    ):
        await ctx.send("Cannot ban members with equal or higher roles.", ephemeral=True)
        return

    dm_sent = await send_punishment_dm(member=member, action="Banned", reason=reason)
    dm_status = "DM Delivered" if dm_sent else "DM Failed (DMs Closed/Blocked)"

    await member.ban(delete_message_days=min(max(delete_days, 0), 7), reason=reason)
    await ctx.send(f"Banned **{member.display_name}** | Reason: *{reason}* ({dm_status})")


@bot.hybrid_command(name="softban", description="Ban and unban to purge messages and kick with auto-DM.")
@app_commands.describe(member="Member to softban", reason="Reason for softban")
@has_perms(ban_members=True)
async def softban(
    ctx: commands.Context,
    member: discord.Member,
    *,
    reason: Optional[str] = "Softban message purge"
):
    if (
        ctx.author.id != AUTHORIZED_OWNER_ID
        and member.top_role >= ctx.author.top_role
        and ctx.author.id != ctx.guild.owner_id
    ):
        await ctx.send("Cannot softban members with equal or higher roles.", ephemeral=True)
        return

    dm_sent = await send_punishment_dm(member=member, action="Softbanned", reason=reason)
    dm_status = "DM Delivered" if dm_sent else "DM Failed (DMs Closed/Blocked)"

    await member.ban(delete_message_days=7, reason=f"Softban: {reason}")
    await ctx.guild.unban(member, reason="Softban cleanup")
    await ctx.send(f"Softbanned **{member.display_name}** ({dm_status}).")


@bot.hybrid_command(name="unban", description="Unban a user by their numerical Discord ID.")
@app_commands.describe(user_id="Discord User ID to unban", reason="Reason for unban")
@has_perms(ban_members=True)
async def unban(
    ctx: commands.Context,
    user_id: str,
    *,
    reason: Optional[str] = "No reason provided"
):
    try:
        user_obj = await bot.fetch_user(int(user_id))
        await ctx.guild.unban(user_obj, reason=reason)
        await ctx.send(f"Unbanned user **{user_obj}**.")
    except (ValueError, discord.NotFound):
        await ctx.send("User ID not found in server ban list.", ephemeral=True)


@bot.hybrid_command(name="timeout", description="Apply a communication timeout with auto-DM notification.")
@app_commands.describe(member="Target member", minutes="Duration in minutes", reason="Reason for timeout")
@has_perms(moderate_members=True)
async def timeout(
    ctx: commands.Context,
    member: discord.Member,
    minutes: int,
    *,
    reason: Optional[str] = "No reason provided"
):
    if (
        ctx.author.id != AUTHORIZED_OWNER_ID
        and member.top_role >= ctx.author.top_role
        and ctx.author.id != ctx.guild.owner_id
    ):
        await ctx.send("Cannot timeout members with equal or higher roles.", ephemeral=True)
        return

    dm_sent = await send_punishment_dm(
        member=member,
        action="Timed Out",
        reason=reason,
        duration=f"{minutes}m"
    )
    dm_status = "DM Delivered" if dm_sent else "DM Failed (DMs Closed/Blocked)"

    await member.timeout(datetime.timedelta(minutes=minutes), reason=reason)
    await ctx.send(f"Timed out **{member.display_name}** for {minutes}m | Reason: *{reason}* ({dm_status})")


@bot.hybrid_command(name="untimeout", description="Remove timeout from a member.")
@has_perms(moderate_members=True)
async def untimeout(ctx: commands.Context, member: discord.Member):
    await member.timeout(None, reason=f"Removed by {ctx.author}")
    await ctx.send(f"Lifted timeout for **{member.display_name}**.")


@bot.hybrid_command(name="slowmode", description="Set channel slowmode in seconds (0 to disable).")
@app_commands.describe(seconds="Slowmode duration (0-21600 seconds)")
@has_perms(manage_channels=True)
async def slowmode(ctx: commands.Context, seconds: int):
    seconds = min(max(seconds, 0), 21600)
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(f"Channel slowmode set to **{seconds}s**.")


@bot.hybrid_command(name="lock", description="Lock the current channel to prevent chatting.")
@has_perms(manage_channels=True)
async def lock(ctx: commands.Context):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send("🔒 Channel locked.")


@bot.hybrid_command(name="unlock", description="Unlock the current channel to allow chatting.")
@has_perms(manage_channels=True)
async def unlock(ctx: commands.Context):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = True
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send("🔓 Channel unlocked.")


# ---------------- STRIKE & WARNINGS ----------------
@bot.hybrid_command(name="warn", description="Issue a formal strike with auto-DM.")
@app_commands.describe(member="Member to warn", reason="Infraction strike details")
@has_perms(moderate_members=True)
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str):
    infraction_id = add_infraction(ctx.guild.id, member.id, ctx.author.id, reason)
    total_warns = len(get_infractions(ctx.guild.id, member.id))

    dm_sent = await send_punishment_dm(
        member=member,
        action=f"Warning (Strike #{total_warns})",
        reason=f"{reason} (Case #{infraction_id})"
    )
    dm_status = "DM Delivered" if dm_sent else "DM Failed (DMs Closed/Blocked)"

    await ctx.send(f"Warned **{member.display_name}** (Strike #{total_warns}, Case ID: `#{infraction_id}`) | {dm_status}")


@bot.hybrid_command(name="warnings", description="View all warnings registered for a member.")
@app_commands.describe(member="Member to inspect")
@has_perms(moderate_members=True)
async def warnings(ctx: commands.Context, member: discord.Member):
    records = get_infractions(ctx.guild.id, member.id)
    if not records:
        await ctx.send(f"**{member.display_name}** has 0 registered infractions.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"Infractions for {member.display_name} ({len(records)})",
        color=discord.Color.dark_gold()
    )
    for r in records[:10]:
        embed.add_field(
            name=f"Case #{r['id']} | Mod: <@{r['mod_id']}>",
            value=f"**Reason:** {r['reason']}\n**Date:** {r['timestamp'][:10]}",
            inline=False
        )
    await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name="delwarn", description="Delete an infraction by its Case ID.")
@app_commands.describe(case_id="Case ID to remove")
@has_perms(moderate_members=True)
async def delwarn(ctx: commands.Context, case_id: int):
    success = remove_infraction(ctx.guild.id, case_id)
    if success:
        await ctx.send(f"Deleted Case `#{case_id}`.")
    else:
        await ctx.send(f"Case `#{case_id}` was not found.", ephemeral=True)


@bot.hybrid_command(name="clearwarns", description="Clear all infractions for a member.")
@app_commands.describe(member="Target member")
@has_perms(administrator=True)
async def clearwarns(ctx: commands.Context, member: discord.Member):
    count = clear_infractions(ctx.guild.id, member.id)
    await ctx.send(f"Cleared **{count}** infractions for {member.display_name}.")
  # ==============================================================================
# BOT PROFILE CUSTOMIZATION COMMANDS
# ==============================================================================
@bot.hybrid_command(
    name="setav",
    aliases=["setavatar"],
    description="Set the bot's profile picture (Guild Owner only)."
)
@app_commands.describe(
    image="Image URL or local file path",
    attachment="Upload an image file directly"
)
async def set_avatar(
    ctx: commands.Context,
    image: Optional[str] = None,
    attachment: Optional[discord.Attachment] = None
):
    if ctx.author.id != ctx.guild.owner_id and ctx.author.id != AUTHORIZED_OWNER_ID:
        await ctx.send("⛔ Access Denied: Only the **Server Owner** can use this command.", ephemeral=True)
        return

    if not image and not attachment:
        await ctx.send("Provide an image URL, a local file path, or an image attachment.", ephemeral=True)
        return

    await ctx.defer(ephemeral=True)

    img_data = await fetch_image_bytes(image_source=image, attachment=attachment)
    if not img_data:
        await ctx.send("❌ Failed to load image. Check the file path or URL and try again.", ephemeral=True)
        return

    try:
        await bot.user.edit(avatar=img_data)
        await ctx.send("✅ Successfully updated the bot's profile picture!", ephemeral=True)
    except discord.HTTPException as e:
        await ctx.send(
            f"❌ Failed to update avatar: {e.text} (Note: Discord limits avatar updates to 2 per hour)",
            ephemeral=True
        )


@bot.hybrid_command(name="setbanner", description="Set the bot's banner (Guild Owner only).")
@app_commands.describe(
    image="Image URL or local file path",
    attachment="Upload an image file directly"
)
async def set_banner(
    ctx: commands.Context,
    image: Optional[str] = None,
    attachment: Optional[discord.Attachment] = None
):
    if ctx.author.id != ctx.guild.owner_id and ctx.author.id != AUTHORIZED_OWNER_ID:
        await ctx.send("⛔ Access Denied: Only the **Server Owner** can use this command.", ephemeral=True)
        return

    if not image and not attachment:
        await ctx.send("Provide an image URL, a local file path, or an image attachment.", ephemeral=True)
        return

    await ctx.defer(ephemeral=True)

    img_data = await fetch_image_bytes(image_source=image, attachment=attachment)
    if not img_data:
        await ctx.send("❌ Failed to load banner. Check the file path or URL and try again.", ephemeral=True)
        return

    try:
        await bot.user.edit(banner=img_data)
        await ctx.send("✅ Successfully updated the bot's banner!", ephemeral=True)
    except discord.HTTPException as e:
        await ctx.send(f"❌ Failed to update banner: {e.text}", ephemeral=True)


# ==============================================================================
# AUTOMOD CONFIGURATION SLASH GROUP
# ==============================================================================
automod_group = app_commands.Group(name="automod", description="Configure automated chat defense filters")


@automod_group.command(name="invites", description="Toggle anti-discord invite link detection.")
@app_has_perms(administrator=True)
async def automod_invites(interaction: discord.Interaction, enabled: bool):
    update_mod_config(interaction.guild_id, automod_invites=1 if enabled else 0)
    status = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"Anti-Invite protection is now **{status}**.")


@automod_group.command(name="links", description="Toggle all external URL filtering.")
@app_has_perms(administrator=True)
async def automod_links(interaction: discord.Interaction, enabled: bool):
    update_mod_config(interaction.guild_id, automod_links=1 if enabled else 0)
    status = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"Anti-Link protection is now **{status}**.")


@automod_group.command(name="antispam", description="Toggle rapid chat flood protection.")
@app_has_perms(administrator=True)
async def automod_spam(interaction: discord.Interaction, enabled: bool):
    update_mod_config(interaction.guild_id, automod_spam=1 if enabled else 0)
    status = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"Anti-Spam flood protection is now **{status}**.")


@automod_group.command(name="mention_limit", description="Set maximum allowed mentions before auto-timeout.")
@app_commands.describe(limit="Threshold (e.g. 5). Set to 0 to disable.")
@app_has_perms(administrator=True)
async def automod_mention_limit(interaction: discord.Interaction, limit: int):
    update_mod_config(interaction.guild_id, automod_mention_limit=max(limit, 0))
    await interaction.response.send_message(f"Mention limit threshold set to **{limit}**.")


@automod_group.command(name="logchannel", description="Set the channel where moderation audits are recorded.")
@app_has_perms(administrator=True)
async def automod_logchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    update_mod_config(interaction.guild_id, log_channel_id=channel.id)
    await interaction.response.send_message(f"Audit log channel set to {channel.mention}.")

bot.tree.add_command(automod_group)


# ==============================================================================
# WELCOME CONFIGURATION SLASH GROUP
# ==============================================================================
welcome_group = app_commands.Group(name="welcome", description="Configure the welcome greeting system")


@welcome_group.command(name="channel", description="Set destination channel for welcome messages.")
@app_has_perms(manage_guild=True)
async def welcome_channel(interaction: discord.Interaction, target_channel: discord.TextChannel):
    update_welcome_config(interaction.guild_id, channel_id=target_channel.id)
    await interaction.response.send_message(f"Welcome channel set to {target_channel.mention}.", ephemeral=True)


@welcome_group.command(name="message", description="Set welcome text and title.")
@app_has_perms(manage_guild=True)
async def welcome_message(interaction: discord.Interaction, description: str, title: Optional[str] = None):
    updates = {"message": description}
    if title:
        updates["title"] = title
    update_welcome_config(interaction.guild_id, **updates)
    await interaction.response.send_message("Welcome message updated.", ephemeral=True)


@welcome_group.command(name="style", description="Set colors, banners, and avatar display.")
@app_has_perms(manage_guild=True)
async def welcome_style(
    interaction: discord.Interaction,
    hex_color: Optional[str] = None,
    banner_url: Optional[str] = None,
    show_avatar: Optional[bool] = None
):
    updates = {}
    if hex_color:
        updates["color"] = hex_color.replace("#", "0x")
    if banner_url is not None:
        updates["banner_url"] = banner_url if banner_url.startswith("http") else None
    if show_avatar is not None:
        updates["show_avatar"] = 1 if show_avatar else 0
    update_welcome_config(interaction.guild_id, **updates)
    await interaction.response.send_message("Welcome design updated.", ephemeral=True)


@welcome_group.command(name="autorole", description="Assign a role automatically to new arrivals.")
@app_has_perms(manage_roles=True)
async def welcome_autorole(interaction: discord.Interaction, role: Optional[discord.Role] = None):
    update_welcome_config(interaction.guild_id, auto_role_id=role.id if role else None)
    target = role.mention if role else "None"
    await interaction.response.send_message(f"Auto-role set to {target}.", ephemeral=True)


@welcome_group.command(name="toggle", description="Turn welcome messages on or off.")
@app_has_perms(manage_guild=True)
async def welcome_toggle(interaction: discord.Interaction, enabled: bool):
    update_welcome_config(interaction.guild_id, is_enabled=1 if enabled else 0)
    status = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"Welcome messages {status}.", ephemeral=True)


@welcome_group.command(name="preview", description="Preview your configured welcome embed.")
@app_has_perms(manage_guild=True)
async def welcome_preview(interaction: discord.Interaction):
    cfg = get_welcome_config(interaction.guild_id)
    try:
        color_val = int(cfg["color"], 16)
    except (ValueError, TypeError):
        color_val = 0x2ECC71

    embed = discord.Embed(
        title=format_text(cfg["title"], interaction.user),
        description=format_text(cfg["message"], interaction.user),
        color=discord.Color(color_val)
    )
    if cfg["show_avatar"]:
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
    if cfg["banner_url"]:
        embed.set_image(url=cfg["banner_url"])
    await interaction.response.send_message(embed=embed, ephemeral=True)

bot.tree.add_command(welcome_group)


# ---------------- LAUNCH ----------------
if __name__ == "__main__":
    bot.run(TOKEN)
