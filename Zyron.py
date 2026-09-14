import os
import discord
from discord import app_commands
from discord.ext import commands
from datetime import timedelta
import random
import requests
from collections import defaultdict, deque

# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
PREFIX = "1"

# Prefijos de comandos:
#   1ban @usuario
#   Zyron ban @usuario
#   zyron ban @usuario
# También acepta el prefijo "Zyron" sin depender de mayúsculas/minúsculas.
def get_prefix(bot, message):
    content = message.content or ""
    lowered = content.lower()

    if lowered.startswith("zyron"):
        # Conservamos exactamente el texto escrito para que discord.py
        # pueda consumir el prefijo aunque el usuario escriba Zyron/zyron/ZYRON.
        return content[:5]

    if content.startswith("1"):
        return "1"

    return "1"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix=get_prefix,
    intents=intents,
    help_command=None,
    case_insensitive=True
)

# ============================================================
# TICKETS
# ============================================================

TICKET_CATEGORY_NAME = "Tickets"
TICKET_TOPIC_PREFIX = "Zyron Ticket | Owner:"
VERIFIED_ROLE_IDS = {}  # guild_id -> role_id



# ============================================================
# ANTI-SPAM
# ============================================================

# Se guarda en memoria por servidor mientras Zyron esté encendido.
ANTI_SPAM_ENABLED = set()
SPAM_MESSAGES = defaultdict(deque)
SPAM_STRIKES = defaultdict(int)
SPAM_WINDOW = 5.0
SPAM_LIMIT = 4

# ============================================================
# AUTO-MOD
# ============================================================

AUTOMOD_ENABLED = set()
AUTOMOD_ACTION_COOLDOWN = {}
AUTOMOD_MENTION_LIMIT = 5
AUTOMOD_REPEAT_LIMIT = 6


# ============================================================
# HELPERS
# ============================================================

def result_embed(title: str, description: str, color=discord.Color.from_rgb(57, 255, 20), footer="Zyron"):
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=footer)
    embed.timestamp = discord.utils.utcnow()
    return embed


def error_embed(description: str):
    return result_embed("❌ Error", description, discord.Color.red(), "Zyron • Error")


def success_embed(title: str, description: str):
    return result_embed(title, description, discord.Color.green(), "Zyron • Sistema")


def parse_duration(value: str):
    """Convierte 30s, 10m, 2h o 3d a timedelta. Máximo: 28 días."""
    value = value.strip().lower()
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    if len(value) < 2 or value[-1] not in units:
        raise ValueError("Formato inválido")
    try:
        amount = int(value[:-1])
    except ValueError:
        raise ValueError("Formato inválido")
    if amount <= 0:
        raise ValueError("La duración debe ser mayor que 0")
    duration = timedelta(**{units[value[-1]]: amount})
    if duration > timedelta(days=28):
        raise ValueError("La duración máxima es de 28 días")
    return duration


def format_duration(duration: timedelta):
    total = int(duration.total_seconds())
    if total % 86400 == 0:
        return f"{total // 86400} día(s)"
    if total % 3600 == 0:
        return f"{total // 3600} hora(s)"
    if total % 60 == 0:
        return f"{total // 60} minuto(s)"
    return f"{total} segundo(s)"


def ban_embed(member: discord.Member, reason: str, moderator: discord.abc.User):
    embed = discord.Embed(
        title="🔨 Usuario baneado",
        description=f"**{member}** ha sido expulsado permanentemente del servidor.",
        color=discord.Color.red()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="👤 Usuario", value=f"{member.mention}\n`{member}`", inline=True)
    embed.add_field(name="🆔 ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="📝 Motivo", value=reason, inline=False)
    embed.add_field(name="🛡️ Moderador", value=f"{moderator.mention}", inline=True)
    embed.set_footer(text="Zyron • Sistema de moderación")
    embed.timestamp = discord.utils.utcnow()
    return embed


def antispam_key(guild_id: int, user_id: int):
    return (guild_id, user_id)


def clear_antispam_user(guild_id: int, user_id: int):
    key = antispam_key(guild_id, user_id)
    SPAM_MESSAGES.pop(key, None)


def antispam_record_message(guild_id: int, user_id: int) -> bool:
    """Devuelve True cuando el usuario alcanza 4 mensajes dentro de 5 segundos."""
    key = antispam_key(guild_id, user_id)
    now = discord.utils.utcnow().timestamp()
    timestamps = SPAM_MESSAGES[key]

    while timestamps and now - timestamps[0] >= SPAM_WINDOW:
        timestamps.popleft()

    timestamps.append(now)

    if len(timestamps) >= SPAM_LIMIT:
        timestamps.clear()
        return True

    return False


def automod_detect(message: discord.Message):
    """Devuelve el motivo de AutoMod o None."""
    content = message.content.strip()
    if not content:
        return None

    # Links y menciones masivas.
    lowered = content.lower()
    if "@everyone" in lowered or "@here" in lowered:
        return "Menciones masivas (@everyone/@here)"

    if len(message.mentions) >= AUTOMOD_MENTION_LIMIT:
        return f"Demasiadas menciones ({len(message.mentions)})"

    # Links comunes. Los enlaces se bloquean para reducir publicidad/spam.
    if "http://" in lowered or "https://" in lowered or "discord.gg/" in lowered:
        return "Enlace no permitido"

    # Flood de un mismo carácter, por ejemplo !!!!!!!! o ???????
    for char in "!?.-_~*#":
        if char * AUTOMOD_REPEAT_LIMIT in content:
            return "Flood de caracteres"

    return None


async def handle_automod(message: discord.Message):
    """Elimina mensajes que infringen las reglas automáticas."""
    if message.guild is None or message.author.bot:
        return False
    if message.guild.id not in AUTOMOD_ENABLED:
        return False

    reason = automod_detect(message)
    if not reason:
        return False

    try:
        await message.delete()
    except (discord.Forbidden, discord.HTTPException):
        # Si no puede borrar, no bloqueamos los demás comandos.
        return False

    embed = discord.Embed(
        title="🛡️ AutoMod",
        description=f"{message.author.mention}, tu mensaje fue eliminado automáticamente.",
        color=discord.Color.orange()
    )
    embed.add_field(name="⚠️ Motivo", value=reason, inline=False)
    embed.set_thumbnail(url=message.author.display_avatar.url)
    embed.set_footer(text="Zyron • AutoMod")
    embed.timestamp = discord.utils.utcnow()

    try:
        warning = await message.channel.send(embed=embed)
        await warning.delete(delay=5)
    except discord.HTTPException:
        pass

    return True


async def handle_antispam(message: discord.Message):
    """Procesa el anti-spam y devuelve True si el mensaje ya fue gestionado."""
    if message.guild is None or message.author.bot:
        return False
    if message.guild.id not in ANTI_SPAM_ENABLED:
        return False

    triggered = antispam_record_message(message.guild.id, message.author.id)
    if not triggered:
        return False

    key = antispam_key(message.guild.id, message.author.id)
    SPAM_STRIKES[key] += 1
    strikes = SPAM_STRIKES[key]
    member = message.author

    if strikes < 3:
        embed = discord.Embed(
            title="⚠️ Anti-Spam",
            description=(
                f"{member.mention} eh llevas **{strikes} de 3** por spam.\n\n"
                "A la **3** serás muteado durante **1 minuto**."
            ),
            color=discord.Color.orange()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="Zyron • Sistema Anti-Spam")
        embed.timestamp = discord.utils.utcnow()
        await message.channel.send(embed=embed)
        return True

    try:
        await member.timeout(timedelta(minutes=1), reason="Anti-Spam: 3 advertencias por spam")
        embed = discord.Embed(
            title="🔇 Anti-Spam — Usuario silenciado",
            description=f"{member.mention} recibió **3 de 3** advertencias por spam y fue silenciado durante **1 minuto**.",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="⚠️ Motivo", value="Spam: 4 mensajes en menos de 5 segundos.", inline=False)
        embed.add_field(name="⏱️ Duración", value="1 minuto", inline=True)
        embed.set_footer(text="Zyron • Sistema Anti-Spam")
        embed.timestamp = discord.utils.utcnow()
        await message.channel.send(embed=embed)
        SPAM_STRIKES.pop(key, None)
    except discord.Forbidden:
        embed = error_embed(f"No pude silenciar a {member.mention} por 1 minuto. Necesito el permiso **Moderate Members** y mi rol debe estar por encima del usuario.")
        await message.channel.send(embed=embed)
    except discord.HTTPException:
        await message.channel.send(embed=error_embed("Discord no pudo aplicar el mute anti-spam. Inténtalo de nuevo."))

    return True


# ============================================================
# SLASH COMMANDS
# ============================================================

@bot.tree.command(name="ban", description="Banea a un miembro del servidor")
@app_commands.checks.has_permissions(ban_members=True)
@app_commands.describe(member="Miembro que quieres banear", reason="Motivo del ban")
async def slash_ban(interaction: discord.Interaction, member: discord.Member, reason: str = "Sin motivo"):
    if member == interaction.user:
        await interaction.response.send_message(embed=error_embed("No puedes banearte a ti mismo."), ephemeral=True)
        return
    if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
        await interaction.response.send_message(embed=error_embed("No puedes banear a un miembro con un rol igual o superior al tuyo."), ephemeral=True)
        return
    try:
        await member.ban(reason=f"{reason} | Por: {interaction.user}")
        await interaction.response.send_message(embed=ban_embed(member, reason, interaction.user))
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("No tengo permisos para banear a ese miembro."), ephemeral=True)


