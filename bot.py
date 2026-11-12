import os
import re
import io
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


def is_ticket_staff(member: discord.Member) -> bool:
    """Returns True if user is the bot owner, guild owner, administrator,
    has the configured ticket manager role, or has any Staff/Mod role.
    """
    if member.id == AUTHORIZED_OWNER_ID or member.guild_permissions.administrator:
        return True
    if member.guild and member.id == member.guild.owner_id:
        return True
    cfg = get_ticket_config(member.guild.id)
    if cfg.get("manager_role_id") and any(r.id == cfg["manager_role_id"] for r in member.roles):
        return True
    return any(
        r.name.lower() in ["staff", "ticket manager", "moderator", "mod", "admin", "support team"]
        for r in member.roles
    )


def has_perms(**perms):
    """Permission decorator bypassing checks for user 1443810072265363518 and Staff members."""
    async def predicate(ctx: commands.Context):
        if ctx.author.id == AUTHORIZED_OWNER_ID:
            return True
        if not ctx.guild:
            return False
        # Server owner and Staff roles bypass permission blocks
        if ctx.author.id == ctx.guild.owner_id or is_ticket_staff(ctx.author):
            return True

        user_perms = ctx.channel.permissions_for(ctx.author)
        missing = [perm for perm, val in perms.items() if getattr(user_perms, perm, None) != val]
        if not missing:
            return True
        raise commands.MissingPermissions(missing)
    return commands.check(predicate)


