import os
import json
import asyncio
import time
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# -------------------------------- КОНФИГ --------------------------------

load_dotenv()

DISCORD_TOKEN = ""
FOOTBALL_DATA_TOKEN = ""

GUILD_ID = 1225075859333845154          # ID сервера
TEXT_CHANNEL_ID = 1407445373571563610   # ID текстового канала
VOICE_CHANNEL_ID = 1289694911234310155  # ID голосового канала

FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"  # v4 API

# Нужны только для /league-table и /leagues, live/upcoming теперь по командам
COMPETITIONS_TRACKED: Dict[str, str] = {
    "WC":  "FIFA World Cup",
    "CL":  "UEFA Champions League",
    "BL1": "Bundesliga",
    "DED": "Eredivisie",
    "BSA": "Campeonato Brasileiro Série A",
    "PD":  "Primera Division",
    "FL1": "Ligue 1",
    "ELC": "Championship",
    "PPL": "Primeira Liga",
    "EC":  "European Championship",
    "SA":  "Serie A",
    "PL":  "Premier League",
}

SUBSCRIPTIONS_FILE = Path("subscriptions.json")

SOUNDS = {
    "command":     "sounds/command.mp3",
    "goal":        "sounds/goal.mp3",
    "match_start": "sounds/start.mp3",
    "timeout":     "sounds/timeout.mp3",
    "match_end":   "sounds/end.mp3",
}

TEAMS_CACHE_FILE = Path("teams_cache.json")

# Кэш live-матчей
LIVE_CACHE_TTL_SECONDS = 60

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

last_fixtures_state: Dict[int, Dict[str, Any]] = {}
TEAMS_CACHE: Dict[str, Dict[str, Any]] = {}
TEAMS_CACHE_BUILT = False

live_cache: Dict[str, Any] = {
    "timestamp": 0,
    "fixtures": [],
}

# ---------------------------- УТИЛИТЫ JSON-БД ----------------------------

def load_subscriptions() -> Dict[str, Any]:
    if not SUBSCRIPTIONS_FILE.exists():
        return {"users": {}}
    with SUBSCRIPTIONS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_subscriptions(data: Dict[str, Any]) -> None:
    SUBSCRIPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SUBSCRIPTIONS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_team_subscription(user_id: int, team_id: int, team_name: str, league_name: str) -> None:
    db = load_subscriptions()
    users = db.setdefault("users", {})
    user_entry = users.setdefault(str(user_id), {"teams": []})

    for t in user_entry["teams"]:
        if t["team_id"] == team_id:
            return

    user_entry["teams"].append(
        {"team_id": team_id, "team_name": team_name, "league": league_name}
    )
    save_subscriptions(db)


def remove_team_subscription(user_id: int, team_id: int) -> bool:
    db = load_subscriptions()
    users = db.setdefault("users", {})
    entry = users.get(str(user_id))
    if not entry:
        return False

    before = len(entry["teams"])
    entry["teams"] = [t for t in entry["teams"] if t["team_id"] != team_id]
    changed = len(entry["teams"]) != before
    if changed:
        save_subscriptions(db)
    return changed


def clear_user_subscriptions(user_id: int) -> None:
    db = load_subscriptions()
    users = db.setdefault("users", {})
    users[str(user_id)] = {"teams": []}
    save_subscriptions(db)


def get_user_subscriptions(user_id: int) -> List[Dict[str, Any]]:
    db = load_subscriptions()
    return db.get("users", {}).get(str(user_id), {}).get("teams", [])


def get_all_subscribed_team_ids() -> set[int]:
    db = load_subscriptions()
    ids: set[int] = set()
    for entry in db.get("users", {}).values():
        for t in entry.get("teams", []):
            ids.add(t["team_id"])
    return ids

# ---------------------------- УТИЛИТЫ ВРЕМЕНИ ----------------------------

def format_match_time(utc_iso: str) -> str:
    try:
        dt_utc = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
        dt_msk = dt_utc + timedelta(hours=3)
        return dt_msk.strftime("%d.%m.%Y %H:%M") + " (по МСК)"
    except Exception:
        return utc_iso