@bot.tree.command(name="kick", description="Expulsa a un miembro del servidor")
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.describe(member="Miembro que quieres expulsar", reason="Motivo de la expulsión")
async def slash_kick(interaction: discord.Interaction, member: discord.Member, reason: str = "Sin motivo"):
    if member == interaction.user:
        await interaction.response.send_message(embed=error_embed("No puedes expulsarte a ti mismo."), ephemeral=True)
        return
    if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
        await interaction.response.send_message(embed=error_embed("No puedes expulsar a un miembro con un rol igual o superior al tuyo."), ephemeral=True)
        return
    try:
        await member.kick(reason=f"{reason} | Por: {interaction.user}")
        embed = discord.Embed(title="👢 Usuario expulsado", description=f"**{member}** fue expulsado del servidor.", color=discord.Color.orange())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="📝 Motivo", value=reason, inline=False)
        embed.add_field(name="🛡️ Moderador", value=interaction.user.mention, inline=True)
        embed.set_footer(text="Zyron • Sistema de moderación")
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("No tengo permisos para expulsar a ese miembro."), ephemeral=True)


@bot.tree.command(name="mute", description="Silencia a un miembro por el tiempo que elijas")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(
    member="Miembro que quieres silenciar",
    duration="Duración: 30s, 10m, 2h o 3d (máximo 28d)",
    reason="Motivo del mute"
)
async def slash_mute(interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = "Sin motivo"):
    if member == interaction.user:
        await interaction.response.send_message(embed=error_embed("No puedes silenciarte a ti mismo."), ephemeral=True)
        return
    if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
        await interaction.response.send_message(embed=error_embed("No puedes silenciar a un miembro con un rol igual o superior al tuyo."), ephemeral=True)
        return
    try:
        delta = parse_duration(duration)
    except ValueError as exc:
        await interaction.response.send_message(embed=error_embed(f"❌ {exc}. Usa por ejemplo `10m`, `2h` o `3d` (máximo `28d`)."), ephemeral=True)
        return
    try:
        await member.timeout(delta, reason=f"{reason} | Por: {interaction.user}")
        embed = discord.Embed(title="🔇 Usuario silenciado", description=f"**{member}** fue silenciado.", color=discord.Color.dark_gray())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="📝 Motivo", value=reason, inline=False)
        embed.add_field(name="⏱️ Duración", value=format_duration(delta), inline=True)
        embed.add_field(name="🛡️ Moderador", value=interaction.user.mention, inline=True)
        embed.set_footer(text="Zyron • Sistema de moderación")
        embed.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embed=embed)
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("No tengo permisos para silenciar a ese miembro."), ephemeral=True)


@bot.tree.command(name="unmute", description="Quita el mute de un miembro")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(member="Miembro al que quieres quitar el mute")
async def slash_unmute(interaction: discord.Interaction, member: discord.Member):
    try:
        await member.timeout(None, reason=f"Unmute por: {interaction.user}")
        await interaction.response.send_message(embed=success_embed("🔊 Mute retirado", f"Se quitó el mute a **{member}**."))
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("No tengo permisos para quitar el mute."), ephemeral=True)


@bot.tree.command(name="lock", description="Bloquea un rol en el canal actual")
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.describe(role="Rol que no podrá hablar en este canal")
async def slash_lock(interaction: discord.Interaction, role: discord.Role):
    try:
        overwrite = interaction.channel.overwrites_for(role)
        overwrite.send_messages = False
        await interaction.channel.set_permissions(role, overwrite=overwrite, reason=f"Canal bloqueado para {role.name} por: {interaction.user}")
        await interaction.response.send_message(embed=success_embed("🔒 Canal bloqueado", f"El rol {role.mention} ya no puede hablar en este canal."))
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("No tengo permisos para modificar este canal."), ephemeral=True)
    except discord.HTTPException:
        await interaction.response.send_message(embed=error_embed("Discord no pudo modificar los permisos del canal."), ephemeral=True)


@bot.tree.command(name="unlock", description="Desbloquea un rol en el canal actual")
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.describe(role="Rol que podrá volver a hablar en este canal")
async def slash_unlock(interaction: discord.Interaction, role: discord.Role):
    try:
        overwrite = interaction.channel.overwrites_for(role)
        overwrite.send_messages = None
        await interaction.channel.set_permissions(role, overwrite=overwrite, reason=f"Canal desbloqueado para {role.name} por: {interaction.user}")
        await interaction.response.send_message(embed=success_embed("🔓 Canal desbloqueado", f"El rol {role.mention} puede volver a hablar en este canal."))
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("No tengo permisos para modificar este canal."), ephemeral=True)
    except discord.HTTPException:
        await interaction.response.send_message(embed=error_embed("Discord no pudo modificar los permisos del canal."), ephemeral=True)


@bot.tree.command(name="lockall", description="Bloquea un rol en todos los canales")
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.describe(role="Rol que no podrá hablar en los canales")
async def slash_lockall(interaction: discord.Interaction, role: discord.Role):
    if interaction.guild is None:
        await interaction.response.send_message(embed=error_embed("Este comando solo puede usarse dentro de un servidor."), ephemeral=True)
        return
    await interaction.response.defer()
    locked = failed = 0
    for channel in interaction.guild.channels:
        try:
            overwrite = channel.overwrites_for(role)
            overwrite.send_messages = False
            await channel.set_permissions(role, overwrite=overwrite, reason=f"Lockall de {role.name} por: {interaction.user}")
            locked += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
    extra = f"\n⚠️ No pude modificar **{failed}** canales." if failed else ""
    await interaction.followup.send(embed=success_embed("🔒 Lock All", f"El rol {role.mention} fue bloqueado en **{locked} canales**. Ya no podrá hablar en ellos.{extra}"))


@bot.tree.command(name="unlockall", description="Desbloquea un rol en todos los canales")
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.describe(role="Rol que podrá volver a hablar en los canales")
async def slash_unlockall(interaction: discord.Interaction, role: discord.Role):
    if interaction.guild is None:
        await interaction.response.send_message(embed=error_embed("Este comando solo puede usarse dentro de un servidor."), ephemeral=True)
        return
    await interaction.response.defer()
    unlocked = failed = 0
    for channel in interaction.guild.channels:
        try:
            overwrite = channel.overwrites_for(role)
            overwrite.send_messages = None
            await channel.set_permissions(role, overwrite=overwrite, reason=f"Unlockall de {role.name} por: {interaction.user}")
            unlocked += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
    extra = f"\n⚠️ No pude modificar **{failed}** canales." if failed else ""
    await interaction.followup.send(embed=success_embed("🔓 Unlock All", f"El rol {role.mention} fue desbloqueado en **{unlocked} canales**. Ya puede hablar nuevamente.{extra}"))