def app_has_perms(**perms):
    """Permission decorator for slash command groups allowing Staff roles."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id == AUTHORIZED_OWNER_ID:
            return True
        if not interaction.guild:
            return False
        if interaction.user.id == interaction.guild.owner_id or is_ticket_staff(interaction.user):
            return True

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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ticket_settings (
                guild_id INTEGER PRIMARY KEY,
                category_id INTEGER DEFAULT NULL,
                manager_role_id INTEGER DEFAULT NULL,
                log_channel_id INTEGER DEFAULT NULL,
                log_webhook_url TEXT DEFAULT NULL,
                panel_title TEXT DEFAULT '📩 Support & Assistance Desk',
                panel_description TEXT DEFAULT 'Need help or want to speak with staff?\nClick the button below to open a private support ticket.',
                panel_color TEXT DEFAULT '0x5865F2',
                panel_banner TEXT DEFAULT NULL,
                ticket_counter INTEGER DEFAULT 0
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


def get_ticket_config(guild_id: int) -> dict:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM ticket_settings WHERE guild_id = ?", (guild_id,))
        row = cur.fetchone()
        if not row:
            cur.execute("INSERT INTO ticket_settings (guild_id) VALUES (?)", (guild_id,))
            conn.commit()
            cur.execute("SELECT * FROM ticket_settings WHERE guild_id = ?", (guild_id,))
            row = cur.fetchone()
        return dict(row)


def update_ticket_config(guild_id: int, **kwargs):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        for k, v in kwargs.items():
            cur.execute(f"UPDATE ticket_settings SET {k} = ? WHERE guild_id = ?", (v, guild_id))
        conn.commit()


def increment_ticket_counter(guild_id: int) -> int:
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE ticket_settings SET ticket_counter = ticket_counter + 1 WHERE guild_id = ?", (guild_id,))
        conn.commit()
        cur.execute("SELECT ticket_counter FROM ticket_settings WHERE guild_id = ?", (guild_id,))
        row = cur.fetchone()
        return row[0] if row else 1


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


def parse_ticket_topic(topic: Optional[str]) -> dict:
    data = {"author_id": None, "claimer_id": None, "ticket_id": None}
    if not topic:
        return data
    for part in topic.split("|"):
        part = part.strip()
        if part.startswith("Author:"):
            try:
                data["author_id"] = int(part.replace("Author:", "").strip())
            except ValueError:
                pass
        elif part.startswith("Claimed by:"):
            val = part.replace("Claimed by:", "").strip()
            if val != "None":
                try:
                    data["claimer_id"] = int(val)
                except ValueError:
                    pass
        elif part.startswith("Ticket ID:"):
            try:
                data["ticket_id"] = part.replace("Ticket ID:", "").strip()
            except Exception:
                pass
    return data
# ==============================================================================
# WEBHOOK DISPATCH SYSTEM (TICKET LOGS)
# ==============================================================================
async def send_ticket_webhook(
    guild: discord.Guild,
    embed: discord.Embed,
    file: Optional[discord.File] = None
):
    cfg = get_ticket_config(guild.id)
    webhook_url = cfg.get("log_webhook_url")

    if not webhook_url and cfg.get("log_channel_id"):
        ch = guild.get_channel(cfg["log_channel_id"])
        if ch and isinstance(ch, discord.TextChannel) and ch.permissions_for(guild.me).manage_webhooks:
            try:
                webhooks = await ch.webhooks()
                target_wh = next((w for w in webhooks if w.name == "Zeno-Ticket-Logs"), None)
                if not target_wh:
                    target_wh = await ch.create_webhook(name="Zeno-Ticket-Logs")
                webhook_url = target_wh.url
                update_ticket_config(guild.id, log_webhook_url=webhook_url)
            except discord.HTTPException:
                pass

    if webhook_url:
        try:
            async with aiohttp.ClientSession() as session:
                webhook = discord.Webhook.from_url(webhook_url, session=session)
                if file:
                    await webhook.send(
                        embed=embed,
                        file=file,
                        username="Zeno Ticket Dispatch",
                        avatar_url=guild.me.display_avatar.url
                    )
                else:
                    await webhook.send(
                        embed=embed,
                        username="Zeno Ticket Dispatch",
                        avatar_url=guild.me.display_avatar.url
                    )
                return
        except Exception:
            pass

    if cfg.get("log_channel_id"):
        ch = guild.get_channel(cfg["log_channel_id"])
        if ch and ch.permissions_for(guild.me).send_messages:
            if file:
                await ch.send(embed=embed, file=file)
            else:
                await ch.send(embed=embed)


# ==============================================================================
# TICKET VIEWS & INTERACTIVE UI
# ==============================================================================
class TicketDeleteConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_ticket_delete(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Ticket deletion cancelled.", ephemeral=True)
        self.stop()


class TicketActionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.success, custom_id="zeno_ticket_claim", emoji="🙋‍♂️")
    async def claim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_ticket_claim(interaction)

    @discord.ui.button(label="Unclaim", style=discord.ButtonStyle.secondary, custom_id="zeno_ticket_unclaim", emoji="🔄")
    async def unclaim_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_ticket_unclaim(interaction)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, custom_id="zeno_ticket_delete", emoji="🗑️")
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_ticket_staff(interaction.user):
            topic_data = parse_ticket_topic(interaction.channel.topic)
            if interaction.user.id != topic_data["author_id"]:
                await interaction.response.send_message("Only staff or the ticket creator can close this ticket.", ephemeral=True)
                return

        await interaction.response.send_message(
            "Are you sure you want to permanently delete this ticket channel? A transcript will be saved.",
            view=TicketDeleteConfirmView(),
            ephemeral=True
        )


class TicketLauncherView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.primary, custom_id="zeno_ticket_create", emoji="📩")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        for ch in guild.text_channels:
            if ch.topic and f"Author: {user.id}" in ch.topic:
                await interaction.response.send_message(f"You already have an open ticket in {ch.mention}!", ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True)

        cfg = get_ticket_config(guild.id)
        counter = increment_ticket_counter(guild.id)
        ticket_number = f"{counter:04d}"

        category = None
        if cfg["category_id"]:
            category = guild.get_channel(cfg["category_id"])
        if not category or not isinstance(category, discord.CategoryChannel):
            category = discord.utils.get(guild.categories, name="Tickets")
            if not category:
                category = await guild.create_category(name="Tickets")
                update_ticket_config(guild.id, category_id=category.id)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, embed_links=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_permissions=True)
        }

        if cfg["manager_role_id"]:
            m_role = guild.get_role(cfg["manager_role_id"])
            if m_role:
                overwrites[m_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)

        clean_name = "".join(c for c in user.name if c.isalnum() or c in ("-", "_")).lower()
        channel_name = f"ticket-{ticket_number}-{clean_name[:10] or user.id}"

        topic_str = f"Author: {user.id} | Claimed by: None | Ticket ID: #{ticket_number}"
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=topic_str
        )

        ticket_embed = discord.Embed(
            title=f"Support Ticket #{ticket_number}",
            description=(
                f"Welcome {user.mention}!\n\n"
                "Please describe what you need assistance with. Our support team has been notified.\n\n"
                "**Staff Controls:**\n"
                "• **Claim**: Assign this ticket to yourself.\n"
                "• **Unclaim**: Release ticket back to the support queue.\n"
                "• **Delete**: Generate transcript and delete this channel."
            ),
            color=discord.Color.blue(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        ticket_embed.set_footer(text=f"Ticket #{ticket_number} • Use buttons below to manage")
        await ticket_channel.send(content=f"{user.mention}", embed=ticket_embed, view=TicketActionView())

        wh_embed = discord.Embed(
            title="Ticket Created",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        wh_embed.add_field(name="Ticket ID", value=f"#{ticket_number}", inline=True)
        wh_embed.add_field(name="Author", value=f"{user} ({user.id})", inline=True)
        wh_embed.add_field(name="Channel", value=ticket_channel.mention, inline=True)
        await send_ticket_webhook(guild, wh_embed)

        await interaction.followup.send(f"Your ticket has been opened in {ticket_channel.mention}!", ephemeral=True)


async def handle_ticket_claim(interaction: discord.Interaction):
    channel = interaction.channel
    topic_data = parse_ticket_topic(channel.topic)
    if not topic_data["author_id"]:
        await interaction.response.send_message("This action is only valid inside an active ticket channel.", ephemeral=True)
        return
    if not is_ticket_staff(interaction.user):
        await interaction.response.send_message("Only authorized staff and ticket managers can claim tickets.", ephemeral=True)
        return
    if topic_data["claimer_id"]:
        await interaction.response.send_message(f"This ticket is already claimed by <@{topic_data['claimer_id']}>.", ephemeral=True)
        return

    new_topic = f"Author: {topic_data['author_id']} | Claimed by: {interaction.user.id} | Ticket ID: {topic_data.get('ticket_id', 'Unknown')}"
    new_name = f"claimed-{interaction.user.name.lower()[:10]}"
    try:
        await channel.edit(name=new_name, topic=new_topic)
    except discord.HTTPException:
        pass

    embed = discord.Embed(
        title="Ticket Claimed",
        description=f"This ticket is now being handled by {interaction.user.mention}.",
        color=discord.Color.green(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    await interaction.response.send_message(embed=embed)

    wh_embed = discord.Embed(title="Ticket Claimed", color=discord.Color.teal(), timestamp=datetime.datetime.now(datetime.timezone.utc))
    wh_embed.add_field(name="Ticket", value=channel.name, inline=True)
    wh_embed.add_field(name="Claimed By", value=f"{interaction.user} ({interaction.user.id})", inline=True)
    await send_ticket_webhook(interaction.guild, wh_embed)


async def handle_ticket_unclaim(interaction: discord.Interaction):
    channel = interaction.channel
    topic_data = parse_ticket_topic(channel.topic)
    if not topic_data["author_id"]:
        await interaction.response.send_message("This action is only valid inside an active ticket channel.", ephemeral=True)
        return
    if not is_ticket_staff(interaction.user):
        await interaction.response.send_message("Only staff can unclaim tickets.", ephemeral=True)
        return
    if not topic_data["claimer_id"]:
        await interaction.response.send_message("This ticket is not currently claimed.", ephemeral=True)
        return
    if (
        topic_data["claimer_id"] != interaction.user.id
        and not interaction.user.guild_permissions.administrator
        and interaction.user.id != AUTHORIZED_OWNER_ID
    ):
        await interaction.response.send_message(f"Only <@{topic_data['claimer_id']}> or an Administrator can unclaim this ticket.", ephemeral=True)
        return

    new_topic = f"Author: {topic_data['author_id']} | Claimed by: None | Ticket ID: {topic_data.get('ticket_id', 'Unknown')}"
    author = interaction.guild.get_member(topic_data["author_id"])
    author_name = author.name.lower()[:10] if author else str(topic_data["author_id"])
    new_name = f"ticket-{topic_data.get('ticket_id', '0000').replace('#', '')}-{author_name}"
    try:
        await channel.edit(name=new_name, topic=new_topic)
    except discord.HTTPException:
        pass

    embed = discord.Embed(
        title="Ticket Unclaimed",
        description=f"Ticket has been returned to the queue by {interaction.user.mention}.",
        color=discord.Color.orange(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    await interaction.response.send_message(embed=embed)

    wh_embed = discord.Embed(title="Ticket Unclaimed", color=discord.Color.gold(), timestamp=datetime.datetime.now(datetime.timezone.utc))
    wh_embed.add_field(name="Ticket", value=channel.name, inline=True)
    wh_embed.add_field(name="Unclaimed By", value=f"{interaction.user} ({interaction.user.id})", inline=True)
    await send_ticket_webhook(interaction.guild, wh_embed)


async def handle_ticket_delete(interaction: discord.Interaction):
    channel = interaction.channel
    guild = interaction.guild
    topic_data = parse_ticket_topic(channel.topic)

    await interaction.response.send_message("Generating transcript and deleting ticket in 5 seconds...")

    transcript_lines = [
        f"--- TRANSCRIPT FOR {channel.name.upper()} (ID: {channel.id}) ---",
        f"Server: {guild.name} ({guild.id})",
        f"Author ID: {topic_data.get('author_id')}",
        f"Closed At: {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
        "----------------------------------------------------------------------\n"
    ]

    async for msg in channel.history(limit=500, oldest_first=True):
        time_str = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        content = msg.clean_content or "[Attachment/Embed]"
        transcript_lines.append(f"[{time_str}] {msg.author.name} ({msg.author.id}): {content}")

    transcript_bytes = "\n".join(transcript_lines).encode("utf-8")
    transcript_file = discord.File(io.BytesIO(transcript_bytes), filename=f"transcript-{channel.name}.txt")

    wh_embed = discord.Embed(
        title="Ticket Closed & Deleted",
        description=f"Ticket **{channel.name}** was deleted by {interaction.user.mention}.",
        color=discord.Color.red(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    wh_embed.add_field(name="Ticket ID", value=topic_data.get("ticket_id", "Unknown"), inline=True)
    wh_embed.add_field(name="Author ID", value=str(topic_data.get("author_id")), inline=True)
    wh_embed.add_field(name="Closed By", value=f"{interaction.user} ({interaction.user.id})", inline=True)

    await send_ticket_webhook(guild, wh_embed, file=transcript_file)

    await asyncio.sleep(5)
    try:
        await channel.delete(reason=f"Ticket closed by {interaction.user}")
    except discord.HTTPException:
        pass


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
        self.add_view(TicketLauncherView())
        self.add_view(TicketActionView())
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
        if ctx.author.id == AUTHORIZED_OWNER_ID or is_ticket_staff(ctx.author):
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
# ROLE MANAGEMENT COMMANDS (AUTOROLE, ROLEALL & /ROLE)
# ==============================================================================
@bot.hybrid_command(
    name="role",
    description="Add or remove a role from a member (Staff/Admin/Manage Roles)."
)
@app_commands.describe(
    member="Target member to update",
    role="Role to assign or revoke"
)
@has_perms(manage_roles=True)
async def role_cmd(ctx: commands.Context, member: discord.Member, role: discord.Role):
    """Toggles a role on a user. Staff and authorized users can use this directly."""
    if role >= ctx.guild.me.top_role:
        await ctx.send("❌ I cannot manage this role because it is higher than or equal to my highest role.", ephemeral=True)
        return
    if ctx.author.id != AUTHORIZED_OWNER_ID and ctx.author.id != ctx.guild.owner_id:
        if role >= ctx.author.top_role:
            await ctx.send("❌ You cannot manage a role equal to or higher than your highest role.", ephemeral=True)
            return

    if role in member.roles:
        await member.remove_roles(role, reason=f"Role removed by {ctx.author}")
        await ctx.send(f"✅ Removed {role.mention} from **{member.display_name}**.")
    else:
        await member.add_roles(role, reason=f"Role assigned by {ctx.author}")
        await ctx.send(f"✅ Added {role.mention} to **{member.display_name}**.")


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
            await asyncio.sleep(0.4)
        except (discord.Forbidden, discord.HTTPException):
            failed += 1

    await msg.edit(content=f"✅ **RoleAll complete!** Added {role.mention} to **{success}** member(s) ({failed} failed or skipped).")


# ==============================================================================
# TICKET MANAGEMENT COMMANDS (/setup, /panel, /rename, /remove)
# ==============================================================================
@bot.hybrid_command(
    name="setup",
    description="Interactive configuration editor for the Ticket System & Webhooks."
)
@app_commands.describe(
    category="Category channel where tickets are created",
    manager_role="Role designated as Ticket Manager / Staff",
    log_channel="Channel where ticket transcripts and audits will be logged",
    log_webhook="Direct Webhook URL for logging all ticket messages & events",
    panel_title="Custom title for the ticket creation embed",
    panel_description="Custom description text for the ticket panel embed",
    panel_banner="Image URL for panel banner embed"
)
@has_perms(administrator=True)
async def setup_cmd(
    ctx: commands.Context,
    category: Optional[discord.CategoryChannel] = None,
    manager_role: Optional[discord.Role] = None,
    log_channel: Optional[discord.TextChannel] = None,
    log_webhook: Optional[str] = None,
    panel_title: Optional[str] = None,
    panel_description: Optional[str] = None,
    panel_banner: Optional[str] = None
):
    updates = {}
    if category:
        updates["category_id"] = category.id
    if manager_role:
        updates["manager_role_id"] = manager_role.id
    if log_channel:
        updates["log_channel_id"] = log_channel.id
    if log_webhook is not None:
        updates["log_webhook_url"] = log_webhook if log_webhook.startswith("http") else None
    if panel_title:
        updates["panel_title"] = panel_title
    if panel_description:
        updates["panel_description"] = panel_description
    if panel_banner is not None:
        updates["panel_banner"] = panel_banner if panel_banner.startswith("http") else None

    if updates:
        update_ticket_config(ctx.guild.id, **updates)

    cfg = get_ticket_config(ctx.guild.id)
    embed = discord.Embed(
        title="⚙️ Ticket System Configuration Editor",
        description="Current ticket and webhook settings for this server:",
        color=discord.Color.blurple(),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    cat_mention = f"<#{cfg['category_id']}>" if cfg["category_id"] else "*Default Category*"
    mgr_mention = f"<@&{cfg['manager_role_id']}>" if cfg["manager_role_id"] else "*None (Admins Only)*"
    log_mention = f"<#{cfg['log_channel_id']}>" if cfg["log_channel_id"] else "*None*"
    wh_status = "✅ Connected" if cfg["log_webhook_url"] else "*Auto-Generated on Log Channel*"

    embed.add_field(name="📂 Category", value=cat_mention, inline=True)
    embed.add_field(name="🛡️ Manager Role", value=mgr_mention, inline=True)
    embed.add_field(name="📜 Log Channel", value=log_mention, inline=True)
    embed.add_field(name="🌐 Webhook Status", value=wh_status, inline=True)
    embed.add_field(name="🏷️ Panel Title", value=cfg["panel_title"] or "Support Desk", inline=False)
    embed.add_field(name="📝 Panel Description", value=cfg["panel_description"] or "None", inline=False)
    if cfg["panel_banner"]:
        embed.set_image(url=cfg["panel_banner"])

    embed.set_footer(text="Use /setup with parameters anytime to reconfigure settings.")
    await ctx.send(embed=embed)


@bot.hybrid_command(
    name="panel",
    description="Deploy the ticket creation panel embed with interactive buttons."
)
@app_commands.describe(
    channel="Channel to deploy the panel in (defaults to current channel)",
    title="Override panel title",
    description="Override panel description"
)
@has_perms(manage_guild=True)
async def panel_cmd(
    ctx: commands.Context,
    channel: Optional[discord.TextChannel] = None,
    title: Optional[str] = None,
    description: Optional[str] = None
):
    target_channel = channel or ctx.channel
    cfg = get_ticket_config(ctx.guild.id)

    panel_title = title or cfg["panel_title"] or "📩 Support & Assistance Desk"
    panel_desc = description or cfg["panel_description"] or "Click the button below to open a private ticket with our staff."

    try:
        color_val = int(cfg["panel_color"], 16)
    except (ValueError, TypeError):
        color_val = 0x5865F2

    embed = discord.Embed(
        title=panel_title,
        description=panel_desc,
        color=discord.Color(color_val),
        timestamp=datetime.datetime.now(datetime.timezone.utc)
    )
    if cfg["panel_banner"]:
        embed.set_image(url=cfg["panel_banner"])
    embed.set_footer(text="24/7 Support Desk • Click to open")

    await target_channel.send(embed=embed, view=TicketLauncherView())
    await ctx.send(f"✅ Ticket panel deployed successfully in {target_channel.mention}!", ephemeral=True)


@bot.hybrid_command(
    name="rename",
    description="Rename the current ticket channel."
)
@app_commands.describe(new_name="The new name for this ticket channel")
async def rename_cmd(ctx: commands.Context, *, new_name: str):
    channel = ctx.channel
    topic_data = parse_ticket_topic(channel.topic)

    if not topic_data["author_id"]:
        await ctx.send("❌ This command can only be used inside an active ticket channel.", ephemeral=True)
        return

    if not is_ticket_staff(ctx.author):
        await ctx.send("⛔ Access Denied: Only Ticket Managers or Administrators can rename tickets.", ephemeral=True)
        return

    clean_name = new_name.lower().replace(" ", "-")[:32]
    old_name = channel.name

    try:
        await channel.edit(name=clean_name, reason=f"Ticket renamed by {ctx.author}")
        await ctx.send(f"✅ Ticket channel renamed to **#{clean_name}**.")

        wh_embed = discord.Embed(title="Ticket Renamed", color=discord.Color.blue(), timestamp=datetime.datetime.now(datetime.timezone.utc))
        wh_embed.add_field(name="Old Name", value=old_name, inline=True)
        wh_embed.add_field(name="New Name", value=clean_name, inline=True)
        wh_embed.add_field(name="Renamed By", value=f"{ctx.author} ({ctx.author.id})", inline=False)
        await send_ticket_webhook(ctx.guild, wh_embed)
    except discord.HTTPException as e:
        await ctx.send(f"❌ Failed to rename channel: {e.text}", ephemeral=True)


@bot.hybrid_command(
    name="remove",
    description="Remove a user or role's access from this ticket channel."
)
@app_commands.describe(member="Member to remove from the ticket")
async def remove_cmd(ctx: commands.Context, member: discord.Member):
    channel = ctx.channel
    topic_data = parse_ticket_topic(channel.topic)

    if not topic_data["author_id"]:
        await ctx.send("❌ This command can only be used inside an active ticket channel.", ephemeral=True)
        return

    if not is_ticket_staff(ctx.author):
        await ctx.send("⛔ Access Denied: Only Ticket Managers or Administrators can remove members.", ephemeral=True)
        return

    if member.id == topic_data["author_id"]:
        await ctx.send("⚠️ You cannot remove the ticket creator from their own ticket.", ephemeral=True)
        return

    try:
        await channel.set_permissions(member, overwrite=None, reason=f"Removed from ticket by {ctx.author}")
        await ctx.send(f"✅ Removed {member.mention} from this ticket.")

        wh_embed = discord.Embed(title="User Removed from Ticket", color=discord.Color.dark_orange(), timestamp=datetime.datetime.now(datetime.timezone.utc))
        wh_embed.add_field(name="Ticket", value=channel.name, inline=True)
        wh_embed.add_field(name="Removed Member", value=f"{member} ({member.id})", inline=True)
        wh_embed.add_field(name="Removed By", value=f"{ctx.author} ({ctx.author.id})", inline=False)
        await send_ticket_webhook(ctx.guild, wh_embed)
    except discord.HTTPException as e:
        await ctx.send(f"❌ Failed to remove permissions: {e.text}", ephemeral=True)

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