def match_datetime_msk(utc_iso: str) -> Optional[datetime]:
    try:
        dt_utc = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
        return dt_utc + timedelta(hours=3)
    except Exception:
        return None


def humanize_time_to_match(utc_iso: str) -> str:
    dt_msk = match_datetime_msk(utc_iso)
    if dt_msk is None:
        return "в неизвестное время"

    now_utc = datetime.now(timezone.utc)
    now_msk = now_utc + timedelta(hours=3)

    delta = dt_msk - now_msk
    total_sec = int(delta.total_seconds())

    if total_sec < -3600:
        return "уже прошёл"
    if total_sec < 0:
        return "уже идёт"

    days = total_sec // 86400
    hours = (total_sec % 86400) // 3600
    minutes = (total_sec % 3600) // 60

    if days == 0 and hours == 0:
        return f"через {minutes} минут"
    if days == 0:
        return f"через {hours} ч {minutes} мин"
    if days == 1:
        return f"через {days} день {hours} ч"
    if 2 <= days <= 4:
        return f"через {days} дня {hours} ч"
    return f"через {days} дней {hours} ч"

# ------------------------- РАБОТА С football-data.org --------------------

def football_headers() -> Dict[str, str]:
    return {
        "X-Auth-Token": FOOTBALL_DATA_TOKEN or "",
        "Accept": "application/json",
    }

# ---------- КЭШ КОМАНД В ФАЙЛЕ (ТОЛЬКО ИЗ ФАЙЛА) ----------

async def build_teams_cache(session: aiohttp.ClientSession):
    global TEAMS_CACHE, TEAMS_CACHE_BUILT

    if TEAMS_CACHE_BUILT:
        return

    if not TEAMS_CACHE_FILE.exists():
        print("[teams_cache] Файл teams_cache.json не найден.")
        return

    try:
        with TEAMS_CACHE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        teams = data.get("teams", {})
        if not teams:
            print("[teams_cache] В файле teams_cache.json нет команд.")
            return
        TEAMS_CACHE = teams
        TEAMS_CACHE_BUILT = True
        print(f"[teams_cache] Загружен локальный кэш команд: {len(TEAMS_CACHE)}")
    except Exception as e:
        print(f"[teams_cache] Ошибка чтения локального кэша: {e}")


async def search_team(session: aiohttp.ClientSession, query: str) -> Optional[Dict[str, Any]]:
    if not TEAMS_CACHE:
        print("[search_team] TEAMS_CACHE пуст — кэш команд не загружен.")
        return None

    q = query.lower().strip()

    if q in TEAMS_CACHE:
        return TEAMS_CACHE[q]

    for key, info in TEAMS_CACHE.items():
        if key.startswith(q):
            return info

    for key, info in TEAMS_CACHE.items():
        if q in key:
            return info

    return None

# ---------- ЗАПРОСЫ ПО КОМАНДАМ ----------