@bot.tree.command(name="adderole", description="Da un rol a un miembro")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(member="Miembro", role="Rol que quieres dar")
async def slash_adderole(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(embed=error_embed("No puedo administrar ese rol porque está por encima de mi rol."), ephemeral=True)
        return
    try:
        await member.add_roles(role, reason=f"Rol añadido por: {interaction.user}")
        await interaction.response.send_message(embed=success_embed("➕ Rol añadido", f"Se dio el rol **{role.name}** a **{member}**."))
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("No puedo administrar ese rol."), ephemeral=True)


@bot.tree.command(name="removerole", description="Quita un rol a un miembro")
@app_commands.checks.has_permissions(manage_roles=True)
@app_commands.describe(member="Miembro", role="Rol que quieres quitar")
async def slash_removerole(interaction: discord.Interaction, member: discord.Member, role: discord.Role):
    if role >= interaction.guild.me.top_role:
        await interaction.response.send_message(embed=error_embed("No puedo administrar ese rol porque está por encima de mi rol."), ephemeral=True)
        return
    try:
        await member.remove_roles(role, reason=f"Rol quitado por: {interaction.user}")
        await interaction.response.send_message(embed=success_embed("➖ Rol retirado", f"Se quitó el rol **{role.name}** a **{member}**."))
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("No puedo administrar ese rol."), ephemeral=True)


