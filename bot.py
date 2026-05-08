import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
from discord import Interaction
import json
import os
import asyncio
import logging
from datetime import datetime, timedelta

# ---- LOGGING SETUP ----
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ---- CONFIG ----
try:
    with open("config.json", "r") as f:
        config = json.load(f)
except FileNotFoundError:
    log.error("config.json nicht gefunden! Bitte erstelle die Datei.")
    raise
except json.JSONDecodeError as e:
    log.error(f"config.json ist ungültig: {e}")
    raise

TOKEN            = config["token"]
CHANNEL_ID       = config["channel_id"]
ROLES_TO_PING    = config["roles_to_ping"]
BUTTON_ROLE_IDS  = config["button_role_ids"]
STATE_FILE       = config["state_file"]
COLLECTED_FILE   = config["collected_file"]
START_FILE       = config["start_file"]

# Stunden-Mapping: Startzeit -> Endzeit (kann in config ausgelagert werden)
HOUR_END_MAP = config.get("hour_end_map", {"10": 16, "16": 22, "22": 4})

# ---- DISCORD SETUP ----
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)

# ---- STATE (RAM) ----
active_reminders: dict[int, asyncio.Task] = {}
last_confirm: dict[int, str] = {}
skip_until_next_hour: dict[int, str] = {}
last_winner_press: dict[int, str] = {}