async def fetch_team_matches(
    session: aiohttp.ClientSession,
    team_id: int,
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    /v4/teams/{id}/matches — матчи конкретной команды. [web:51][web:81]
    """
    url = f"{FOOTBALL_DATA_BASE}/teams/{team_id}/matches"
    params: Dict[str, str] = {}
    if status:
        params["status"] = status
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to

    async with session.get(url, params=params, headers=football_headers()) as resp:
        try:
            data = await resp.json()
        except Exception:
            data = {}

    if resp.status == 429:
        print(f"[team_matches] 429 для team_id={team_id}: {data}")
        return []
    if resp.status == 403:
        print(f"[team_matches] 403 (нет доступа) для team_id={team_id}: {data}")
        return []
    if resp.status != 200:
        print(f"[team_matches] Ошибка {resp.status} для team_id={team_id}: {data}")
        return []

    return data.get("matches", [])

# ---------- LIVE-МАТЧИ ПО ПОДПИСАННЫМ КОМАНДАМ ----------

async def fetch_live_fixtures(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    """
    Live + свежие FINISHED ТОЛЬКО по командам, на которые кто-то подписан. [web:51]
    """
    global live_cache

    if time.time() - live_cache.get("timestamp", 0) <= LIVE_CACHE_TTL_SECONDS and live_cache.get("fixtures"):
        return live_cache["fixtures"]

    subscribed_team_ids = get_all_subscribed_team_ids()
    if not subscribed_team_ids:
        print("[live_fixtures] Нет подписанных команд — live не опрашиваем.")
        live_cache = {"timestamp": time.time(), "fixtures": []}
        return []

    fixtures_by_id: Dict[int, Dict[str, Any]] = {}

    today = datetime.now(timezone.utc).date()
    date_from = (today - timedelta(days=1)).isoformat()
    date_to = (today + timedelta(days=1)).isoformat()

    for team_id in subscribed_team_ids:
        matches = await fetch_team_matches(
            session,
            team_id=team_id,
            status="LIVE,IN_PLAY,PAUSED,FINISHED",
            date_from=date_from,
            date_to=date_to,
        )
        for m in matches:
            mid = m["id"]
            fixtures_by_id[mid] = m  # dedup по матчу

    fixtures = list(fixtures_by_id.values())

    now_utc = datetime.now(timezone.utc)
    recent: List[Dict[str, Any]] = []
    for m in fixtures:
        status = m.get("status", "")
        if status in ("LIVE", "IN_PLAY", "PAUSED"):
            recent.append(m)
        elif status == "FINISHED":
            try:
                dt = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
                if now_utc - dt < timedelta(hours=4):
                    recent.append(m)
            except Exception:
                pass

    print(f"[live_fixtures] По командам найдено матчей: {len(fixtures)}, использовано: {len(recent)}")

    live_cache = {
        "timestamp": time.time(),
        "fixtures": recent,
    }
    return recent

# ---------- UPCOMING ПО ПОДПИСАННЫМ КОМАНДАМ (ПО ПОЛЬЗОВАТЕЛЮ) ----------

async def fetch_upcoming_for_user(
    session: aiohttp.ClientSession,
    user_team_ids: List[int],
) -> Dict[int, List[Dict[str, Any]]]:
    """
    Возвращает словарь team_id -> список SCHEDULED/TIMED матчей на 14 дней вперёд. [web:51]
    """
    today = datetime.now(timezone.utc).date()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=14)).isoformat()

    result: Dict[int, List[Dict[str, Any]]] = {}

    for tid in user_team_ids:
        matches = await fetch_team_matches(
            session,
            team_id=tid,
            status="SCHEDULED,TIMED",
            date_from=date_from,
            date_to=date_to,
        )
        # фильтруем только нормальные матчи (где обе команды есть)
        clean: List[Dict[str, Any]] = []
        for m in matches:
            if "homeTeam" in m and "awayTeam" in m and "competition" in m and "utcDate" in m:
                clean.append(m)
        if clean:
            result[tid] = clean

    return result

# ----------------------------- ВОЙС И ЗВУК -------------------------------

async def ensure_voice_connected():
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(VOICE_CHANNEL_ID)
    if not isinstance(channel, discord.VoiceChannel):
        return

    if guild.voice_client is None or not guild.voice_client.is_connected():
        await channel.connect()
    elif guild.voice_client.channel.id != VOICE_CHANNEL_ID:
        await guild.voice_client.move_to(channel)


async def play_sound(kind: str):
    await ensure_voice_connected()
    guild = bot.get_guild(GUILD_ID)
    if not guild or guild.voice_client is None:
        return

    path = SOUNDS.get(kind)
    if not path or not Path(path).exists():
        return

    vc = guild.voice_client
    if vc.is_playing():
        vc.stop()

    source = discord.FFmpegPCMAudio(path)
    vc.play(source)

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.id != bot.user.id:
        return

    if after.channel is None or (after.channel and after.channel.id != VOICE_CHANNEL_ID):
        await asyncio.sleep(1)
        await ensure_voice_connected()

# --------- ДЕКОРАТОР ДЛЯ ОГРАНИЧЕНИЯ КОМАНД ПО КАНАЛУ ---------

def only_in_allowed_channel():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "Команды бота можно использовать только на сервере, не в личных сообщениях.",
                ephemeral=True
            )
            return False
        if interaction.channel_id != TEXT_CHANNEL_ID:
            await interaction.response.send_message(
                "Эти команды разрешено использовать только в указанном служебном канале.",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)

# -------------------------- AUTOCOMPLETE ДЛЯ /live -----------------------

async def team_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> List[app_commands.Choice[str]]:
    choices: List[app_commands.Choice[str]] = []

    if not TEAMS_CACHE:
        return choices

    names_seen = set()
    q = current.lower().strip()

    for info in TEAMS_CACHE.values():
        name = info["team_name"]
        lname = name.lower()
        if q and q not in lname:
            continue
        if name in names_seen:
            continue
        names_seen.add(name)
        choices.append(app_commands.Choice(name=name, value=name))
        if len(choices) >= 25:
            break

    if not q and not choices:
        for info in list(TEAMS_CACHE.values())[:25]:
            name = info["team_name"]
            choices.append(app_commands.Choice(name=name, value=name))

    return choices

# ------------------------------- КОМАНДЫ -------------------------------

@tree.command(name="help", description="Показать список команд футбольного бота")
@only_in_allowed_channel()
async def help_command(interaction: discord.Interaction):
    await play_sound("command")

    embed = discord.Embed(
        title="⚽ Футбольный бот — помощь",
        description="Бот показывает live-счёт, таблицы и уведомляет о матчах отслеживаемых турниров.",
        colour=discord.Colour.blue()
    )
    embed.add_field(
        name="/live [команда]",
        value="Подписаться на уведомления по выбранной команде.",
        inline=False
    )
    embed.add_field(
        name="/live-upcoming",
        value="Ближайшие матчи по твоим подписанным командам.",
        inline=False
    )
    embed.add_field(
        name="/live-now",
        value="Live-матчи по подписанным командам.",
        inline=False
    )
    embed.add_field(
        name="/live-list",
        value="Список твоих подписанных команд.",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="live", description="Подписаться на команду из поддерживаемых турниров")
@only_in_allowed_channel()
@app_commands.describe(team="Название команды (можно часть названия)")
@app_commands.autocomplete(team=team_autocomplete)
async def live_subscribe(interaction: discord.Interaction, team: str):
    await interaction.response.defer(ephemeral=True)
    await play_sound("command")

    async with aiohttp.ClientSession() as session:
        info = await search_team(session, team)

    if not info:
        await interaction.followup.send(
            "Не удалось найти такую команду среди поддерживаемых турниров.",
            ephemeral=True
        )
        return

    add_team_subscription(
        user_id=interaction.user.id,
        team_id=info["team_id"],
        team_name=info["team_name"],
        league_name=info["league_name"],
    )

    embed = discord.Embed(
        title="✅ Подписка оформлена",
        description=f"Теперь ты будешь получать уведомления по команде **{info['team_name']}** "
                    f"({info['league_name']}).",
        colour=discord.Colour.green()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)

@tree.command(name="live-stop", description="Отменить подписку на команду")
@only_in_allowed_channel()
@app_commands.describe(team_id="ID команды (смотри /live-list)")
async def live_stop(interaction: discord.Interaction, team_id: int):
    await play_sound("command")

    ok = remove_team_subscription(interaction.user.id, team_id)
    if not ok:
        await interaction.response.send_message(
            "У тебя нет подписки на эту команду (проверь /live-list).",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Подписка на команду с ID `{team_id}` удалена.",
        ephemeral=True
    )

@tree.command(name="live-stop-all", description="Удалить все подписки на команды")
@only_in_allowed_channel()
async def live_stop_all(interaction: discord.Interaction):
    await play_sound("command")

    clear_user_subscriptions(interaction.user.id)
    await interaction.response.send_message(
        "Все твои подписки на команды удалены.",
        ephemeral=True
    )

@tree.command(name="live-list", description="Показать твои подписанные команды")
@only_in_allowed_channel()
async def live_list(interaction: discord.Interaction):
    await play_sound("command")

    subs = get_user_subscriptions(interaction.user.id)
    if not subs:
        await interaction.response.send_message(
            "У тебя пока нет подписок на команды. Используй `/live`.",
            ephemeral=True
        )
        return

    desc_lines = [
        f"ID: `{t['team_id']}` — **{t['team_name']}** ({t['league']})"
        for t in subs
    ]

    embed = discord.Embed(
        title="📜 Твои команды",
        description="\n".join(desc_lines),
        colour=discord.Colour.gold()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="live-upcoming", description="Ближайшие матчи по твоим подписанным командам")
@only_in_allowed_channel()
async def live_upcoming(interaction: discord.Interaction):
    await play_sound("command")

    subs = get_user_subscriptions(interaction.user.id)
    if not subs:
        await interaction.response.send_message(
            "У тебя пока нет подписок на команды. Используй `/live`, чтобы подписаться.",
            ephemeral=True
        )
        return

    user_team_ids = [t["team_id"] for t in subs]
    team_names_by_id = {t["team_id"]: t["team_name"] for t in subs}

    async with aiohttp.ClientSession() as session:
        team_matches = await fetch_upcoming_for_user(session, user_team_ids)

    if not team_matches:
        await interaction.response.send_message(
            "В ближайшие две недели нет матчей для твоих подписанных команд в поддерживаемых лигах.",
            ephemeral=True
        )
        return

    # сортируем команды по ближайшему матчу
    def parse_utc(m: Dict[str, Any]) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        except Exception:
            return None

    team_order: List[tuple[int, datetime]] = []
    for tid, ms in team_matches.items():
        ms_sorted = sorted(ms, key=lambda mm: mm.get("utcDate", ""))
        first_dt = parse_utc(ms_sorted[0])
        if first_dt is not None:
            team_order.append((tid, first_dt))

    if not team_order:
        await interaction.response.send_message(
            "Не удалось определить время ближайших матчей для подписанных команд.",
            ephemeral=True
        )
        return

    team_order.sort(key=lambda x: x[1])
    selected_team_ids = [tid for tid, _ in team_order[:3]]

    lines: List[str] = []
    MAX_MATCHES_PER_TEAM = 3

    for tid in selected_team_ids:
        matches = sorted(team_matches[tid], key=lambda m: m.get("utcDate", ""))
        team_name = team_names_by_id.get(tid, f"Team {tid}")

        lines.append(f"**{team_name}**")

        for m in matches[:MAX_MATCHES_PER_TEAM]:
            league_name = m["competition"]["name"]
            home = m["homeTeam"]["name"]
            away = m["awayTeam"]["name"]
            utc_iso = m["utcDate"]

            when_str = humanize_time_to_match(utc_iso)
            lines.append(
                f"{league_name}: **{home} - {away}** start time: {when_str}"
            )

        lines.append("")

    description = "\n".join(lines).strip()

    embed = discord.Embed(
        title="📅 Ближайшие матчи твоих команд",
        description=description,
        colour=discord.Colour.blue()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="live-now", description="Матчи, которые сейчас идут по подписанным командам")
@only_in_allowed_channel()
async def live_now(interaction: discord.Interaction):
    await play_sound("command")

    async with aiohttp.ClientSession() as session:
        fixtures = await fetch_live_fixtures(session)

    if not fixtures:
        await interaction.response.send_message(
            "Сейчас нет идущих матчей для подписанных команд.",
            ephemeral=True
        )
        return

    lines = []
    for m in fixtures[:10]:
        league_name = m["competition"]["name"]
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        score = m.get("score", {})
        ft = score.get("fullTime", {}) or {}
        home_goals = ft.get("home") or 0
        away_goals = ft.get("away") or 0
        status = m.get("status", "LIVE")
        lines.append(
            f"**{league_name}** — {home} {home_goals}:{away_goals} {away} ({status})"
        )

    embed = discord.Embed(
        title="📡 Сейчас в эфире (подписанные команды)",
        description="\n".join(lines),
        colour=discord.Colour.green()
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------------- ФОНОВЫЙ ОПРОС LIVE-МАТЧЕЙ ----------------------

@tasks.loop(seconds=180)
async def poll_live_matches():
    """
    Live-ивенты только по матчам подписанных команд. [web:51]
    """
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    text_channel = guild.get_channel(TEXT_CHANNEL_ID) if guild else None

    async with aiohttp.ClientSession() as session:
        fixtures = await fetch_live_fixtures(session)

    global last_fixtures_state
    current_state: Dict[int, Dict[str, Any]] = {}
    db = load_subscriptions()
    users = db.get("users", {})

    notifications: List[Dict[str, Any]] = []

    for m in fixtures:
        match_id = m["id"]
        current_state[match_id] = m

        prev = last_fixtures_state.get(match_id)

        status = m.get("status")
        score = m.get("score", {})
        ft = score.get("fullTime", {}) or {}
        home_goals = ft.get("home") or 0
        away_goals = ft.get("away") or 0

        if prev is None and status not in ("SCHEDULED", "TIMED", "POSTPONED", "CANCELLED"):
            notifications.append({"type": "start", "match": m, "message": "Матч начался!"})

        if prev is not None:
            prev_score = prev.get("score", {})
            prev_ft = prev_score.get("fullTime", {}) or {}
            prev_home = prev_ft.get("home") or 0
            prev_away = prev_ft.get("away") or 0
            if home_goals != prev_home or away_goals != prev_away:
                notifications.append({"type": "goal", "match": m, "message": "Забит гол!"})

        if status == "PAUSED" and (prev is None or prev.get("status") != "PAUSED"):
            notifications.append({"type": "pause", "match": m, "message": "Перерыв в матче."})

        if status == "FINISHED" and (prev is None or prev.get("status") != "FINISHED"):
            notifications.append({"type": "end", "match": m, "message": "Матч окончен."})

    last_fixtures_state = current_state

    for note in notifications:
        m = note["match"]
        home = m["homeTeam"]
        away = m["awayTeam"]
        league_name = m["competition"]["name"]
        score = m.get("score", {})
        ft = score.get("fullTime", {}) or {}
        home_goals = ft.get("home") or 0
        away_goals = ft.get("away") or 0

        involved_team_ids = {home["id"], away["id"]}

        matched_users: List[int] = []
        for user_id_str, entry in users.items():
            user_teams = {t["team_id"] for t in entry.get("teams", [])}
            if user_teams & involved_team_ids:
                matched_users.append(int(user_id_str))

        if not matched_users:
            continue

        if note["type"] == "goal":
            await play_sound("goal")
        elif note["type"] == "end":
            await play_sound("match_end")
        elif note["type"] == "start":
            await play_sound("match_start")
        elif note["type"] == "pause":
            await play_sound("timeout")

        text = (
            f"**{note['message']}**\n"
            f"Турнир: **{league_name}**\n"
            f"Матч: **{home['name']} {home_goals}:{away_goals} {away['name']}**"
        )

        embed = discord.Embed(
            title="⚽ Уведомление о матче",
            description=text,
            colour=discord.Colour.orange()
        )

        for user_id in matched_users:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            try:
                await user.send(embed=embed)
            except discord.Forbidden:
                pass

        if text_channel and text_channel.permissions_for(guild.me).send_messages:
            await text_channel.send(embed=embed)

# --------------------------- ЖИЗНЕННЫЙ ЦИКЛ БОТА --------------------------

@bot.event
async def on_ready():
    print(f"Вошёл как {bot.user} (ID: {bot.user.id})")
    await bot.wait_until_ready()
    await bot.change_presence(activity=discord.Game(name="Футбол (football-data.org)"))

    await ensure_voice_connected()

    async with aiohttp.ClientSession() as session:
        await build_teams_cache(session)

    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

    if not poll_live_matches.is_running():
        poll_live_matches.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Не задан DISCORD_TOKEN.")
    if not FOOTBALL_DATA_TOKEN:
        raise RuntimeError("Не задан FOOTBALL_DATA_TOKEN.")
    bot.run(DISCORD_TOKEN)