@bot.tree.command(name="clear", description="Borra una cantidad de mensajes del canal")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(cantidad="Cantidad de mensajes a borrar (1-100)")
async def slash_clear(interaction: discord.Interaction, cantidad: app_commands.Range[int, 1, 100]):
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=cantidad)
        await interaction.followup.send(embed=success_embed("🧹 Mensajes eliminados", f"Se borraron **{len(deleted)}** mensajes."), ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(embed=error_embed("No tengo permisos para borrar mensajes."), ephemeral=True)
    except discord.HTTPException:
        await interaction.followup.send(embed=error_embed("Discord no pudo borrar los mensajes. Intenta con una cantidad menor."), ephemeral=True)


@bot.tree.command(name="purge", description="Borra todos los mensajes del canal")
@app_commands.checks.has_permissions(manage_messages=True)
async def slash_purge(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=None)
        await interaction.followup.send(
            embed=success_embed(
                "🧹 Canal limpiado",
                f"Se eliminaron **{len(deleted)} mensajes** de este canal."
            ),
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send(embed=error_embed("No tengo permisos para borrar los mensajes de este canal."), ephemeral=True)
    except (discord.HTTPException, AttributeError):
        await interaction.followup.send(embed=error_embed("Discord no pudo borrar todos los mensajes de este canal."), ephemeral=True)


@bot.tree.command(name="purgeuser", description="Borra todos los mensajes de un miembro en este canal")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(member="Miembro cuyos mensajes quieres borrar")
async def slash_purgeuser(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(
            limit=None,
            check=lambda message: message.author.id == member.id
        )
        await interaction.followup.send(
            embed=success_embed(
                "🧹 Mensajes del usuario eliminados",
                f"Se eliminaron **{len(deleted)} mensajes** de {member.mention} en este canal."
            ),
            ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send(embed=error_embed("No tengo permisos para borrar los mensajes de este canal."), ephemeral=True)
    except (discord.HTTPException, AttributeError):
        await interaction.followup.send(embed=error_embed("Discord no pudo completar la limpieza de los mensajes."), ephemeral=True)


@bot.tree.command(name="say", description="Hace que el bot envíe un mensaje")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(mensaje="Mensaje que quieres que envíe el bot")
async def slash_say(interaction: discord.Interaction, mensaje: str):
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.channel.send(mensaje)
        await interaction.delete_original_response()
    except discord.Forbidden:
        await interaction.followup.send("❌ No puedo enviar mensajes en este canal.", ephemeral=True)


@bot.tree.command(name="warn", description="Advierte a un miembro del servidor")
@app_commands.checks.has_permissions(moderate_members=True)
@app_commands.describe(member="Miembro al que quieres advertir", reason="Motivo de la advertencia")
async def slash_warn(interaction: discord.Interaction, member: discord.Member, reason: str = "Sin motivo"):
    if member == interaction.user:
        await interaction.response.send_message(embed=error_embed("No puedes advertirte a ti mismo."), ephemeral=True)
        return
    if member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
        await interaction.response.send_message(embed=error_embed("No puedes advertir a un miembro con un rol igual o superior al tuyo."), ephemeral=True)
        return
    embed = discord.Embed(
        title="⚠️ Advertencia emitida",
        description=f"**{member.mention}** recibió una advertencia.",
        color=discord.Color.orange()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="📝 Motivo", value=reason, inline=False)
    embed.add_field(name="👤 Usuario", value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name="🛡️ Moderador", value=interaction.user.mention, inline=True)
    embed.set_footer(text="Zyron • Sistema de moderación")
    embed.timestamp = discord.utils.utcnow()
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nick", description="Cambia el apodo de un miembro")
@app_commands.checks.has_permissions(manage_nicknames=True)
@app_commands.describe(member="Miembro al que quieres cambiar el apodo", apodo="Nuevo apodo; déjalo vacío para quitarlo")
async def slash_nick(interaction: discord.Interaction, member: discord.Member, apodo: str = None):
    if member == interaction.guild.owner or member.top_role >= interaction.user.top_role and interaction.user != interaction.guild.owner:
        await interaction.response.send_message(embed=error_embed("No puedes cambiar el apodo de un miembro con un rol igual o superior al tuyo."), ephemeral=True)
        return
    if member.top_role >= interaction.guild.me.top_role:
        await interaction.response.send_message(embed=error_embed("Mi rol debe estar por encima del miembro para cambiar su apodo."), ephemeral=True)
        return
    if apodo is not None and len(apodo) > 32:
        await interaction.response.send_message(embed=error_embed("El apodo no puede superar los 32 caracteres."), ephemeral=True)
        return
    try:
        await member.edit(nick=apodo, reason=f"Nick cambiado por {interaction.user}")
        texto = f"Se cambió el apodo de {member.mention} a **{discord.utils.escape_markdown(apodo)}**." if apodo else f"Se quitó el apodo de {member.mention}."
        await interaction.response.send_message(embed=success_embed("🏷️ Apodo actualizado", texto))
    except discord.Forbidden:
        await interaction.response.send_message(embed=error_embed("No tengo permiso para cambiar ese apodo."), ephemeral=True)
    except discord.HTTPException:
        await interaction.response.send_message(embed=error_embed("Discord no pudo cambiar el apodo."), ephemeral=True)


@bot.tree.command(name="avatar", description="Muestra el avatar de un miembro")
@app_commands.describe(member="Miembro cuyo avatar quieres ver")
async def slash_avatar(interaction: discord.Interaction, member: discord.Member):
    embed = discord.Embed(
        title=f"🖼️ Avatar de {member}",
        color=discord.Color.from_rgb(57, 255, 20)
    )
    embed.set_image(url=member.display_avatar.url)
    embed.set_footer(text=f"Zyron • ID: {member.id}")
    await interaction.response.send_message(embed=embed)


def build_roles_embed(guild: discord.Guild):
    roles = list(reversed(guild.roles[1:]))  # omite @everyone y muestra los más altos primero
    embed = discord.Embed(
        title=f"🎭 Roles de {guild.name}",
        description=f"Este servidor tiene **{len(roles)} roles**.",
        color=discord.Color.from_rgb(57, 255, 20)
    )
    if not roles:
        embed.add_field(name="Roles", value="No hay roles personalizados.", inline=False)
        return embed

    role_lines = [role.mention for role in roles]
    text = "\n".join(role_lines)
    if len(text) <= 3900:
        embed.add_field(name="📋 Lista de roles", value=text, inline=False)
    else:
        # Mantiene el embed dentro del límite de Discord si hay muchísimos roles.
        visible = []
        length = 0
        for line in role_lines:
            extra = len(line) + (1 if visible else 0)
            if length + extra > 3900:
                break
            visible.append(line)
            length += extra
        restantes = len(roles) - len(visible)
        value = "\n".join(visible) + f"\n\n… y **{restantes}** roles más."
        embed.add_field(name="📋 Lista de roles", value=value, inline=False)

    embed.set_footer(text="Zyron • Roles del servidor")
    embed.timestamp = discord.utils.utcnow()
    return embed


@bot.tree.command(name="roles", description="Muestra los roles del servidor")
async def slash_roles(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(embed=error_embed("Este comando solo puede usarse dentro de un servidor."), ephemeral=True)
        return
    await interaction.response.send_message(embed=build_roles_embed(interaction.guild))


# ============================================================
# FUN + USER INFO
# ============================================================

@bot.tree.command(name="userinfo", description="Muestra información de un miembro")
@app_commands.describe(member="Miembro del que quieres ver la información")
async def slash_userinfo(interaction: discord.Interaction, member: discord.Member):
    embed = discord.Embed(title=f"👤 Información de {member}", color=discord.Color.from_rgb(57, 255, 20))
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🏷️ Usuario", value=f"{member.mention}\n`{member}`", inline=True)
    embed.add_field(name="🆔 ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="🎭 Rol más alto", value=member.top_role.mention, inline=True)
    embed.add_field(name="📅 Cuenta creada", value=discord.utils.format_dt(member.created_at, style="F"), inline=False)
    embed.add_field(name="📥 Entró al servidor", value=discord.utils.format_dt(member.joined_at, style="F") if member.joined_at else "Desconocido", inline=False)
    embed.set_footer(text="Zyron • Información de usuario")
    embed.timestamp = discord.utils.utcnow()
    await interaction.response.send_message(embed=embed)


# ============================================================
# SERVER INFO
# ============================================================

def build_server_embed(guild: discord.Guild):
    embed = discord.Embed(
        title=f"🖥️ {guild.name}",
        description="Información del servidor",
        color=discord.Color.from_rgb(57, 255, 20)
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)

    owner = guild.owner
    owner_text = f"{owner.mention}\n`{owner}`" if owner else f"`{guild.owner_id}`"
    text_channels = len(guild.text_channels)
    voice_channels = len(guild.voice_channels)
    categories = len(guild.categories)

    embed.add_field(name="👑 OWNER", value=owner_text, inline=True)
    embed.add_field(name="🆔 ID del servidor", value=f"`{guild.id}`", inline=True)
    embed.add_field(name="👥 Miembros", value=f"`{guild.member_count or 0}`", inline=True)
    embed.add_field(name="📚 Canales", value=f"`{len(guild.channels)}` total\n💬 `{text_channels}` texto • 🔊 `{voice_channels}` voz", inline=True)
    embed.add_field(name="📁 Categorías", value=f"`{categories}`", inline=True)
    embed.add_field(name="🎭 Roles", value=f"`{max(0, len(guild.roles) - 1)}`", inline=True)
    embed.add_field(name="📅 Creado", value=discord.utils.format_dt(guild.created_at, style="F"), inline=False)
    embed.set_footer(text="Zyron • Información del servidor")
    embed.timestamp = discord.utils.utcnow()
    return embed


@bot.tree.command(name="server", description="Muestra información del servidor")
async def slash_server(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=error_embed("Este comando solo puede usarse dentro de un servidor."),
            ephemeral=True
        )
        return
    await interaction.response.send_message(embed=build_server_embed(interaction.guild))



# ============================================================
# IA — ASK
# ============================================================

async def ask_ai(prompt: str):
    if not OPENAI_API_KEY:
        return None, "Falta configurar la variable `OPENAI_API_KEY` en Temalix."

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "system",
                "content": (
                    "Eres Zyron, un asistente de Discord amigable, útil y breve. "
                    "Responde en español salvo que el usuario pida otro idioma. "
                    "No inventes datos cuando no estés seguro."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "max_output_tokens": 700
    }

    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=30
        )

        if response.status_code != 200:
            print("OpenAI API:", response.status_code, response.text[:1000])
            return None, "La IA no pudo responder ahora mismo."

        data = response.json()
        answer = data.get("output_text")

        if not answer:
            # Fallback por si la respuesta viene en la estructura de salida.
            parts = []
            for item in data.get("output", []):
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        parts.append(content.get("text", ""))
            answer = "\n".join(parts).strip()

        if not answer:
            return None, "La IA no devolvió una respuesta."

        return answer[:4000], None

    except requests.Timeout:
        return None, "La IA tardó demasiado en responder. Inténtalo otra vez."
    except requests.RequestException as exc:
        print("Error de conexión con OpenAI:", exc)
        return None, "No pude conectar con la IA."
    except Exception as exc:
        print("Error de IA:", exc)
        return None, "Ocurrió un error al procesar la pregunta."


def ai_embed(question: str, answer: str):
    embed = discord.Embed(
        title="🤖 Zyron AI",
        description=answer,
        color=discord.Color.from_rgb(57, 255, 20)
    )
    embed.add_field(name="❓ Pregunta", value=question[:1024], inline=False)
    embed.set_footer(text="Zyron • Inteligencia artificial")
    embed.timestamp = discord.utils.utcnow()
    return embed


@bot.tree.command(name="ask", description="Hazle una pregunta a la IA de Zyron")
@app_commands.describe(pregunta="Lo que quieres preguntarle a Zyron")
async def slash_ask(interaction: discord.Interaction, pregunta: str):
    await interaction.response.defer()
    answer, error = await ask_ai(pregunta)

    if error:
        await interaction.followup.send(embed=error_embed(error), ephemeral=True)
        return

    await interaction.followup.send(embed=ai_embed(pregunta, answer))


@bot.tree.command(name="antispam", description="Activa el sistema anti-spam del servidor")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_antispam(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(embed=error_embed("Este comando solo puede usarse dentro de un servidor."), ephemeral=True)
        return

    guild_id = interaction.guild.id
    if guild_id in ANTI_SPAM_ENABLED:
        await interaction.response.send_message(embed=success_embed("🛡️ Anti-Spam activo", "El sistema anti-spam ya estaba activado en este servidor."), ephemeral=True)
        return

    ANTI_SPAM_ENABLED.add(guild_id)
    await interaction.response.send_message(
        embed=success_embed(
            "🛡️ Anti-Spam activado",
            "Zyron detectará **4 mensajes en menos de 5 segundos**.\n\n"
            "⚠️ A la 1.ª y 2.ª detección dará una advertencia.\n"
            "🔇 A la 3.ª detección aplicará **mute de 1 minuto**."
        )
    )


@bot.tree.command(name="unantispam", description="Desactiva el sistema anti-spam del servidor")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_unantispam(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(embed=error_embed("Este comando solo puede usarse dentro de un servidor."), ephemeral=True)
        return

    ANTI_SPAM_ENABLED.discard(interaction.guild.id)
    # Limpia los contadores de este servidor al desactivarlo.
    for key in list(SPAM_MESSAGES):
        if key[0] == interaction.guild.id:
            SPAM_MESSAGES.pop(key, None)
            SPAM_STRIKES.pop(key, None)

    await interaction.response.send_message(embed=success_embed("🛡️ Anti-Spam desactivado", "El sistema anti-spam ya no está activo en este servidor."))


@bot.tree.command(name="automod", description="Activa, desactiva o consulta el AutoMod")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(accion="Qué quieres hacer con AutoMod")
@app_commands.choices(accion=[
    app_commands.Choice(name="Activar", value="activar"),
    app_commands.Choice(name="Desactivar", value="desactivar"),
    app_commands.Choice(name="Estado", value="estado"),
])
async def slash_automod(interaction: discord.Interaction, accion: app_commands.Choice[str]):
    if interaction.guild is None:
        await interaction.response.send_message(embed=error_embed("Este comando solo puede usarse dentro de un servidor."), ephemeral=True)
        return

    guild_id = interaction.guild.id
    if accion.value == "activar":
        AUTOMOD_ENABLED.add(guild_id)
        texto = (
            "AutoMod quedó **activado**.\n\n"
            "🔗 Bloquea enlaces.\n"
            "📢 Bloquea @everyone/@here y exceso de menciones.\n"
            "🧹 Detecta flood de caracteres.\n"
            "⚠️ Los mensajes detectados se eliminan automáticamente."
        )
        await interaction.response.send_message(embed=success_embed("🛡️ AutoMod activado", texto))
    elif accion.value == "desactivar":
        AUTOMOD_ENABLED.discard(guild_id)
        await interaction.response.send_message(embed=success_embed("🛡️ AutoMod desactivado", "Zyron dejará de revisar automáticamente los mensajes."))
    else:
        estado = "🟢 Activado" if guild_id in AUTOMOD_ENABLED else "🔴 Desactivado"
        embed = discord.Embed(title="🛡️ Estado de AutoMod", description=f"Estado actual: **{estado}**", color=discord.Color.from_rgb(57, 255, 20))
        embed.add_field(name="🔗 Enlaces", value="Bloqueados", inline=True)
        embed.add_field(name="📢 Menciones", value=f"Máximo {AUTOMOD_MENTION_LIMIT}", inline=True)
        embed.add_field(name="🧹 Flood", value=f"Detecta {AUTOMOD_REPEAT_LIMIT}+ caracteres repetidos", inline=True)
        embed.set_footer(text="Zyron • AutoMod")
        await interaction.response.send_message(embed=embed)


# ============================================================
# VERIFIED
# ============================================================

class VerifiedView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="CLICK FOR VERIFIED", style=discord.ButtonStyle.green, emoji="✅", custom_id="zyron:verified")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(embed=error_embed("Este botón solo puede usarse dentro de un servidor."), ephemeral=True)
            return
        role_id = VERIFIED_ROLE_IDS.get(interaction.guild.id)
        role = interaction.guild.get_role(role_id) if role_id else None
        if role is None:
            await interaction.response.send_message(embed=error_embed("La verificación no está configurada. Usa `/verified @rol`."), ephemeral=True)
            return
        member = interaction.user
        me = interaction.guild.me
        if role in member.roles:
            await interaction.response.send_message(embed=success_embed("✅ Ya estás verificado", f"Ya tienes el rol {role.mention}."), ephemeral=True)
            return
        if me is None or not me.guild_permissions.manage_roles:
            await interaction.response.send_message(embed=error_embed("Necesito el permiso **Gestionar roles**."), ephemeral=True)
            return
        if role.is_default() or role.managed or role >= me.top_role:
            await interaction.response.send_message(embed=error_embed(f"No puedo asignar {role.mention}. Revisa la posición de mi rol y sus permisos."), ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="Verified mediante botón de Zyron")
            await interaction.response.send_message(embed=success_embed("✅ Verificación completada", f"{member.mention}, recibiste el rol {role.mention}."), ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(embed=error_embed("Discord no me permite asignarte ese rol. Revisa **Gestionar roles** y la posición de mi rol."), ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message(embed=error_embed("Discord no pudo asignarte el rol. Inténtalo de nuevo."), ephemeral=True)


@bot.tree.command(name="verified", description="Crea un panel para verificar miembros")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(role="Rol que Zyron dará al verificar")
async def slash_verified(interaction: discord.Interaction, role: discord.Role):
    if interaction.guild is None:
        await interaction.response.send_message(embed=error_embed("Este comando solo puede usarse dentro de un servidor."), ephemeral=True)
        return
    me = interaction.guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await interaction.response.send_message(embed=error_embed("Necesito el permiso **Gestionar roles**."), ephemeral=True)
        return
    if role.is_default() or role.managed or role >= me.top_role:
        await interaction.response.send_message(embed=error_embed(f"No puedo administrar {role.mention}. Revisa la posición de mi rol."), ephemeral=True)
        return
    VERIFIED_ROLE_IDS[interaction.guild.id] = role.id
    embed=discord.Embed(title="🛡️ Verificación", description=f"Pulsa el botón de abajo para verificarte.\n\nRecibirás automáticamente el rol {role.mention}.", color=discord.Color.green())
    embed.set_footer(text="Zyron • Sistema de verificación")
    embed.timestamp=discord.utils.utcnow()
    await interaction.response.send_message(embed=embed, view=VerifiedView())


# ============================================================
# TICKETS
# ============================================================

def get_ticket_owner(channel: discord.TextChannel):
    topic = channel.topic or ""
    if not topic.startswith(TICKET_TOPIC_PREFIX):
        return None
    try:
        return int(topic.split(":", 1)[1].strip())
    except (ValueError, IndexError):
        return None


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Crear ticket",
        style=discord.ButtonStyle.green,
        emoji="🎫",
        custom_id="zyron:create_ticket"
    )
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("Este botón solo puede usarse dentro de un servidor."),
                ephemeral=True
            )
            return

        guild = interaction.guild
        member = interaction.user

        # Evita que un usuario cree varios tickets abiertos.
        existing = discord.utils.find(
            lambda c: isinstance(c, discord.TextChannel) and get_ticket_owner(c) == member.id,
            guild.text_channels
        )
        if existing:
            await interaction.response.send_message(
                embed=error_embed(f"Ya tienes un ticket abierto: {existing.mention}"),
                ephemeral=True
            )
            return

        category = discord.utils.get(guild.categories, name=TICKET_CATEGORY_NAME)
        try:
            if category is None:
                category = await guild.create_category(
                    TICKET_CATEGORY_NAME,
                    reason=f"Categoría de tickets creada por Zyron para {member}"
                )

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    attach_files=True,
                    embed_links=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    manage_channels=True,
                    manage_messages=True,
                    attach_files=True,
                    embed_links=True
                )
            }

            channel = await guild.create_text_channel(
                name=f"ticket-{member.name.lower().replace(' ', '-')[:80]}",
                category=category,
                overwrites=overwrites,
                topic=f"{TICKET_TOPIC_PREFIX} {member.id}",
                reason=f"Ticket creado por {member}"
            )

            embed = discord.Embed(
                title="🎫 Ticket abierto",
                description=(
                    f"Hola {member.mention}, tu ticket está listo.\n\n"
                    "📝 Escribe aquí lo que necesitas y el equipo podrá ayudarte.\n"
                    "🔐 El canal es privado y puedes modificar manualmente sus permisos.\n"
                    "🔒 Cuando termines, usa **Cerrar ticket** para eliminar este canal."
                ),
                color=discord.Color.from_rgb(57, 255, 20)
            )
            embed.add_field(name="👤 Creador", value=member.mention, inline=True)
            embed.add_field(name="🆔 Canal", value=f"`{channel.id}`", inline=True)
            embed.set_footer(text="Zyron • Sistema de tickets")
            embed.timestamp = discord.utils.utcnow()

            await channel.send(content=member.mention, embed=embed, view=TicketCloseView())
            await interaction.response.send_message(
                embed=success_embed("🎫 Ticket creado", f"Tu ticket está aquí: {channel.mention}"),
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                embed=error_embed("No tengo permisos para crear la categoría o el canal del ticket."),
                ephemeral=True
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=error_embed("No pude crear el ticket. Inténtalo de nuevo."),
                ephemeral=True
            )


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Cerrar ticket",
        style=discord.ButtonStyle.red,
        emoji="🔒",
        custom_id="zyron:close_ticket"
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=error_embed("Este botón solo funciona dentro de un ticket."),
                ephemeral=True
            )
            return

        owner_id = get_ticket_owner(channel)
        if owner_id is None:
            await interaction.response.send_message(
                embed=error_embed("Este canal no parece ser un ticket de Zyron."),
                ephemeral=True
            )
            return

        is_owner = interaction.user.id == owner_id
        is_staff = isinstance(interaction.user, discord.Member) and (
            interaction.user.guild_permissions.manage_channels or
            interaction.user.guild_permissions.administrator
        )
        if not is_owner and not is_staff:
            await interaction.response.send_message(
                embed=error_embed("Solo el creador del ticket o un moderador con permisos puede cerrarlo."),
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            embed=success_embed("🔒 Cerrando ticket", "El canal se eliminará en unos segundos.")
        )
        try:
            await channel.delete(reason=f"Ticket cerrado por {interaction.user}")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass


@bot.tree.command(name="panel", description="Crea un panel para abrir tickets")
@app_commands.checks.has_permissions(manage_guild=True)
async def slash_panel(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            embed=error_embed("Este comando solo puede usarse dentro de un servidor."),
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🎫 Soporte de Zyron",
        description=(
            "¿Necesitas ayuda?\n\n"
            "Pulsa **Crear ticket** para abrir un canal privado con el equipo.\n"
            "Puedes modificar los permisos del canal una vez creado.\n\n"
            "🔒 Para cerrar el ticket, usa el botón **Cerrar ticket** dentro del canal."
        ),
        color=discord.Color.from_rgb(57, 255, 20)
    )
    embed.set_footer(text="Zyron • Sistema de tickets")
    embed.timestamp = discord.utils.utcnow()

    await interaction.response.send_message(embed=embed, view=TicketView())


# ============================================================
# HELP EMBED
# ============================================================

def build_help_embed():
    embed = discord.Embed(
        title="🟢 ZYRON — AYUDA",
        description="Todos los comandos disponibles de Zyron, organizados por categoría.",
        color=discord.Color.from_rgb(57, 255, 20)
    )
    embed.add_field(name="🛡️ Moderación", value=(
        "`ban` — Banea a un usuario.\n"
        "`kick` — Expulsa al usuario mencionado del servidor.\n"
        "`mute` — Silencia a un usuario por una duración.\n"
        "`unmute` — Quita el mute.\n"
        "`lock @rol` — Impide que un rol hable en este canal.\n"
        "`unlock @rol` — Permite que un rol vuelva a hablar en este canal.\n"
        "`automod` — Activa, desactiva o consulta el AutoMod.\n"
        "`antispam` — Activa el anti-spam.\n"
        "`unantispam` — Desactiva el anti-spam.\n"
        "`warn` — Advierte a un usuario.\n"
        "`clear` — Borra una cantidad de mensajes.\n"
        "`purge` — Borra los mensajes del canal.\n"
        "`purgeuser` — Borra los mensajes de un usuario."
    ), inline=False)
    embed.add_field(name="🔧 Utilidad", value=(
        "`server` — Muestra información del servidor.\n"
        "`avatar` — Muestra el avatar de un usuario.\n"
        "`roles` — Muestra los roles del servidor.\n"
        "`adderole @usuario @rol` — Añade un rol.\n"
        "`removerole @usuario @rol` — Quita un rol.\n"
        "`nick @usuario` — Cambia el nickname.\n"
        "`say` — Envía un mensaje mediante Zyron.\n"
        "`userinfo` — Muestra información de un usuario.\n"
        "`ask` — Pregunta a Zyron AI."
    ), inline=False)
    embed.add_field(name="✨ Extras", value=(
        "`panel` — Crea el panel de tickets.\n"
        "`verified @rol` — Crea el panel de verificación.\n"
        "`lockall @rol` — Bloquea ese rol en todos los canales.\n"
        "`unlockall @rol` — Desbloquea ese rol en todos los canales."
    ), inline=False)
    embed.add_field(name="📌 Formas de usar los comandos", value="`/comando`  •  `1comando`  •  `Zyron comando`", inline=False)
    embed.set_footer(text="Zyron • Sistema de ayuda")
    embed.timestamp = discord.utils.utcnow()
    return embed

@bot.tree.command(name="help", description="Muestra todos los comandos del bot")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_help_embed())



@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # AutoMod se procesa antes de los comandos.
    if await handle_automod(message):
        return

    # El anti-spam se procesa antes de los comandos para contar cualquier mensaje.
    if await handle_antispam(message):
        return

    if bot.user and message.reference and message.reference.message_id:
        try:
            referenced = message.reference.resolved
            if referenced is None:
                referenced = await message.channel.fetch_message(message.reference.message_id)

            if referenced.author.id == bot.user.id:
                prompt = message.content.strip()
                if bot.user.mention:
                    prompt = prompt.replace(bot.user.mention, "").strip()

                if prompt:
                    async with message.channel.typing():
                        answer, error = await ask_ai(prompt)

                    if error:
                        await message.reply(embed=error_embed(error), mention_author=False)
                    else:
                        await message.reply(
                            embed=ai_embed(prompt, answer),
                            mention_author=False
                        )
                    return
        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
            pass

    await bot.process_commands(message)


# ============================================================
# ERROR HANDLER — SLASH
# ============================================================

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        permissions = ", ".join(error.missing_permissions)
        embed = error_embed(f"No tienes los permisos necesarios: `{permissions}`.")
    else:
        print(f"Error en slash command: {error}")
        embed = error_embed("Ocurrió un error al ejecutar el comando. Revisa la consola del bot para ver el error exacto.")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ============================================================
# PREFIX COMMANDS (1ban, 1kick, 1mute, etc.)
# ============================================================

@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def prefix_ban(ctx, member: discord.Member, *, reason: str = "Sin motivo"):
    if member == ctx.author:
        await ctx.send(embed=error_embed("No puedes banearte a ti mismo."))
        return
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        await ctx.send(embed=error_embed("No puedes banear a un miembro con un rol igual o superior al tuyo."))
        return
    try:
        await member.ban(reason=f"{reason} | Por: {ctx.author}")
        await ctx.send(embed=ban_embed(member, reason, ctx.author))
    except discord.Forbidden:
        await ctx.send(embed=error_embed("No tengo permisos para banear a ese miembro."))


@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def prefix_kick(ctx, member: discord.Member, *, reason: str = "Sin motivo"):
    if member == ctx.author:
        await ctx.send(embed=error_embed("No puedes expulsarte a ti mismo."))
        return
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        await ctx.send(embed=error_embed("No puedes expulsar a un miembro con un rol igual o superior al tuyo."))
        return
    try:
        await member.kick(reason=f"{reason} | Por: {ctx.author}")
        embed = discord.Embed(title="👢 Usuario expulsado", description=f"**{member}** fue expulsado del servidor.", color=discord.Color.orange())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="📝 Motivo", value=reason, inline=False)
        embed.add_field(name="🛡️ Moderador", value=ctx.author.mention, inline=True)
        embed.set_footer(text="Zyron • Sistema de moderación")
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send(embed=error_embed("No tengo permisos para expulsar a ese miembro."))


@bot.command(name="mute")
@commands.has_permissions(moderate_members=True)
async def prefix_mute(ctx, member: discord.Member, duration: str, *, reason: str = "Sin motivo"):
    if member == ctx.author:
        await ctx.send(embed=error_embed("No puedes silenciarte a ti mismo."))
        return
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        await ctx.send(embed=error_embed("No puedes silenciar a un miembro con un rol igual o superior al tuyo."))
        return
    try:
        delta = parse_duration(duration)
    except ValueError as exc:
        await ctx.send(embed=error_embed(f"❌ {exc}. Usa por ejemplo `10m`, `2h` o `3d` (máximo `28d`)."))
        return
    try:
        await member.timeout(delta, reason=f"{reason} | Por: {ctx.author}")
        embed = discord.Embed(title="🔇 Usuario silenciado", description=f"**{member}** fue silenciado.", color=discord.Color.dark_gray())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="📝 Motivo", value=reason, inline=False)
        embed.add_field(name="⏱️ Duración", value=format_duration(delta), inline=True)
        embed.add_field(name="🛡️ Moderador", value=ctx.author.mention, inline=True)
        embed.set_footer(text="Zyron • Sistema de moderación")
        embed.timestamp = discord.utils.utcnow()
        await ctx.send(embed=embed)
    except discord.Forbidden:
        await ctx.send(embed=error_embed("No tengo permisos para silenciar a ese miembro."))