# ---- JSON HELPER ----
def load_json(file: str) -> dict | list:
    if os.path.exists(file):
        with open(file, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError as e:
                log.warning(f"Fehler beim Lesen von {file}: {e} — leere Daten werden verwendet.")
                return {}
    return {}

def save_json(file: str, data: dict | list) -> None:
    try:
        with open(file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as e:
        log.error(f"Fehler beim Speichern von {file}: {e}")

# ---- LOGGING HELPERS ----
def log_collection(user: discord.Member, start_hour: int) -> None:
    entry = {
        "DiscordName": str(user),
        "DiscordID":   str(user.id),
        "Timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "StartHour":   start_hour
    }
    data = load_json(COLLECTED_FILE)
    if not isinstance(data, list):
        data = []
    data.append(entry)
    save_json(COLLECTED_FILE, data)
    log.info(f"Eingesammelt: {user} um {start_hour} Uhr")

def log_winner(user: discord.Member, start_hour: int) -> None:
    data = load_json(START_FILE)
    current_hour_key = datetime.now().strftime("%Y-%m-%d_%H")
    hour_str = str(start_hour)
    if hour_str not in data:
        data[hour_str] = {}
    data[hour_str][current_hour_key] = {
        "DiscordName": str(user),
        "DiscordID":   str(user.id),
        "Timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_json(START_FILE, data)
    log.info(f"Gewinner: {user} um {start_hour} Uhr")

# ---- HELPER ----
def has_button_role(member: discord.Member) -> bool:
    return any(r.id in BUTTON_ROLE_IDS for r in getattr(member, "roles", []))

def get_end_hour(start_hour: int) -> int:
    return HOUR_END_MAP.get(str(start_hour), {10: 16, 16: 22, 22: 4}[start_hour])

def mentions_string() -> str:
    return " ".join([f"<@&{rid}>" for rid in ROLES_TO_PING])

# ---- UI: HAUPTMENÜ ----
class RPView(View):
    def __init__(self):
        super().__init__(timeout=None)
        for label in ["10 Uhr", "16 Uhr", "22 Uhr"]:
            btn = Button(
                label=label,
                style=discord.ButtonStyle.primary,
                custom_id=f"time_{label}"
            )
            btn.callback = self.time_button_callback
            self.add_item(btn)

    async def time_button_callback(self, interaction: Interaction):
        if not has_button_role(interaction.user):
            await interaction.response.send_message(
                "Du hast keine Berechtigung, diesen Button zu benutzen!",
                ephemeral=True
            )
            return

        label = interaction.data["custom_id"].replace("time_", "")
        hour = int(label.split(" ")[0])
        current_hour_key = datetime.now().strftime("%Y-%m-%d_%H")

        if last_winner_press.get(hour) == current_hour_key:
            await interaction.response.send_message(
                f"Für **{label}** wurde in dieser Stunde bereits auf **Gewonnen** gedrückt!",
                ephemeral=True
            )
            return

        last_winner_press[hour] = current_hour_key
        log_winner(interaction.user, hour)

        await interaction.response.send_message(
            f"{interaction.user.mention} hat **{label} gewonnen!**"
        )

        end_hour = get_end_hour(hour)

        # Alten Reminder-Task sauber beenden
        if hour in active_reminders:
            task = active_reminders.pop(hour)
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        task = asyncio.create_task(start_reminder(interaction.guild, hour, end_hour))
        active_reminders[hour] = task
        log.info(f"Reminder gestartet für {hour} Uhr → endet um {end_hour} Uhr")

# ---- UI: EINSAMMELN BUTTON ----
class ConfirmView(View):
    def __init__(self, start_hour: int):
        super().__init__(timeout=None)
        self.start_hour = start_hour
        btn = Button(
            label="Eingesammelt",
            style=discord.ButtonStyle.success,
            custom_id=f"confirm_{start_hour}"
        )
        btn.callback = self.confirm_callback
        self.add_item(btn)

    async def confirm_callback(self, interaction: Interaction):
        if not has_button_role(interaction.user):
            await interaction.response.send_message(
                "Du hast keine Berechtigung, diesen Button zu benutzen!",
                ephemeral=True
            )
            return

        current_hour_key = datetime.now().strftime("%Y-%m-%d_%H")

        if last_confirm.get(self.start_hour) == current_hour_key:
            await interaction.response.send_message(
                "Für diese Stunde wurde bereits eingesammelt!",
                ephemeral=True
            )
            return

        last_confirm[self.start_hour] = current_hour_key
        skip_until_next_hour[self.start_hour] = current_hour_key
        log_collection(interaction.user, self.start_hour)

        await interaction.response.send_message(
            f"{interaction.user.mention} hat eingesammelt!"
        )

# ---- REMINDER LOGIK ----
async def start_reminder(guild: discord.Guild, start_hour: int, end_hour: int):
    channel = guild.get_channel(CHANNEL_ID)
    if not channel:
        log.error(f"Kanal {CHANNEL_ID} nicht gefunden!")
        return

    now = datetime.now()
    today = now.date()

    # Endzeit berechnen (22 Uhr → 4 Uhr nächsten Tag)
    if start_hour > end_hour:
        end_time = datetime.combine(today + timedelta(days=1), datetime.min.time()).replace(hour=end_hour)
    else:
        end_time = datetime.combine(today, datetime.min.time()).replace(hour=end_hour)

    mentions = mentions_string()
    current_hour = now.replace(minute=0, second=0, microsecond=0)

    # Erste Nachricht sofort senden
    await channel.send(f"{mentions} RP Tickets einsammeln!", view=ConfirmView(start_hour))

    # Erinnerungen bei :15, :30, :45 der aktuellen Stunde
    for step in [15, 30, 45]:
        remind_time = current_hour + timedelta(minutes=step)
        if remind_time <= now or remind_time >= end_time:
            continue

        wait = (remind_time - datetime.now()).total_seconds()
        if wait > 0:
            await asyncio.sleep(wait)

        if start_hour not in active_reminders:
            return

        if skip_until_next_hour.get(start_hour) == current_hour.strftime("%Y-%m-%d_%H"):
            break

        await channel.send(f"{mentions} Erinnerung: RP Tickets einsammeln!", view=ConfirmView(start_hour))

    # Folgestunden durchlaufen
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    while next_hour < end_time:
        wait = (next_hour - datetime.now()).total_seconds()
        if wait > 0:
            await asyncio.sleep(wait)

        if start_hour not in active_reminders:
            return

        current_hour_key = next_hour.strftime("%Y-%m-%d_%H")

        if skip_until_next_hour.get(start_hour) == current_hour_key:
            next_hour += timedelta(hours=1)
            continue

        await channel.send(f"{mentions} RP Tickets einsammeln!", view=ConfirmView(start_hour))

        for step in [15, 30, 45]:
            remind_time = next_hour + timedelta(minutes=step)
            if remind_time >= end_time:
                break

            wait = (remind_time - datetime.now()).total_seconds()
            if wait > 0:
                await asyncio.sleep(wait)

            if start_hour not in active_reminders:
                return

            if skip_until_next_hour.get(start_hour) == current_hour_key:
                break

            await channel.send(f"{mentions} Erinnerung: RP Tickets einsammeln!", view=ConfirmView(start_hour))

        next_hour += timedelta(hours=1)

    # Letzte Nachricht am Ende des Zyklus
    await channel.send(f"{mentions} Letzte Chance! RP Tickets einsammeln!", view=ConfirmView(start_hour))

    active_reminders.pop(start_hour, None)
    log.info(f"Reminder für {start_hour} Uhr beendet.")

# ---- BOT EVENTS ----
@bot.event
async def on_ready():
    log.info(f"Eingeloggt als {bot.user} (ID: {bot.user.id})")

    bot.add_view(RPView())
    # Alle möglichen ConfirmViews persistent registrieren
    for hour in [10, 16, 22]:
        bot.add_view(ConfirmView(hour))

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        log.error(f"Kanal {CHANNEL_ID} nicht gefunden!")
        return

    state = load_json(STATE_FILE)
    msg_id = state.get("message_id")

    if msg_id:
        try:
            await channel.fetch_message(msg_id)
            log.info(f"Bestehende UI-Nachricht gefunden (ID: {msg_id})")
        except discord.NotFound:
            log.warning("Alte UI-Nachricht nicht mehr vorhanden — neue wird erstellt.")
            msg = await channel.send("**RP Fabrik gewonnen?**", view=RPView())
            state["message_id"] = msg.id
            save_json(STATE_FILE, state)
        except discord.HTTPException as e:
            log.error(f"Fehler beim Abrufen der Nachricht: {e}")
    else:
        msg = await channel.send("**RP Fabrik gewonnen?**", view=RPView())
        state["message_id"] = msg.id
        save_json(STATE_FILE, state)
        log.info(f"Neue UI-Nachricht erstellt (ID: {msg.id})")

    try:
        synced = await bot.tree.sync()
        log.info(f"{len(synced)} Slash-Commands synchronisiert.")
    except Exception as e:
        log.error(f"Slash-Command Sync Fehler: {e}")

    # Nur starten wenn nicht bereits läuft (wichtig bei Reconnects!)
    if not repost_ui.is_running():
        repost_ui.start()

@bot.event
async def on_disconnect():
    log.warning("Bot getrennt — warte auf Reconnect...")

# ---- REPOST UI ----
@tasks.loop(minutes=1)
async def repost_ui():
    now = datetime.now()
    state = load_json(STATE_FILE)
    repost_times = {(10, 15), (16, 15), (22, 15)}

    if (now.hour, now.minute) not in repost_times:
        return

    today_key = f"{now.date()}_{now.hour}_{now.minute}"
    if state.get("last_repost") == today_key:
        return

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        log.error(f"Repost: Kanal {CHANNEL_ID} nicht gefunden!")
        return

    msg_id = state.get("message_id")
    if msg_id:
        try:
            old_msg = await channel.fetch_message(msg_id)
            await old_msg.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            log.warning(f"Alte Nachricht konnte nicht gelöscht werden: {e}")

    msg = await channel.send("**RP Fabrik gewonnen?**", view=RPView())
    state["message_id"] = msg.id
    state["last_repost"] = today_key
    save_json(STATE_FILE, state)
    log.info(f"UI-Nachricht neu gepostet um {now.strftime('%H:%M')}")

@repost_ui.before_loop
async def before_repost():
    await bot.wait_until_ready()

# ---- START ----
bot.run(TOKEN, log_handler=None)  # log_handler=None da wir eigenes Logging nutzen