@bot.command(name="unmute")
@commands.has_permissions(moderate_members=True)
async def prefix_unmute(ctx, member: discord.Member):
    try:
        await member.timeout(None, reason=f"Unmute por: {ctx.author}")
        await ctx.send(embed=success_embed("🔊 Mute retirado", f"Se quitó el mute a **{member}**."))
    except discord.Forbidden:
        await ctx.send(embed=error_embed("No tengo permisos para quitar el mute."))


@bot.command(name="lock")
@commands.has_permissions(manage_channels=True)
async def prefix_lock(ctx, role: discord.Role):
    try:
        overwrite = ctx.channel.overwrites_for(role)
        overwrite.send_messages = False
        await ctx.channel.set_permissions(role, overwrite=overwrite, reason=f"Canal bloqueado para {role.name} por: {ctx.author}")
        await ctx.send(embed=success_embed("🔒 Canal bloqueado", f"El rol {role.mention} ya no puede hablar en este canal."))
    except discord.Forbidden:
        await ctx.send(embed=error_embed("No tengo permisos para modificar este canal."))
    except discord.HTTPException:
        await ctx.send(embed=error_embed("Discord no pudo modificar los permisos del canal."))


@bot.command(name="unlock")
@commands.has_permissions(manage_channels=True)
async def prefix_unlock(ctx, role: discord.Role):
    try:
        overwrite = ctx.channel.overwrites_for(role)
        overwrite.send_messages = None
        await ctx.channel.set_permissions(role, overwrite=overwrite, reason=f"Canal desbloqueado para {role.name} por: {ctx.author}")
        await ctx.send(embed=success_embed("🔓 Canal desbloqueado", f"El rol {role.mention} puede volver a hablar en este canal."))
    except discord.Forbidden:
        await ctx.send(embed=error_embed("No tengo permisos para modificar este canal."))
    except discord.HTTPException:
        await ctx.send(embed=error_embed("Discord no pudo modificar los permisos del canal."))


@bot.command(name="lockall")
@commands.has_permissions(manage_channels=True)
async def prefix_lockall(ctx, role: discord.Role):
    locked = failed = 0
    for channel in ctx.guild.channels:
        try:
            overwrite = channel.overwrites_for(role)
            overwrite.send_messages = False
            await channel.set_permissions(role, overwrite=overwrite, reason=f"Lockall de {role.name} por: {ctx.author}")
            locked += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
    extra = f"\n⚠️ No pude modificar **{failed}** canales." if failed else ""
    await ctx.send(embed=success_embed("🔒 Lock All", f"El rol {role.mention} fue bloqueado en **{locked} canales**. Ya no podrá hablar en ellos.{extra}"))


@bot.command(name="unlockall")
@commands.has_permissions(manage_channels=True)
async def prefix_unlockall(ctx, role: discord.Role):
    unlocked = failed = 0
    for channel in ctx.guild.channels:
        try:
            overwrite = channel.overwrites_for(role)
            overwrite.send_messages = None
            await channel.set_permissions(role, overwrite=overwrite, reason=f"Unlockall de {role.name} por: {ctx.author}")
            unlocked += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
    extra = f"\n⚠️ No pude modificar **{failed}** canales." if failed else ""
    await ctx.send(embed=success_embed("🔓 Unlock All", f"El rol {role.mention} fue desbloqueado en **{unlocked} canales**. Ya puede hablar nuevamente.{extra}"))


@bot.command(name="adderole")
@commands.has_permissions(manage_roles=True)
async def prefix_adderole(ctx, member: discord.Member, role: discord.Role):
    if role >= ctx.guild.me.top_role:
        await ctx.send(embed=error_embed("No puedo administrar ese rol porque está por encima de mi rol."))
        return
    try:
        await member.add_roles(role, reason=f"Rol añadido por: {ctx.author}")
        await ctx.send(embed=success_embed("➕ Rol añadido", f"Se dio el rol **{role.name}** a **{member}**."))
    except discord.Forbidden:
        await ctx.send(embed=error_embed("No puedo administrar ese rol."))


@bot.command(name="removerole")
@commands.has_permissions(manage_roles=True)
async def prefix_removerole(ctx, member: discord.Member, role: discord.Role):
    if role >= ctx.guild.me.top_role:
        await ctx.send(embed=error_embed("No puedo administrar ese rol porque está por encima de mi rol."))
        return
    try:
        await member.remove_roles(role, reason=f"Rol quitado por: {ctx.author}")
        await ctx.send(embed=success_embed("➖ Rol retirado", f"Se quitó el rol **{role.name}** a **{member}**."))
    except discord.Forbidden:
        await ctx.send(embed=error_embed("No puedo administrar ese rol."))


@bot.command(name="clear")
@commands.has_permissions(manage_messages=True)
async def prefix_clear(ctx, cantidad: int):
    if not 1 <= cantidad <= 100:
        await ctx.send(embed=error_embed("La cantidad debe estar entre **1 y 100**."), delete_after=4)
        return
    try:
        deleted = await ctx.channel.purge(limit=cantidad + 1)
        confirm = await ctx.send(embed=success_embed("🧹 Mensajes eliminados", f"Se borraron **{max(0, len(deleted) - 1)}** mensajes."))
        await confirm.delete(delay=3)
    except discord.Forbidden:
        await ctx.send(embed=error_embed("No tengo permisos para borrar mensajes."))
    except discord.HTTPException:
        await ctx.send(embed=error_embed("Discord no pudo borrar los mensajes."))


@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def prefix_purge(ctx):
    try:
        deleted = await ctx.channel.purge(limit=None)
        await ctx.send(
            embed=success_embed(
                "🧹 Canal limpiado",
                f"Se eliminaron **{len(deleted)} mensajes** de este canal."
            ),
            delete_after=5
        )
    except discord.Forbidden:
        await ctx.send(embed=error_embed("No tengo permisos para borrar los mensajes de este canal."))
    except (discord.HTTPException, AttributeError):
        await ctx.send(embed=error_embed("Discord no pudo borrar todos los mensajes de este canal."))


@bot.command(name="purgeuser")
@commands.has_permissions(manage_messages=True)
async def prefix_purgeuser(ctx, member: discord.Member):
    try:
        deleted = await ctx.channel.purge(
            limit=None,
            check=lambda message: message.author.id == member.id
        )
        await ctx.send(
            embed=success_embed(
                "🧹 Mensajes del usuario eliminados",
                f"Se eliminaron **{len(deleted)} mensajes** de {member.mention} en este canal."
            ),
            delete_after=5
        )
    except discord.Forbidden:
        await ctx.send(embed=error_embed("No tengo permisos para borrar los mensajes de este canal."))
    except (discord.HTTPException, AttributeError):
        await ctx.send(embed=error_embed("Discord no pudo completar la limpieza de los mensajes."))


@bot.command(name="say")
@commands.has_permissions(manage_messages=True)
async def prefix_say(ctx, *, mensaje: str):
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass
    await ctx.send(embed=result_embed("💬 Zyron", mensaje))


@bot.command(name="warn")
@commands.has_permissions(moderate_members=True)
async def prefix_warn(ctx, member: discord.Member, *, reason: str = "Sin motivo"):
    if member == ctx.author:
        await ctx.send(embed=error_embed("No puedes advertirte a ti mismo."))
        return
    if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
        await ctx.send(embed=error_embed("No puedes advertir a un miembro con un rol igual o superior al tuyo."))
        return
    embed = discord.Embed(
        title="⚠️ Advertencia emitida",
        description=f"**{member.mention}** recibió una advertencia.",
        color=discord.Color.orange()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="📝 Motivo", value=reason, inline=False)
    embed.add_field(name="👤 Usuario", value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name="🛡️ Moderador", value=ctx.author.mention, inline=True)
    embed.set_footer(text="Zyron • Sistema de moderación")
    embed.timestamp = discord.utils.utcnow()
    await ctx.send(embed=embed)


@bot.command(name="nick")
@commands.has_permissions(manage_nicknames=True)
async def prefix_nick(ctx, member: discord.Member, *, apodo: str = None):
    if member == ctx.guild.owner or (member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner):
        await ctx.send(embed=error_embed("No puedes cambiar el apodo de un miembro con un rol igual o superior al tuyo."), delete_after=5)
        return
    if member.top_role >= ctx.guild.me.top_role:
        await ctx.send(embed=error_embed("Mi rol debe estar por encima del miembro para cambiar su apodo."), delete_after=5)
        return
    if apodo is not None and len(apodo) > 32:
        await ctx.send(embed=error_embed("El apodo no puede superar los 32 caracteres."), delete_after=5)
        return
    try:
        await member.edit(nick=apodo, reason=f"Nick cambiado por {ctx.author}")
        texto = f"Se cambió el apodo de {member.mention} a **{discord.utils.escape_markdown(apodo)}**." if apodo else f"Se quitó el apodo de {member.mention}."
        await ctx.send(embed=success_embed("🏷️ Apodo actualizado", texto))
    except discord.Forbidden:
        await ctx.send(embed=error_embed("No tengo permiso para cambiar ese apodo."), delete_after=5)
    except discord.HTTPException:
        await ctx.send(embed=error_embed("Discord no pudo cambiar el apodo."), delete_after=5)


@bot.command(name="avatar")
async def prefix_avatar(ctx, member: discord.Member):
    embed = discord.Embed(
        title=f"🖼️ Avatar de {member}",
        color=discord.Color.from_rgb(57, 255, 20)
    )
    embed.set_image(url=member.display_avatar.url)
    embed.set_footer(text=f"Zyron • ID: {member.id}")
    await ctx.send(embed=embed)


@bot.command(name="roles")
async def prefix_roles(ctx):
    if ctx.guild is None:
        await ctx.send(embed=error_embed("Este comando solo puede usarse dentro de un servidor."))
        return
    await ctx.send(embed=build_roles_embed(ctx.guild))


@bot.command(name="userinfo")
async def prefix_userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"👤 Información de {member}", color=discord.Color.from_rgb(57, 255, 20))
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🏷️ Usuario", value=f"{member.mention}\n`{member}`", inline=True)
    embed.add_field(name="🆔 ID", value=f"`{member.id}`", inline=True)
    embed.add_field(name="🎭 Rol más alto", value=member.top_role.mention, inline=True)
    embed.add_field(name="📅 Cuenta creada", value=discord.utils.format_dt(member.created_at, style="F"), inline=False)
    embed.add_field(name="📥 Entró al servidor", value=discord.utils.format_dt(member.joined_at, style="F") if member.joined_at else "Desconocido", inline=False)
    embed.set_footer(text="Zyron • Información de usuario")
    embed.timestamp = discord.utils.utcnow()
    await ctx.send(embed=embed)


@bot.command(name="server")
async def prefix_server(ctx):
    if ctx.guild is None:
        await ctx.send(embed=error_embed("Este comando solo puede usarse dentro de un servidor."))
        return
    await ctx.send(embed=build_server_embed(ctx.guild))



@bot.command(name="ask")
async def prefix_ask(ctx, *, pregunta: str):
    async with ctx.typing():
        answer, error = await ask_ai(pregunta)

    if error:
        await ctx.send(embed=error_embed(error))
        return

    await ctx.send(embed=ai_embed(pregunta, answer))


@bot.command(name="antispam")
@commands.has_permissions(manage_guild=True)
async def prefix_antispam(ctx):
    if ctx.guild is None:
        await ctx.send(embed=error_embed("Este comando solo puede usarse dentro de un servidor."))
        return

    if ctx.guild.id in ANTI_SPAM_ENABLED:
        await ctx.send(embed=success_embed("🛡️ Anti-Spam activo", "El sistema anti-spam ya estaba activado en este servidor."))
        return

    ANTI_SPAM_ENABLED.add(ctx.guild.id)
    await ctx.send(embed=success_embed("🛡️ Anti-Spam activado", "Zyron detectará **4 mensajes en menos de 5 segundos**.\n\n⚠️ A la 1.ª y 2.ª detección dará una advertencia.\n🔇 A la 3.ª detección aplicará **mute de 1 minuto**."))


@bot.command(name="unantispam")
@commands.has_permissions(manage_guild=True)
async def prefix_unantispam(ctx):
    if ctx.guild is None:
        await ctx.send(embed=error_embed("Este comando solo puede usarse dentro de un servidor."))
        return

    ANTI_SPAM_ENABLED.discard(ctx.guild.id)
    for key in list(SPAM_MESSAGES):
        if key[0] == ctx.guild.id:
            SPAM_MESSAGES.pop(key, None)
            SPAM_STRIKES.pop(key, None)

    await ctx.send(embed=success_embed("🛡️ Anti-Spam desactivado", "El sistema anti-spam ya no está activo en este servidor."))


@bot.command(name="automod")
@commands.has_permissions(manage_guild=True)
async def prefix_automod(ctx, accion: str = None):
    if ctx.guild is None:
        await ctx.send(embed=error_embed("Este comando solo puede usarse dentro de un servidor."), delete_after=5)
        return

    accion = (accion or "estado").lower()
    guild_id = ctx.guild.id
    if accion == "activar":
        AUTOMOD_ENABLED.add(guild_id)
        await ctx.send(embed=success_embed("🛡️ AutoMod activado", "Zyron bloqueará enlaces, menciones masivas y flood de caracteres."))
    elif accion == "desactivar":
        AUTOMOD_ENABLED.discard(guild_id)
        await ctx.send(embed=success_embed("🛡️ AutoMod desactivado", "Zyron dejará de revisar automáticamente los mensajes."))
    elif accion == "estado":
        estado = "🟢 Activado" if guild_id in AUTOMOD_ENABLED else "🔴 Desactivado"
        embed = discord.Embed(title="🛡️ Estado de AutoMod", description=f"Estado actual: **{estado}**", color=discord.Color.from_rgb(57, 255, 20))
        embed.add_field(name="🔗 Enlaces", value="Bloqueados", inline=True)
        embed.add_field(name="📢 Menciones", value=f"Máximo {AUTOMOD_MENTION_LIMIT}", inline=True)
        embed.add_field(name="🧹 Flood", value=f"Detecta {AUTOMOD_REPEAT_LIMIT}+ caracteres repetidos", inline=True)
        embed.set_footer(text="Zyron • AutoMod")
        await ctx.send(embed=embed)
    else:
        await ctx.send(embed=error_embed(f"Usa `{PREFIX}automod activar`, `{PREFIX}automod desactivar` o `{PREFIX}automod estado`."), delete_after=5)


@bot.command(name="verified")
@commands.has_permissions(manage_guild=True)
async def prefix_verified(ctx, role: discord.Role):
    if ctx.guild is None:
        await ctx.send(embed=error_embed("Este comando solo puede usarse dentro de un servidor."), delete_after=5)
        return
    me=ctx.guild.me
    if me is None or not me.guild_permissions.manage_roles:
        await ctx.send(embed=error_embed("Necesito el permiso **Gestionar roles**."), delete_after=5)
        return
    if role.is_default() or role.managed or role >= me.top_role:
        await ctx.send(embed=error_embed(f"No puedo administrar {role.mention}. Revisa la posición de mi rol."), delete_after=5)
        return
    VERIFIED_ROLE_IDS[ctx.guild.id]=role.id
    embed=discord.Embed(title="🛡️ Verificación", description=f"Pulsa el botón de abajo para verificarte.\n\nRecibirás automáticamente el rol {role.mention}.", color=discord.Color.green())
    embed.set_footer(text="Zyron • Sistema de verificación")
    embed.timestamp=discord.utils.utcnow()
    await ctx.send(embed=embed, view=VerifiedView())


@bot.command(name="panel")
@commands.has_permissions(manage_guild=True)
async def prefix_panel(ctx):
    if ctx.guild is None:
        await ctx.send(embed=error_embed("Este comando solo puede usarse dentro de un servidor."), delete_after=5)
        return

    embed = discord.Embed(
        title="🎫 Soporte de Zyron",
        description=(
            "¿Necesitas ayuda?\n\n"
            "Pulsa **Crear ticket** para abrir un canal privado con el equipo.\n"
            "Puedes modificar los permisos del canal una vez creado.\n\n"
            "🔒 Para cerrar el ticket, usa el botón **Cerrar ticket** dentro del canal."
        ),
        color=discord.Color.from_rgb(57, 255, 20)
    )
    embed.set_footer(text="Zyron • Sistema de tickets")
    embed.timestamp = discord.utils.utcnow()
    await ctx.send(embed=embed, view=TicketView())


@bot.command(name="help")
async def prefix_help(ctx):
    await ctx.send(embed=build_help_embed())


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=error_embed("No tienes los permisos necesarios para usar este comando."), delete_after=5)
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=error_embed(f"Faltan argumentos. Usa `/help`, `1help` o `Zyron help` para ver cómo funcionan."), delete_after=5)
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send(embed=error_embed("No encontré ese miembro."), delete_after=5)
    elif isinstance(error, commands.RoleNotFound):
        await ctx.send(embed=error_embed("No encontré ese rol."), delete_after=5)
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=error_embed("Revisa los argumentos del comando."), delete_after=5)
    elif isinstance(error, commands.CommandNotFound):
        return
    else:
        print(f"Error en comando: {error}")


# ============================================================
# EVENTS
# ============================================================

@bot.event
async def setup_hook():
    bot.add_view(VerifiedView())
    bot.add_view(TicketView())
    bot.add_view(TicketCloseView())
    await bot.tree.sync()
    print("Comandos / sincronizados")


@bot.event
async def on_ready():
    print(f"Conectado como {bot.user}")


# ============================================================
# START
# ============================================================

if not TOKEN:
    raise RuntimeError("Falta DISCORD_TOKEN")

bot.run(TOKEN)
